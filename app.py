"""
Bikeodon web frontend.

Run:  flask --app app run
  or: python app.py
"""

import os
import threading

import yaml
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, flash, redirect, render_template, request, send_from_directory, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_login import (
    LoginManager, UserMixin, current_user,
    login_required, login_user, logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash

import json

from database import (
    _conn, clear_rendered, count_activities, create_user, enrich_activity_stream,
    find_overlapping_activity, get_activity, get_admin_stats, get_all_peak_powers,
    get_activities_without_metrics, get_daily_loads, get_error_activities,
    get_followers, get_following, get_setting, get_site_setting, get_stream, get_user_by_athlete_id, get_user_by_id,
    get_user_by_username, get_user_stats, get_zone_totals, get_zones, init_db,
    apply_zone_preset, HR_ZONE_PRESETS, POWER_ZONE_PRESETS,
    job_finish, job_start, get_recent_jobs,
    list_activities, list_settings, load_user_config, mark_posted, mark_rendered,
    reset_metrics_computed, set_activity_error, set_scheduled, set_setting,
    update_activity_metrics, upsert_activity,
)
from mastodon_client import MastodonClient

SYNC_COOLDOWN_SECS = 15 * 60

# Ensures only one metric backfill runs at a time across all users
_backfill_lock = threading.Lock()


def _sync_cooldown_remaining(user_id: int) -> int:
    """Return seconds until the user may sync again, or 0 if available."""
    last = get_setting(DB_PATH, user_id, "strava", "last_manual_sync_at")
    if not last:
        return 0
    try:
        from datetime import datetime, timezone
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return max(0, int(SYNC_COOLDOWN_SECS - elapsed))
    except (ValueError, AttributeError):
        return 0


# ---------------------------------------------------------------------------
# Shared rendering helper (used by webhook, manual sync, upload, rerender)
# ---------------------------------------------------------------------------

def _render_and_track(activity_id: int, uid: int, cfg: dict, out_dir: str, row=None):
    """Render map + charts for an activity, storing any errors in the DB."""
    if row is None:
        row = get_activity(DB_PATH, activity_id, user_id=uid)
    if not row:
        return
    os.makedirs(out_dir, exist_ok=True)
    errors = []

    from database import get_points as _gp
    pts = _gp(row)
    if pts:
        try:
            img = render_activity_map(pts, dict(row), cfg)
            if img:
                img.save(os.path.join(out_dir, f"{activity_id}.png"))
            mark_rendered(DB_PATH, activity_id, uid, map=True)
        except Exception as e:
            errors.append(f"map: {e}")
            print(f"[render] map failed for {activity_id}: {e}")
    else:
        mark_rendered(DB_PATH, activity_id, uid, map=True)

    if not cfg["charts"]["power"]["ftp"]:
        v = get_setting(DB_PATH, uid, "inference", "ftp")
        if v:
            cfg["charts"]["power"]["ftp"] = float(v)
    if not cfg["charts"]["heart_rate"]["max_hr"]:
        v = get_setting(DB_PATH, uid, "inference", "max_hr")
        if v:
            cfg["charts"]["heart_rate"]["max_hr"] = float(v)

    stream = get_stream(row)
    try:
        generate_charts(activity_id, stream, cfg, out_dir, db_path=DB_PATH)
        mark_rendered(DB_PATH, activity_id, uid, charts=True)
    except Exception as e:
        errors.append(f"charts: {e}")
        print(f"[render] charts failed for {activity_id}: {e}")

    set_activity_error(DB_PATH, activity_id, uid, "render",
                       "; ".join(errors) if errors else None)

    _compute_and_store_metrics(activity_id, uid, cfg, stream, row)


def _compute_and_store_metrics(activity_id: int, uid: int, cfg: dict, stream: list, row):
    """Compute NP, TSS, TRIMP, peak powers, and zone times from stream data and store them."""
    try:
        row = dict(row)
        watts_list   = [p.get("power")        for p in stream]
        hr_list      = [p.get("hr")           for p in stream]
        elapsed_list = [p.get("elapsed_secs") for p in stream]

        ftp    = cfg["charts"]["power"]["ftp"]
        hr_max = cfg["charts"]["heart_rate"]["max_hr"]

        # Fall back to inference cache (populated once per backfill run)
        if not ftp:
            v = get_setting(DB_PATH, uid, "inference", "ftp")
            ftp = float(v) if v else None
        if not hr_max:
            v = get_setting(DB_PATH, uid, "inference", "max_hr")
            hr_max = float(v) if v else None

        hr_rest  = float(get_setting(DB_PATH, uid, "training", "hr_rest") or 0) or None
        lthr     = float(get_setting(DB_PATH, uid, "training", "lthr")     or 0) or None
        # Fall back to 88% of max HR if LTHR not explicitly set
        if not lthr and hr_max:
            lthr = hr_max * 0.88
        duration = row["moving_time"] or row["elapsed_time"]

        np_w     = compute_np(watts_list)
        tss      = compute_tss(np_w, duration, ftp) if np_w else None
        trimp    = compute_trimp(hr_list, elapsed_list, hr_max, hr_rest) if hr_max and hr_rest else None
        peaks    = compute_peak_powers(stream)
        peak_json = json.dumps(peaks) if peaks else None

        # Breakthrough detection — only on first-time metrics computation
        breakthroughs_json = None
        is_first_time = not row.get("peak_power_json") and not row.get("metrics_computed_at")
        if is_first_time:
            breakthrus = []
            if peaks:
                hist_peaks = get_all_peak_powers(DB_PATH, uid, exclude_id=activity_id)
                hist_mmp   = aggregate_power_curve(hist_peaks)
                for label, val in peaks.items():
                    prev = hist_mmp.get(label)
                    if prev is None or val > prev:
                        breakthrus.append({
                            "type": "mmp", "label": label,
                            "watts": round(val), "prev": round(prev) if prev else None,
                        })
            if row.get("max_heartrate"):
                conn_b = _conn(DB_PATH)
                prev_hr = conn_b.execute(
                    "SELECT MAX(max_heartrate) FROM activities"
                    " WHERE user_id=? AND id != ? AND max_heartrate IS NOT NULL",
                    (uid, activity_id),
                ).fetchone()[0]
                conn_b.close()
                if prev_hr is None or row["max_heartrate"] > prev_hr:
                    breakthrus.append({
                        "type": "hr",
                        "bpm": round(row["max_heartrate"]),
                        "prev": round(prev_hr) if prev_hr else None,
                    })
            breakthroughs_json = json.dumps(breakthrus)

        # hrTSS as fallback when power TSS is unavailable
        hr_tss = None
        if tss is None and hr_max and hr_rest and lthr:
            hr_tss = compute_hr_tss(hr_list, elapsed_list, hr_max, hr_rest, lthr)

        hr_zones    = cfg["charts"]["heart_rate"]["zones"]
        power_zones = cfg["charts"]["power"]["zones"]
        hr_zone_secs, power_zone_secs = compute_zone_times(
            stream, hr_zones, power_zones, hr_max, ftp
        )
        hr_zone_json    = json.dumps(hr_zone_secs)    if hr_zone_secs    else None
        power_zone_json = json.dumps(power_zone_secs) if power_zone_secs else None

        update_activity_metrics(
            DB_PATH, activity_id, uid,
            tss, np_w, trimp, peak_json,
            hr_zone_json, power_zone_json,
            hr_tss=hr_tss,
            breakthroughs_json=breakthroughs_json,
        )

        # Update inference cache if this activity sets new bests
        if not cfg["charts"]["power"]["ftp"] and peaks and peaks.get("20min"):
            new_ftp = peaks["20min"] * 0.95
            cached  = get_setting(DB_PATH, uid, "inference", "ftp")
            if not cached or new_ftp > float(cached):
                set_setting(DB_PATH, uid, "inference", "ftp", str(round(new_ftp, 1)))
                print(f"[metrics] Updated inferred FTP: {new_ftp:.0f} W")

        if not cfg["charts"]["heart_rate"]["max_hr"] and row.get("max_heartrate"):
            new_hr = row["max_heartrate"]
            cached = get_setting(DB_PATH, uid, "inference", "max_hr")
            cached_f = float(cached) if cached else 0
            # Guard against sensor spikes: only accept if plausible (≤220) and
            # not more than 10 bpm above the current cached value
            if new_hr <= 220 and new_hr > cached_f and (not cached_f or new_hr <= cached_f + 10):
                set_setting(DB_PATH, uid, "inference", "max_hr", str(round(new_hr, 1)))
                print(f"[metrics] Updated inferred max HR: {new_hr:.0f} bpm")

    except Exception as e:
        print(f"[metrics] Failed for {activity_id}: {e}")


# ---------------------------------------------------------------------------
# Mastodon posting (runs in background thread)
# ---------------------------------------------------------------------------

def _build_post_text(activity: dict, template: str) -> str:
    def fmt_time(secs):
        if not secs:
            return "?"
        h, m = divmod(int(secs) // 60, 60)
        return f"{h}h {m:02d}m" if h else f"{m}m"
    return template.format(
        name          = activity.get("name", "Activity"),
        distance_km   = (activity.get("distance") or 0) / 1000,
        elevation_m   = activity.get("total_elevation_gain") or 0,
        moving_time   = fmt_time(activity.get("moving_time")),
        average_speed = (activity.get("average_speed") or 0) * 3.6,
        date          = (activity.get("start_date") or "")[:10],
        sport_type    = activity.get("sport_type") or "",
    )


def _do_post_activity(activity_id: int, uid: int):
    """Post an activity to Mastodon. Meant to run in a background thread."""
    row = get_activity(DB_PATH, activity_id, user_id=uid)
    if not row:
        return

    cfg     = load_user_config(DB_PATH, uid, _base_cfg)
    out_dir = _base_cfg["map"].get("output_dir", "output")
    os.makedirs(out_dir, exist_ok=True)
    img_path = os.path.join(out_dir, f"{activity_id}.png")

    # Ensure map exists
    if not os.path.exists(img_path):
        _render_and_track(activity_id, uid, cfg, out_dir, row=row)
        row = get_activity(DB_PATH, activity_id, user_id=uid)
        if not row or row["render_error"]:
            msg = (row["render_error"] if row else "render failed")
            set_activity_error(DB_PATH, activity_id, uid, "post", f"render required first: {msg}")
            set_scheduled(DB_PATH, activity_id, uid, False)
            return

    try:
        text        = _build_post_text(dict(row), cfg["mastodon"].get("post_template", "{name}\n#cycling"))
        stream      = get_stream(row)
        chart_paths = generate_charts(activity_id, stream, cfg, out_dir, db_path=DB_PATH)
        images      = ([img_path] if os.path.exists(img_path) else []) + chart_paths
        images      = images[:4]

        client    = MastodonClient.from_cfg(cfg)
        media_ids = [client.upload_image(p) for p in images]
        resp = client._session.post(
            f"{client._base}/api/v1/statuses",
            json={
                "status":     text,
                "media_ids":  media_ids,
                "visibility": cfg["mastodon"].get("visibility", "public"),
            },
        )
        resp.raise_for_status()
        post_url = resp.json().get("url", "")
        mark_posted(DB_PATH, activity_id, post_url, user_id=uid)
        set_activity_error(DB_PATH, activity_id, uid, "post", None)
        print(f"[post] Posted activity {activity_id} → {post_url}")
    except Exception as e:
        set_activity_error(DB_PATH, activity_id, uid, "post", str(e))
        set_scheduled(DB_PATH, activity_id, uid, False)
        print(f"[post] Failed to post activity {activity_id}: {e}")
from activity_parser import parse_file
from charts import generate_charts
from map_renderer import render_activity_map
from strava import StravaClient, exchange_code, strava_auth_url
from training_load import (
    aggregate_power_curve, compute_hr_tss, compute_np, compute_peak_powers,
    compute_pmc, compute_trimp, compute_tss, compute_wbal, compute_zone_times,
    fit_critical_power, weekly_load,
)
from inference import infer_training_params

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

with open(os.environ.get("BIKEODON_CONFIG", "config.yaml")) as f:
    _base_cfg = yaml.safe_load(f)

DB_PATH              = _base_cfg["database"]["path"]
STRAVA_CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key                      = os.environ.get("FLASK_SECRET_KEY", "dev-key-change-me-in-production")
app.config["DB_PATH"]               = DB_PATH
app.config["PREFERRED_URL_SCHEME"]  = "https"

init_db(DB_PATH)

from activitypub import bp as _ap_bp, start_delivery_worker
app.register_blueprint(_ap_bp)
start_delivery_worker(DB_PATH)

# Log unhandled exceptions to a file so they're visible without a debugger attached.
import logging
_log_path = os.path.join(os.path.dirname(DB_PATH), "bikeodon_errors.log")
logging.basicConfig(
    filename=_log_path,
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
app.logger.setLevel(logging.ERROR)


@app.errorhandler(500)
def internal_error(exc):
    import traceback
    tb = traceback.format_exc()
    app.logger.error("500 on %s\n%s", request.path, tb)
    return (
        f"<pre style='padding:2rem;font-family:monospace;white-space:pre-wrap'>"
        f"<strong>500 Internal Server Error</strong>\n\n{tb}</pre>"
    ), 500


app.jinja_env.globals["enumerate"] = enumerate

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

login_manager = LoginManager(app)
login_manager.login_view = "login"


class User(UserMixin):
    def __init__(self, id, username, is_admin=False):
        self.id       = str(id)
        self.username = username
        self.is_admin = is_admin


@login_manager.user_loader
def load_user(user_id):
    row = get_user_by_id(DB_PATH, int(user_id))
    if row and row["username"]:
        return User(row["id"], row["username"], bool(row["is_admin"]))
    return None


def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Admin access required.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username    = request.form["username"].strip()
        password    = request.form["password"].strip()
        invite_code = request.form.get("invite_code", "").strip()
        if not username or not password:
            flash("Username and password are required.", "error")
            return render_template("register.html")
        required_code = get_site_setting(DB_PATH, "invite_code")
        if required_code and invite_code != required_code:
            flash("Invalid invite code.", "error")
            return render_template("register.html")
        if get_user_by_username(DB_PATH, username):
            flash("Username already taken.", "error")
            return render_template("register.html")
        user_id = create_user(DB_PATH, username, generate_password_hash(password))
        user = User(user_id, username)
        login_user(user)
        flash("Account created!", "success")
        return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()
        row = get_user_by_username(DB_PATH, username)
        if not row or not check_password_hash(row["password_hash"] or "", password):
            flash("Invalid username or password.", "error")
            return render_template("login.html")
        login_user(User(row["id"], row["username"]))
        return redirect(request.args.get("next") or url_for("index"))
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

PER_PAGE = 20

@app.route("/")
def index():
    if not current_user.is_authenticated:
        return render_template("landing.html")
    uid  = int(current_user.id)
    sort = request.args.get("sort", "date")
    dir_ = request.args.get("dir", "desc")
    page = max(1, request.args.get("page", 1, type=int))

    total    = count_activities(DB_PATH, uid)
    n_pages  = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page     = min(page, n_pages)
    offset   = (page - 1) * PER_PAGE

    rows = list_activities(DB_PATH, user_id=uid, limit=PER_PAGE, offset=offset,
                           sort=sort, direction=dir_)
    activities = []
    for r in rows:
        activities.append({
            "id":            r["id"],
            "name":          r["name"] or "—",
            "sport_type":    r["sport_type"] or "",
            "date":          (r["start_date"] or "")[:10],
            "distance":      f"{(r['distance'] or 0) / 1000:.1f} km",
            "elevation":     f"{r['total_elevation_gain'] or 0:.0f} m",
            "distance_raw":  (r["distance"] or 0) / 1000,
            "elevation_raw": r["total_elevation_gain"] or 0,
            "posted":        bool(r["posted_at"]),
            "scheduled":     bool(r["scheduled_for_post"]),
            "post_url":      r["mastodon_post_url"] or "",
            "strava_url":    r["strava_url"] or "",
            "has_breakthrough": bool(r["breakthroughs_json"] and
                                     r["breakthroughs_json"] != "[]"),
        })
    strava_connected  = bool(get_setting(DB_PATH, uid, "strava", "access_token"))
    sync_remaining    = _sync_cooldown_remaining(uid) if strava_connected else 0
    sync_mins_left    = (sync_remaining + 59) // 60 if sync_remaining > 0 else 0
    return render_template("index.html", activities=activities,
                           strava_connected=strava_connected,
                           sync_available=(sync_remaining == 0),
                           sync_mins_left=sync_mins_left,
                           page=page, n_pages=n_pages, total=total,
                           sort=sort, dir=dir_)


# ---------------------------------------------------------------------------
# Manual Strava sync
# ---------------------------------------------------------------------------

@app.route("/sync", methods=["POST"])
@login_required
def manual_sync():
    uid = int(current_user.id)

    remaining = _sync_cooldown_remaining(uid)
    if remaining > 0:
        mins = (remaining + 59) // 60
        flash(f"Sync rate-limited — try again in {mins} min.", "error")
        return redirect(url_for("index"))

    from datetime import datetime, timezone
    set_setting(DB_PATH, uid, "strava", "last_manual_sync_at",
                datetime.now(timezone.utc).isoformat())

    def _run():
        access_token = get_setting(DB_PATH, uid, "strava", "access_token") or ""
        refresh_tok  = get_setting(DB_PATH, uid, "strava", "refresh_token") or ""
        expires_at   = float(get_setting(DB_PATH, uid, "strava", "token_expires_at") or 0)
        if not access_token:
            return

        def _on_refresh(a, r, e):
            set_setting(DB_PATH, uid, "strava", "access_token",     a)
            set_setting(DB_PATH, uid, "strava", "refresh_token",    r)
            set_setting(DB_PATH, uid, "strava", "token_expires_at", str(e))

        client = StravaClient(
            access_token=access_token, client_id=STRAVA_CLIENT_ID,
            client_secret=STRAVA_CLIENT_SECRET, refresh_tok=refresh_tok,
            expires_at=expires_at, on_refresh=_on_refresh,
        )
        try:
            ids = client.get_activity_ids(n=10)
        except Exception as e:
            print(f"[manual-sync] Strava API error: {e}")
            return

        cfg     = load_user_config(DB_PATH, uid, _base_cfg)
        out_dir = _base_cfg["map"].get("output_dir", "output")
        os.makedirs(out_dir, exist_ok=True)

        new_ids = []
        for activity_id in ids:
            if get_activity(DB_PATH, activity_id, user_id=uid):
                continue
            try:
                data = client.get_activity(activity_id)
                upsert_activity(DB_PATH, data, user_id=uid)
                new_ids.append(activity_id)
                print(f"[manual-sync] + {data['name']}")
            except Exception as e:
                print(f"[manual-sync] Failed {activity_id}: {e}")

        for activity_id in new_ids:
            _render_and_track(activity_id, uid, cfg, out_dir)

        print(f"[manual-sync] Done — {len(new_ids)} new activities for user {uid}")

    threading.Thread(target=_run, daemon=True).start()
    flash("Syncing from Strava — new activities will appear shortly.", "success")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Activity detail
# ---------------------------------------------------------------------------

def _fmt_time(secs):
    if not secs:
        return None
    h, m = divmod(int(secs) // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


@app.route("/activity/<int:activity_id>")
@login_required
def activity(activity_id):
    uid = int(current_user.id)
    row = get_activity(DB_PATH, activity_id, user_id=uid)
    if not row:
        flash("Activity not found.", "error")
        return redirect(url_for("index"))

    out_dir = _base_cfg["map"].get("output_dir", "output")

    map_url = None
    map_path = os.path.join(out_dir, f"{activity_id}.png")
    if os.path.exists(map_path):
        map_url = url_for("output_file", filename=f"{activity_id}.png")

    chart_urls = []
    for suffix in ("_hr", "_power"):
        chart_path = os.path.join(out_dir, f"{activity_id}{suffix}.png")
        if os.path.exists(chart_path):
            chart_urls.append(url_for("output_file", filename=f"{activity_id}{suffix}.png"))

    act = {
        "id":         row["id"],
        "name":       row["name"] or "—",
        "sport_type": row["sport_type"] or "",
        "date":       (row["start_date"] or "")[:10],
        "distance":   f"{(row['distance'] or 0) / 1000:.1f} km" if row["distance"] else None,
        "elevation":  f"{row['total_elevation_gain'] or 0:.0f} m" if row["total_elevation_gain"] is not None else None,
        "moving_time": _fmt_time(row["moving_time"]),
        "avg_speed":  f"{(row['max_speed'] or 0) * 3.6:.1f} km/h" if row["max_speed"] else None,
        "avg_hr":     f"{row['average_heartrate']:.0f}" if row["average_heartrate"] else None,
        "avg_watts":  f"{row['average_watts']:.0f}" if row["average_watts"] else None,
        "tss":        round(row["tss"])    if row["tss"]    is not None else None,
        "hr_tss":     round(row["hr_tss"]) if row["hr_tss"] is not None else None,
        "np_watts":   round(row["np_watts"]) if row["np_watts"] is not None else None,
        "strava_url":    row["strava_url"] or "",
        "post_url":      row["mastodon_post_url"] or "",
        "scheduled":     bool(row["scheduled_for_post"]),
        "render_error":  row["render_error"] or "",
        "post_error":    row["post_error"] or "",
        "breakthroughs": json.loads(row["breakthroughs_json"])
                         if row["breakthroughs_json"] else [],
    }

    mastodon_configured = bool(get_setting(DB_PATH, uid, "mastodon", "token"))
    has_avg_watts   = bool(row["average_watts"])
    has_power_chart = os.path.exists(os.path.join(out_dir, f"{activity_id}_power.png"))

    # W' balance — only available when this activity has power data and CP is cached
    wbal_json = None
    act_cp = act_w_prime = None
    _cp_v     = get_setting(DB_PATH, uid, "inference", "cp")
    _wprime_v = get_setting(DB_PATH, uid, "inference", "w_prime")
    if row["average_watts"] and _cp_v and _wprime_v:
        try:
            _stream = get_stream(row)
            _wbal   = compute_wbal(_stream, float(_cp_v), float(_wprime_v))
            if _wbal:
                wbal_json   = json.dumps(_wbal)
                act_cp      = float(_cp_v)
                act_w_prime = float(_wprime_v)
        except Exception as _e:
            print(f"[wbal] Failed for activity {activity_id}: {_e}")

    return render_template("activity.html", activity=act, map_url=map_url,
                           chart_urls=chart_urls, mastodon_configured=mastodon_configured,
                           has_avg_watts=has_avg_watts, has_power_chart=has_power_chart,
                           wbal_json=wbal_json, cp=act_cp, w_prime=act_w_prime)


@app.route("/activity/<int:activity_id>/rerender", methods=["POST"])
@login_required
def rerender_activity(activity_id):
    uid = int(current_user.id)
    row = get_activity(DB_PATH, activity_id, user_id=uid)
    if not row:
        flash("Activity not found.", "error")
        return redirect(url_for("index"))

    cfg     = load_user_config(DB_PATH, uid, _base_cfg)
    out_dir = _base_cfg["map"].get("output_dir", "output")
    clear_rendered(DB_PATH, activity_id, uid)

    def _do_render():
        try:
            _render_and_track(activity_id, uid, cfg, out_dir, row=row)
        except Exception as e:
            print(f"[rerender] Failed for {activity_id}: {e}")

    threading.Thread(target=_do_render, daemon=True).start()
    flash("Re-rendering in the background — refresh in a few seconds.", "success")
    return redirect(url_for("activity", activity_id=activity_id))


@app.route("/activity/<int:activity_id>/schedule", methods=["POST"])
@login_required
def schedule_activity(activity_id):
    uid = int(current_user.id)
    row = get_activity(DB_PATH, activity_id, user_id=uid)
    if not row:
        flash("Activity not found.", "error")
        return redirect(url_for("index"))

    if row["posted_at"]:
        return redirect(request.referrer or url_for("activity", activity_id=activity_id))

    if row["scheduled_for_post"]:
        # Cancel an in-progress post
        set_scheduled(DB_PATH, activity_id, uid, False)
    else:
        # Post now (or retry after failure)
        if row["post_error"]:
            set_activity_error(DB_PATH, activity_id, uid, "post", None)
        set_scheduled(DB_PATH, activity_id, uid, True)
        threading.Thread(target=_do_post_activity, args=(activity_id, uid), daemon=True).start()

    return redirect(request.referrer or url_for("activity", activity_id=activity_id))


@app.route("/screenshots/<path:filename>")
def screenshot(filename):
    docs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "screenshots")
    return send_from_directory(docs_dir, filename)


@app.route("/output/<path:filename>")
@login_required
def output_file(filename):
    out_dir = os.path.abspath(_base_cfg["map"].get("output_dir", "output"))
    response = send_from_directory(out_dir, filename)
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/users/<username>/avatar")
def user_avatar(username):
    """Serve a user's avatar image publicly (needed for ActivityPub icon URL)."""
    user = get_user_by_username(DB_PATH, username)
    avatar = dict(user).get("avatar_filename") if user else None
    if not avatar:
        return app.send_static_file("default_avatar.png")
    avatars_dir = os.path.abspath(os.path.join(
        _base_cfg["map"].get("output_dir", "output"), "avatars"
    ))
    return send_from_directory(avatars_dir, avatar)


@app.route("/me/profile", methods=["POST"])
@login_required
def save_profile():
    uid  = int(current_user.id)
    conn = _conn(DB_PATH)

    display_name = request.form.get("display_name", "").strip()
    summary      = request.form.get("summary", "").strip()

    avatar_filename = None
    f = request.files.get("avatar")
    if f and f.filename:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            flash("Avatar must be a JPG, PNG, GIF or WebP image.", "error")
            return redirect(url_for("me", tab="profile"))
        avatars_dir = os.path.abspath(os.path.join(
            _base_cfg["map"].get("output_dir", "output"), "avatars"
        ))
        os.makedirs(avatars_dir, exist_ok=True)
        avatar_filename = f"{uid}{ext}"
        f.save(os.path.join(avatars_dir, avatar_filename))

    updates = {"display_name": display_name or None, "summary": summary or None}
    if avatar_filename:
        updates["avatar_filename"] = avatar_filename

    set_clause = ", ".join(f"{k}=?" for k in updates)
    conn.execute(
        f"UPDATE users SET {set_clause} WHERE id=?",
        (*updates.values(), uid),
    )
    conn.commit()
    flash("Profile updated.", "success")
    return redirect(url_for("me", tab="profile"))


@app.route("/ap/follow", methods=["POST"])
@login_required
def ap_follow():
    from activitypub import send_follow
    actor_url = request.form.get("actor_url", "").strip()
    if not actor_url:
        abort(400)
    uid  = int(current_user.id)
    user = get_user_by_id(DB_PATH, uid)
    send_follow(current_user.username, user, actor_url, DB_PATH)
    return redirect(request.referrer or url_for("me", tab="followers"))


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "GET":
        return render_template("upload.html")

    uid     = int(current_user.id)
    files   = request.files.getlist("files")
    cfg     = load_user_config(DB_PATH, uid, _base_cfg)
    out_dir = _base_cfg["map"].get("output_dir", "output")
    os.makedirs(out_dir, exist_ok=True)

    imported = 0
    enriched = 0
    skipped  = 0
    errors   = []

    for f in files:
        if not f.filename:
            continue
        content = f.read()
        try:
            activities = parse_file(f.filename, content)
        except Exception as e:
            errors.append(f"{f.filename}: {e}")
            continue

        for act in activities:
            # Exact ID match → already imported, skip
            if get_activity(DB_PATH, act["id"], user_id=uid):
                skipped += 1
                continue

            # Time overlap with an existing activity → enrich its stream
            overlap_row = find_overlapping_activity(
                DB_PATH, uid,
                act.get("start_date"),
                act.get("elapsed_time") or act.get("moving_time"),
            )
            if overlap_row:
                file_points = act.get("points") or []
                enrich_activity_stream(DB_PATH, overlap_row["id"], uid, file_points)
                enriched_row = get_activity(DB_PATH, overlap_row["id"], user_id=uid)
                if enriched_row:
                    file_has_gps = any(p[0] is not None for p in file_points)
                    map_path = os.path.join(out_dir, f"{overlap_row['id']}.png")
                    if file_has_gps and not os.path.exists(map_path):
                        # Full re-render when file adds GPS we didn't have
                        clear_rendered(DB_PATH, overlap_row["id"], uid)
                        _render_and_track(overlap_row["id"], uid, cfg, out_dir, row=enriched_row)
                    else:
                        # Charts only — map already rendered or no GPS in file
                        clear_rendered(DB_PATH, overlap_row["id"], uid, map=False, charts=True)
                        _render_and_track(overlap_row["id"], uid, cfg, out_dir, row=enriched_row)
                enriched += 1
                continue

            # No match → import as a new activity
            upsert_activity(DB_PATH, act, user_id=uid, source="upload")
            _render_and_track(act["id"], uid, cfg, out_dir)

            imported += 1

    if imported:
        flash(f"Imported {imported} activit{'y' if imported == 1 else 'ies'}.", "success")
    if enriched:
        flash(f"Enriched {enriched} existing activit{'y' if enriched == 1 else 'ies'} with stream data from file.", "success")
    if skipped:
        flash(f"{skipped} already in your library — skipped.", "success")
    for e in errors:
        flash(e, "error")

    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------

@app.route("/admin")
@login_required
@admin_required
def admin():
    stats        = get_admin_stats(DB_PATH)
    errors       = get_error_activities(DB_PATH)
    recent_jobs  = get_recent_jobs(DB_PATH)
    backfill_running = _backfill_lock.locked()
    return render_template("admin.html", stats=stats, errors=errors,
                           recent_jobs=recent_jobs, backfill_running=backfill_running)


@app.route("/admin/full-sync", methods=["POST"])
@login_required
@admin_required
def admin_full_sync():
    uid = int(current_user.id)

    def _run():
        from strava import StravaClient as _SC
        access_token  = get_setting(DB_PATH, uid, "strava", "access_token") or ""
        refresh_tok   = get_setting(DB_PATH, uid, "strava", "refresh_token") or ""
        expires_at    = float(get_setting(DB_PATH, uid, "strava", "token_expires_at") or 0)
        if not access_token:
            return

        def _on_refresh(a, r, e):
            set_setting(DB_PATH, uid, "strava", "access_token",     a)
            set_setting(DB_PATH, uid, "strava", "refresh_token",    r)
            set_setting(DB_PATH, uid, "strava", "token_expires_at", str(e))

        client = _SC(access_token=access_token, client_id=STRAVA_CLIENT_ID,
                     client_secret=STRAVA_CLIENT_SECRET, refresh_tok=refresh_tok,
                     expires_at=expires_at, on_refresh=_on_refresh)

        print(f"[full-sync] Fetching all activity IDs for user {uid}…")
        all_ids = client.get_all_activity_ids()
        print(f"[full-sync] {len(all_ids)} total activities found.")

        new_ids = []
        for activity_id in all_ids:
            if get_activity(DB_PATH, activity_id, user_id=uid):
                continue
            try:
                data = client.get_activity(activity_id)
                upsert_activity(DB_PATH, data, user_id=uid)
                new_ids.append(activity_id)
                print(f"[full-sync] + {data['name']}")
            except Exception as e:
                print(f"[full-sync] Failed {activity_id}: {e}")

        if new_ids:
            cfg = load_user_config(DB_PATH, uid, _base_cfg)
            _render_new(new_ids, uid, cfg)
        print(f"[full-sync] Done — {len(new_ids)} new activities imported.")

    def _render_new(new_ids, uid, cfg):
        out_dir = _base_cfg["map"].get("output_dir", "output")
        for activity_id in new_ids:
            _render_and_track(activity_id, uid, cfg, out_dir)

    threading.Thread(target=_run, daemon=True).start()
    flash("Full sync started in the background — check logs for progress.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/recompute-metrics", methods=["POST"])
@login_required
@admin_required
def admin_recompute_metrics():
    """Clear metrics_computed_at for all activities and trigger a fresh backfill."""
    uid = int(current_user.id)
    reset_metrics_computed(DB_PATH, uid)
    # Invalidate cached inference so it runs fresh
    set_setting(DB_PATH, uid, "inference", "ftp", "")
    set_setting(DB_PATH, uid, "inference", "max_hr", "")
    flash("Metrics reset. Visit the You page to trigger recomputation.", "success")
    return redirect(url_for("admin"))


# ---------------------------------------------------------------------------
# You / user dashboard
# ---------------------------------------------------------------------------

@app.route("/me")
@login_required
def me():
    uid = int(current_user.id)
    tab = request.args.get("tab", "overview")

    stats = get_user_stats(DB_PATH, uid)
    cfg   = load_user_config(DB_PATH, uid, _base_cfg)

    # Backfill metrics for activities that have never been processed.
    # The lock ensures at most one backfill runs at a time (across all page loads).
    def _backfill():
        if not _backfill_lock.acquire(blocking=False):
            return   # another backfill is already running
        try:
            pending = get_activities_without_metrics(DB_PATH, uid)
            if not pending:
                return
            job_id = job_start(DB_PATH, "metrics_backfill",
                               f"Starting — {len(pending)} activities pending")
            print(f"[backfill] Starting metrics for {len(pending)} activities (user {uid})")

            # Run inference once and cache it so each activity can use it without re-scanning all streams
            bcfg_pre = load_user_config(DB_PATH, uid, _base_cfg)
            if not bcfg_pre["charts"]["power"]["ftp"] or not bcfg_pre["charts"]["heart_rate"]["max_hr"]:
                inferred = infer_training_params(DB_PATH)
                if inferred["ftp"] and not bcfg_pre["charts"]["power"]["ftp"]:
                    set_setting(DB_PATH, uid, "inference", "ftp", str(round(inferred["ftp"], 1)))
                    print(f"[backfill] Inferred FTP: {inferred['ftp']:.0f} W")
                if inferred["max_hr"] and not bcfg_pre["charts"]["heart_rate"]["max_hr"]:
                    set_setting(DB_PATH, uid, "inference", "max_hr", str(round(inferred["max_hr"], 1)))
                    print(f"[backfill] Inferred max HR: {inferred['max_hr']:.0f} bpm")

            import time as _time
            done = 0
            for brow in pending:
                try:
                    bcfg    = load_user_config(DB_PATH, uid, _base_cfg)
                    bstream = get_stream(brow)
                    _compute_and_store_metrics(brow["id"], uid, bcfg, bstream, brow)
                    done += 1
                except Exception as e:
                    print(f"[backfill] Error on activity {brow['id']}: {e}")
                _time.sleep(0.05)   # yield CPU between activities
            job_finish(DB_PATH, job_id, "done",
                       f"Computed metrics for {done}/{len(pending)} activities")
            print(f"[backfill] Done — {done}/{len(pending)} activities processed")
        except Exception as e:
            print(f"[backfill] Fatal error: {e}")
        finally:
            _backfill_lock.release()

    threading.Thread(target=_backfill, daemon=True).start()

    # PMC and weekly load
    daily_loads = get_daily_loads(DB_PATH, uid)
    pmc_rows    = compute_pmc(daily_loads, days=180)
    weekly_rows = weekly_load(daily_loads, weeks=26)

    # Current fitness snapshot (last PMC point)
    pmc_latest = pmc_rows[-1] if pmc_rows else {"ctl": 0, "atl": 0, "tsb": 0}

    # Power curve
    ftp = cfg["charts"]["power"]["ftp"]
    body_weight = float(get_setting(DB_PATH, uid, "training", "body_weight_kg") or 0) or None
    all_peaks    = get_all_peak_powers(DB_PATH, uid)
    recent_peaks = get_all_peak_powers(DB_PATH, uid, days=90)
    curve_all    = aggregate_power_curve(all_peaks)
    curve_recent = aggregate_power_curve(recent_peaks)

    # Critical Power model fit from aggregated MMP curve
    cp, w_prime = fit_critical_power(curve_all)
    if cp:
        set_setting(DB_PATH, uid, "inference", "cp",      str(cp))
        set_setting(DB_PATH, uid, "inference", "w_prime", str(w_prime))

    # W/kg variants
    if body_weight:
        wpk_all    = {k: round(v / body_weight, 2) for k, v in curve_all.items()}
        wpk_recent = {k: round(v / body_weight, 2) for k, v in curve_recent.items()}
    else:
        wpk_all = wpk_recent = {}

    # Zones
    hr_zones    = get_zones(DB_PATH, uid, "hr")
    power_zones = get_zones(DB_PATH, uid, "power")
    hr_max      = cfg["charts"]["heart_rate"]["max_hr"]
    hr_rest     = float(get_setting(DB_PATH, uid, "training", "hr_rest") or 0) or None

    # Zone distribution totals
    hr_totals, power_totals = get_zone_totals(DB_PATH, uid)

    def _zone_chart_data(zones, totals):
        """Build {labels, colors, secs, total} for a zone doughnut chart."""
        data = [{"name": z["name"], "color": z["color"],
                 "secs": totals.get(z["name"], 0.0)} for z in zones]
        return json.dumps(data)

    hr_zone_chart_json    = _zone_chart_data(hr_zones,    hr_totals)
    power_zone_chart_json = _zone_chart_data(power_zones, power_totals)

    # Profile fields for the Profile tab
    profile_row = get_user_by_id(DB_PATH, uid)

    # Followers / Following for those tabs
    followers = get_followers(DB_PATH, current_user.username)
    following = get_following(DB_PATH, current_user.username)
    following_urls = {f["actor_url"] for f in following}

    # Fediverse search on the Following tab
    search_result = None
    search_error  = None
    if tab == "following":
        from activitypub import webfinger_lookup
        q = request.args.get("q", "").strip()
        if q:
            search_result = webfinger_lookup(q)
            if search_result is None:
                search_error = f"Could not find \"{q}\" — check the handle and try again."

    return render_template(
        "me.html",
        username=current_user.username,
        tab=tab,
        stats=stats,
        pmc_json=json.dumps(pmc_rows),
        weekly_json=json.dumps(weekly_rows),
        pmc_latest=pmc_latest,
        ftp=ftp,
        profile=dict(profile_row) if profile_row else {},
        hr_max=hr_max,
        hr_rest=hr_rest,
        body_weight=body_weight,
        cp=cp,
        w_prime=w_prime,
        hr_zones=hr_zones,
        power_zones=power_zones,
        hr_zone_chart_json=hr_zone_chart_json,
        power_zone_chart_json=power_zone_chart_json,
        curve_all=json.dumps(curve_all),
        curve_recent=json.dumps(curve_recent),
        wpk_all=json.dumps(wpk_all),
        wpk_recent=json.dumps(wpk_recent),
        followers=followers,
        following=following,
        following_urls=following_urls,
        search_result=search_result,
        search_error=search_error,
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_STAT_FIELDS = [
    "distance", "elevation_gain", "moving_time", "elapsed_time",
    "average_speed", "max_speed", "average_heartrate", "max_heartrate",
    "average_watts", "max_watts",
]


@app.route("/settings")
@login_required
def settings():
    uid     = int(current_user.id)
    section = request.args.get("section", "strava")
    cfg     = load_user_config(DB_PATH, uid, _base_cfg)
    hr_zones    = get_zones(DB_PATH, uid, "hr")
    power_zones = get_zones(DB_PATH, uid, "power")
    strava_connected   = bool(get_setting(DB_PATH, uid, "strava", "access_token"))
    strava_configured  = bool(STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET)
    active_fields = [
        f.strip()
        for f in (get_setting(DB_PATH, uid, "stats", "fields") or "").split(",")
        if f.strip()
    ]
    inferred_ftp    = get_setting(DB_PATH, uid, "inference", "ftp")
    inferred_max_hr = get_setting(DB_PATH, uid, "inference", "max_hr")
    # Suggest LTHR as 88% of max HR if not explicitly set
    _hr_max_v = cfg["charts"]["heart_rate"]["max_hr"] or (float(inferred_max_hr) if inferred_max_hr else None)
    inferred_lthr = round(_hr_max_v * 0.88) if _hr_max_v else None
    return render_template(
        "settings.html",
        cfg=cfg,
        hr_zones=hr_zones,
        power_zones=power_zones,
        stat_fields=_STAT_FIELDS,
        active_fields=active_fields,
        strava_connected=strava_connected,
        strava_configured=strava_configured,
        inferred_ftp=float(inferred_ftp) if inferred_ftp else None,
        inferred_max_hr=float(inferred_max_hr) if inferred_max_hr else None,
        inferred_lthr=inferred_lthr,
        section=section,
    )


@app.route("/settings/mastodon", methods=["POST"])
@login_required
def save_mastodon():
    uid = int(current_user.id)
    for key in ("instance", "handle", "visibility", "post_template"):
        val = request.form.get(key, "").strip()
        set_setting(DB_PATH, uid, "mastodon", key, val)
    token = request.form.get("token", "").strip()
    if token:
        set_setting(DB_PATH, uid, "mastodon", "token", token)
    flash("Mastodon settings saved.", "success")
    return redirect(url_for("settings", section="mastodon"))


@app.route("/settings/map", methods=["POST"])
@login_required
def save_map():
    uid = int(current_user.id)
    fields = [
        "width", "height", "zoom_offset", "max_tiles", "tiles_url", "tiles_user_agent",
        "padding_top", "padding_bottom", "padding_left", "padding_right",
        "route_color", "route_width", "route_opacity",
        "route_outline_color", "route_outline_width", "route_antialias_scale",
        "start_marker_color", "start_marker_radius",
        "start_marker_outline_color", "start_marker_outline_width",
        "end_marker_color", "end_marker_radius",
        "end_marker_outline_color", "end_marker_outline_width",
    ]
    for key in fields:
        val = request.form.get(key, "").strip()
        if val:
            set_setting(DB_PATH, uid, "map", key, val)
    for key in ("start_marker_enabled", "end_marker_enabled", "watopia_enabled"):
        set_setting(DB_PATH, uid, "map", key, "true" if request.form.get(key) else "false")
    flash("Map settings saved.", "success")
    return redirect(url_for("settings", section="map"))


@app.route("/settings/charts", methods=["POST"])
@login_required
def save_charts():
    uid = int(current_user.id)
    for key in ("style_background_color", "style_text_color", "style_grid_color",
                "style_line_color", "style_power_line_color"):
        val = request.form.get(key, "").strip()
        if val:
            set_setting(DB_PATH, uid, "charts", key, val)
    for key in ("hr_enabled", "power_enabled"):
        set_setting(DB_PATH, uid, "charts", key, "true" if request.form.get(key) else "false")
    for key in ("max_hr", "ftp"):
        val = request.form.get(key, "").strip()
        set_setting(DB_PATH, uid, "charts", key, val if val else "")
    flash("Chart settings saved.", "success")
    return redirect(url_for("settings", section="charts"))


@app.route("/settings/training", methods=["POST"])
@login_required
def save_training():
    uid = int(current_user.id)
    for key in ("body_weight_kg", "hr_rest", "lthr"):
        val = request.form.get(key, "").strip()
        set_setting(DB_PATH, uid, "training", key, val)
    flash("Training settings saved.", "success")
    return redirect(url_for("settings", section="training"))


@app.route("/settings/stats", methods=["POST"])
@login_required
def save_stats():
    uid = int(current_user.id)
    fields = [f for f in _STAT_FIELDS if request.form.get(f"field_{f}")]
    set_setting(DB_PATH, uid, "stats", "fields", ",".join(fields))
    for key in ("enabled", "background_color", "background_opacity",
                "padding", "gap", "font_size", "font_color", "icon_size"):
        val = request.form.get(key, "").strip()
        if val:
            set_setting(DB_PATH, uid, "stats_overlay", key, val)
    set_setting(DB_PATH, uid, "stats_overlay", "enabled",
                "true" if request.form.get("overlay_enabled") else "false")
    flash("Stats settings saved.", "success")
    return redirect(url_for("settings", section="stats"))


@app.route("/settings/zones", methods=["GET", "POST"])
@login_required
def zones():
    uid = int(current_user.id)
    if request.method == "POST":
        zone_type = request.form.get("zone_type", "hr")
        conn = _conn(DB_PATH)
        conn.execute(
            "DELETE FROM zones WHERE user_id=? AND type=?", (uid, zone_type)
        )
        i = 0
        while True:
            name    = request.form.get(f"name_{i}", "").strip()
            max_pct = request.form.get(f"max_pct_{i}", "").strip()
            color   = request.form.get(f"color_{i}", "").strip()
            if not name:
                break
            try:
                conn.execute(
                    "INSERT INTO zones (user_id, type, zone_index, name, max_pct, color)"
                    " VALUES (?,?,?,?,?,?)",
                    (uid, zone_type, i, name, float(max_pct), color),
                )
            except (ValueError, Exception):
                pass
            i += 1
        conn.commit()
        conn.close()
        flash(f"{'HR' if zone_type == 'hr' else 'Power'} zones saved.", "success")
        return redirect(url_for("zones") + f"?type={zone_type}")

    zone_type   = request.args.get("type", "hr")
    hr_zones    = get_zones(DB_PATH, uid, "hr")
    power_zones = get_zones(DB_PATH, uid, "power")
    return render_template(
        "zones.html",
        hr_zones=hr_zones, power_zones=power_zones,
        active_type=zone_type,
        hr_presets=list(HR_ZONE_PRESETS.keys()),
        power_presets=list(POWER_ZONE_PRESETS.keys()),
    )


@app.route("/settings/zones/preset", methods=["POST"])
@login_required
def apply_preset():
    uid       = int(current_user.id)
    zone_type = request.form.get("zone_type", "hr")
    preset    = request.form.get("preset", "")
    try:
        apply_zone_preset(DB_PATH, uid, zone_type, preset)
        flash(f"Applied {preset} {zone_type.upper()} zone preset.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("zones") + f"?type={zone_type}")


# ---------------------------------------------------------------------------
# Strava OAuth
# ---------------------------------------------------------------------------

@app.route("/strava/connect")
@login_required
def strava_connect():
    if not STRAVA_CLIENT_ID or not STRAVA_CLIENT_SECRET:
        flash("Add STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET to .env and restart the server.", "error")
        return redirect(url_for("settings"))
    redirect_uri = url_for("strava_callback", _external=True)
    return redirect(strava_auth_url(STRAVA_CLIENT_ID, redirect_uri))


@app.route("/strava/callback")
@login_required
def strava_callback():
    error = request.args.get("error")
    if error:
        flash(f"Strava authorization denied: {error}", "error")
        return redirect(url_for("settings"))

    code = request.args.get("code", "")
    if not code:
        flash("No authorization code received from Strava.", "error")
        return redirect(url_for("settings"))

    try:
        data = exchange_code(STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, code)
    except Exception as e:
        flash(f"Strava token exchange failed: {e}", "error")
        return redirect(url_for("settings"))

    uid = int(current_user.id)
    set_setting(DB_PATH, uid, "strava", "access_token",    data["access_token"])
    set_setting(DB_PATH, uid, "strava", "refresh_token",   data["refresh_token"])
    set_setting(DB_PATH, uid, "strava", "token_expires_at", str(data["expires_at"]))
    athlete = data.get("athlete", {})
    if athlete.get("id"):
        set_setting(DB_PATH, uid, "strava", "athlete_id", str(athlete["id"]))

    flash("Strava connected successfully!", "success")
    return redirect(url_for("settings"))


@app.route("/strava/webhook", methods=["GET"])
def strava_webhook_verify():
    verify_token = os.environ.get("STRAVA_WEBHOOK_VERIFY_TOKEN", "")
    if request.args.get("hub.verify_token") != verify_token:
        return "Forbidden", 403
    return {"hub.challenge": request.args.get("hub.challenge")}, 200


@app.route("/strava/webhook", methods=["POST"])
def strava_webhook_event():
    event = request.json or {}
    threading.Thread(target=_handle_webhook_event, args=(event,), daemon=True).start()
    return "", 200


def _handle_webhook_event(event: dict):
    obj_type   = event.get("object_type")
    aspect     = event.get("aspect_type")
    obj_id     = event.get("object_id")
    owner_id   = event.get("owner_id")

    user = get_user_by_athlete_id(DB_PATH, owner_id)
    if not user:
        return

    uid = user["id"]

    if obj_type == "athlete" and aspect == "deauthorize":
        for key in ("access_token", "refresh_token", "token_expires_at", "athlete_id"):
            set_setting(DB_PATH, uid, "strava", key, "")
        return

    if obj_type != "activity":
        return

    if aspect == "delete":
        conn = _conn(DB_PATH)
        conn.execute("DELETE FROM activities WHERE id=? AND user_id=?", (obj_id, uid))
        conn.commit()
        conn.close()
        return

    if aspect in ("create", "update"):
        access_token  = get_setting(DB_PATH, uid, "strava", "access_token") or ""
        refresh_tok   = get_setting(DB_PATH, uid, "strava", "refresh_token") or ""
        expires_at    = float(get_setting(DB_PATH, uid, "strava", "token_expires_at") or 0)
        if not access_token:
            return

        def _on_refresh(new_access, new_refresh, new_expires):
            set_setting(DB_PATH, uid, "strava", "access_token",      new_access)
            set_setting(DB_PATH, uid, "strava", "refresh_token",     new_refresh)
            set_setting(DB_PATH, uid, "strava", "token_expires_at",  str(new_expires))

        client = StravaClient(
            access_token=access_token,
            client_id=STRAVA_CLIENT_ID,
            client_secret=STRAVA_CLIENT_SECRET,
            refresh_tok=refresh_tok,
            expires_at=expires_at,
            on_refresh=_on_refresh,
        )
        try:
            data = client.get_activity(obj_id)
        except Exception:
            return

        upsert_activity(DB_PATH, data, user_id=uid)

        cfg     = load_user_config(DB_PATH, uid, _base_cfg)
        out_dir = _base_cfg["map"].get("output_dir", "output")
        _render_and_track(obj_id, uid, cfg, out_dir)


@app.route("/strava/disconnect")
@login_required
def strava_disconnect():
    uid = int(current_user.id)
    for key in ("access_token", "refresh_token", "token_expires_at", "athlete_id"):
        set_setting(DB_PATH, uid, "strava", key, "")
    flash("Strava disconnected.", "success")
    return redirect(url_for("settings"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
