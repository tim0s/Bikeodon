"""
Bikeodon web frontend.

Run:  flask --app app run
  or: python app.py
"""

import json
import logging
import os
import re
import threading

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_login import (
    LoginManager, UserMixin, current_user,
    login_required, login_user, logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash

from config import DB_PATH, _base_cfg, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, SYNC_COOLDOWN_SECS
from database import (
    _conn, attach_source_file, clear_rendered, count_activities, create_user,
    find_overlapping_activity, get_activity, get_all_peak_powers,
    get_cp_history, get_daily_loads, get_followers, get_following,
    get_setting, get_site_setting, get_user_by_id,
    get_user_by_username, get_user_stats, get_zone_totals, get_zones, init_db,
    add_feed_item, add_local_reaction, remove_local_reaction, get_local_reactions,
    list_activities, load_user_config, mark_ap_posted,
    save_activity_file, set_activity_error, set_scheduled, set_setting, upsert_activity,
)
from activity_parser import parse_file, stream_from_file
from training_load import (
    aggregate_power_curve, compute_pmc, compute_wbal,
    fit_critical_power, weekly_load,
)
from tasks import (
    _collect_activity_images, _do_post_activity,
    _render_and_track, request_backfill, start_backfill_worker,
)
from strava_routes import _sync_cooldown_remaining
import admin_routes
import settings_routes
import strava_routes

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
_secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-key-change-me-in-production")
if _secret_key == "dev-key-change-me-in-production" and not app.debug:
    raise RuntimeError("FLASK_SECRET_KEY must be set in production (DEBUG=False)")
app.secret_key = _secret_key
app.config["DB_PATH"]              = DB_PATH
app.config["OUTPUT_DIR"]           = _base_cfg["map"].get("output_dir", "output")
app.config["PREFERRED_URL_SCHEME"] = "https"

init_db(DB_PATH)

from activitypub import bp as _ap_bp, start_delivery_worker
app.register_blueprint(_ap_bp)
start_delivery_worker(DB_PATH)
start_backfill_worker()

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

_SPORT_EMOJI = {
    "Ride":            ("🚴", "Ride"),
    "VirtualRide":     ("🚴💻", "Virtual Ride"),
    "Run":             ("🏃", "Run"),
    "VirtualRun":      ("🏃💻", "Virtual Run"),
    "Walk":            ("🚶", "Walk"),
    "Hike":            ("🥾", "Hike"),
    "Swim":            ("🏊", "Swim"),
    "Workout":         ("💪", "Workout"),
    "WeightTraining":  ("🏋️", "Weight Training"),
    "Yoga":            ("🧘", "Yoga"),
    "Rowing":          ("🚣", "Rowing"),
    "Kayaking":        ("🛶", "Kayaking"),
    "Skiing":          ("⛷️", "Skiing"),
    "Snowboard":       ("🏂", "Snowboard"),
    "Skateboard":      ("🛹", "Skateboard"),
    "Soccer":          ("⚽", "Soccer"),
    "Tennis":          ("🎾", "Tennis"),
    "Golf":            ("⛳", "Golf"),
    "MountainBikeRide": ("🚵", "MTB"),
    "GravelRide":      ("🚵", "Gravel"),
    "EBikeRide":       ("⚡🚴", "E-Bike"),
}

@app.template_filter("sport_emoji")
def sport_emoji_filter(sport_type):
    emoji, label = _SPORT_EMOJI.get(sport_type, ("🏅", sport_type or "?"))
    return emoji, label

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


# Register route modules (after login_manager so decorators have context)
admin_routes.register_routes(app)
settings_routes.register_routes(app)
strava_routes.register_routes(app)

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
        if not re.match(r'^[A-Za-z0-9_.-]{1,30}$', username):
            flash(
                "Username may only contain letters, numbers, underscores, hyphens, and dots, "
                "and must be 30 characters or fewer.",
                "error",
            )
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
        from urllib.parse import urlparse
        next_url = request.args.get("next") or ""
        if not next_url or urlparse(next_url).netloc:
            next_url = url_for("index")
        return redirect(next_url)
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

    total   = count_activities(DB_PATH, uid)
    n_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page    = min(page, n_pages)
    offset  = (page - 1) * PER_PAGE

    rows = list_activities(DB_PATH, user_id=uid, limit=PER_PAGE, offset=offset,
                           sort=sort, direction=dir_)
    activities = []
    for r in rows:
        activities.append({
            "id":            r["id"],
            "name":          r["name"] or "—",
            "sport_type":    r["sport_type"] or "",
            "date":          (lambda d: f"{d[8:10]}.{d[5:7]}.{d[2:4]}" if len(d) >= 10 else d)((r["start_date"] or "")[:10]),
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
    strava_connected = bool(get_setting(DB_PATH, uid, "strava", "access_token"))
    sync_remaining   = _sync_cooldown_remaining(uid) if strava_connected else 0
    sync_mins_left   = (sync_remaining + 59) // 60 if sync_remaining > 0 else 0
    return render_template("index.html", activities=activities,
                           strava_connected=strava_connected,
                           sync_available=(sync_remaining == 0),
                           sync_mins_left=sync_mins_left,
                           page=page, n_pages=n_pages, total=total,
                           sort=sort, dir=dir_)


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
        "date":       (lambda d: f"{d[8:10]}.{d[5:7]}.{d[2:4]}" if len(d) >= 10 else d)((row["start_date"] or "")[:10]),
        "distance":   f"{(row['distance'] or 0) / 1000:.1f} km" if row["distance"] else None,
        "elevation":  f"{row['total_elevation_gain'] or 0:.0f} m" if row["total_elevation_gain"] is not None else None,
        "moving_time": _fmt_time(row["moving_time"]),
        "avg_speed":  f"{row['average_speed'] * 3.6:.1f}" if row["average_speed"] else (
                      f"{row['distance'] / row['moving_time'] * 3.6:.1f}" if row["distance"] and row["moving_time"] else None),
        "max_speed":  f"{row['max_speed'] * 3.6:.1f}" if row["max_speed"] else None,
        "avg_hr":     f"{row['average_heartrate']:.0f}" if row["average_heartrate"] else None,
        "max_hr":     f"{row['max_heartrate']:.0f}" if row["max_heartrate"] else None,
        "avg_watts":  f"{row['average_watts']:.0f}" if row["average_watts"] else None,
        "max_watts":  f"{row['max_watts']:.0f}" if row["max_watts"] else None,
        "tss":        round(row["tss"])    if row["tss"]    is not None else None,
        "hr_tss":     round(row["hr_tss"]) if row["hr_tss"] is not None else None,
        "np_watts":   round(row["np_watts"]) if row["np_watts"] is not None else None,
        "strava_url":    row["strava_url"] or "",
        "post_url":      row["mastodon_post_url"] or "",
        "scheduled":     bool(row["scheduled_for_post"]),
        "render_error":  row["render_error"] or "",
        "post_error":    row["post_error"] or "",
        "ap_posted_at":  (row["ap_posted_at"] or "")[:10],
        "breakthroughs": json.loads(row["breakthroughs_json"])
                         if row["breakthroughs_json"] else [],
    }

    mastodon_configured = bool(get_setting(DB_PATH, uid, "mastodon", "token"))
    has_avg_watts   = bool(row["average_watts"])
    has_power_chart = os.path.exists(os.path.join(out_dir, f"{activity_id}_power.png"))

    wbal_json = None
    act_cp = act_w_prime = None
    _cp_v     = get_setting(DB_PATH, uid, "inference", "cp")
    _wprime_v = get_setting(DB_PATH, uid, "inference", "w_prime")
    if row["average_watts"] and _cp_v and _wprime_v:
        try:
            _source_file = row["source_file"]
            _stream = stream_from_file(_source_file) if _source_file else []
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
        set_scheduled(DB_PATH, activity_id, uid, False)
    else:
        if row["post_error"]:
            set_activity_error(DB_PATH, activity_id, uid, "post", None)
        set_scheduled(DB_PATH, activity_id, uid, True)
        threading.Thread(target=_do_post_activity, args=(activity_id, uid), daemon=True).start()

    return redirect(request.referrer or url_for("activity", activity_id=activity_id))


@app.route("/activity/<int:activity_id>/ap_post", methods=["POST"])
@login_required
def ap_post_activity(activity_id):
    uid = int(current_user.id)
    row = get_activity(DB_PATH, activity_id, user_id=uid)
    if not row:
        flash("Activity not found.", "error")
        return redirect(url_for("index"))

    if row["ap_posted_at"]:
        flash("Already posted to followers.", "info")
        return redirect(request.referrer or url_for("activity", activity_id=activity_id))

    user     = get_user_by_id(DB_PATH, uid)
    username = user["username"]
    followers = get_followers(DB_PATH, username)
    if not followers:
        flash("No followers to post to.", "info")
        return redirect(request.referrer or url_for("activity", activity_id=activity_id))

    from activitypub import _activity_row_to_ap, _deliver_activity, get_or_create_keypair
    actor_url  = url_for("activitypub.actor",  username=username, _external=True)
    outbox_url = url_for("activitypub.outbox", username=username, _external=True)

    cfg     = load_user_config(DB_PATH, uid, _base_cfg)
    out_dir = _base_cfg["map"].get("output_dir", "output")
    image_paths = _collect_activity_images(activity_id, uid, cfg, out_dir, row)
    image_urls  = [
        url_for("output_file", filename=os.path.basename(p), _external=True)
        for p in image_paths
    ]

    create_activity = _activity_row_to_ap(row, actor_url, outbox_url, image_urls=image_urls)
    # Stable unique Create ID — avoids Mastodon deduplication on retries without allowing duplicates
    create_activity["id"] = create_activity["object"]["id"] + "/create"
    _, priv_pem = get_or_create_keypair(DB_PATH, uid)
    key_id = f"{actor_url}#main-key"

    for follower in followers:
        inbox_url = follower.get("inbox_url")
        if inbox_url:
            _deliver_activity(inbox_url, create_activity, key_id, DB_PATH)

    mark_ap_posted(DB_PATH, activity_id, uid)

    # Add to own feed so the post appears in the Bikeodon home feed
    note        = create_activity["object"]
    user_avatar = dict(user).get("avatar_filename")
    avatar_url  = url_for("user_avatar", username=username, _external=True) if user_avatar else None
    dist_m  = dict(row).get("distance") or 0
    elev_m  = dict(row).get("total_elevation_gain") or 0
    name    = dict(row).get("name", "Activity")
    content = f"<p>{name}</p>"
    if dist_m:
        content += f"<p>📍 {dist_m/1000:.1f} km  🏔 {elev_m:.0f} m</p>"
    attachments = [{"url": u, "mediaType": "image/png", "type": "Document"} for u in image_urls]
    add_feed_item(
        DB_PATH, username, actor_url,
        dict(user).get("display_name") or username,
        avatar_url,
        note["id"],
        url_for("activity", activity_id=activity_id, _external=True),
        content,
        note.get("published"),
        json.dumps(attachments) if attachments else None,
    )

    flash(f"Activity queued for delivery to {len(followers)} follower(s).", "success")
    return redirect(request.referrer or url_for("activity", activity_id=activity_id))


# ---------------------------------------------------------------------------
# File serving
# ---------------------------------------------------------------------------

@app.route("/screenshots/<path:filename>")
def screenshot(filename):
    docs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "screenshots")
    return send_from_directory(docs_dir, filename)


@app.route("/output/<path:filename>")
def output_file(filename):
    out_dir = os.path.abspath(_base_cfg["map"].get("output_dir", "output"))
    response = send_from_directory(out_dir, filename)
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/users/<username>/avatar")
def user_avatar(username):
    """Serve a user's avatar image publicly (needed for ActivityPub icon URL)."""
    user   = get_user_by_username(DB_PATH, username)
    avatar = dict(user).get("avatar_filename") if user else None
    if not avatar:
        return app.send_static_file("default_avatar.png")
    avatars_dir = os.path.abspath(os.path.join(
        _base_cfg["map"].get("output_dir", "output"), "avatars"
    ))
    return send_from_directory(avatars_dir, avatar)


# ---------------------------------------------------------------------------
# Profile + Fediverse actions
# ---------------------------------------------------------------------------

@app.route("/me/profile", methods=["POST"])
@login_required
def save_profile():
    uid  = int(current_user.id)

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

    conn = _conn(DB_PATH)
    try:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE users SET {set_clause} WHERE id=?",
            (*updates.values(), uid),
        )
        conn.commit()
    finally:
        conn.close()

    from activitypub import send_profile_update
    user = get_user_by_id(DB_PATH, uid)
    send_profile_update(user["username"], user, DB_PATH)

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


@app.route("/ap/unfollow", methods=["POST"])
@login_required
def ap_unfollow():
    from activitypub import send_unfollow
    actor_url = request.form.get("actor_url", "").strip()
    if not actor_url:
        abort(400)
    uid  = int(current_user.id)
    user = get_user_by_id(DB_PATH, uid)
    send_unfollow(current_user.username, user, actor_url, DB_PATH)
    return redirect(request.referrer or url_for("me", tab="following"))


# ---------------------------------------------------------------------------
# Home feed
# ---------------------------------------------------------------------------

@app.route("/feed")
@login_required
def feed():
    from database import get_feed_items, count_feed_items, get_reaction_counts
    page     = max(1, request.args.get("page", 1, type=int))
    per_page = 20
    offset   = (page - 1) * per_page
    items    = get_feed_items(DB_PATH, current_user.username, limit=per_page, offset=offset)
    total    = count_feed_items(DB_PATH, current_user.username)

    actor_url = url_for("activitypub.actor", username=current_user.username, _external=True)
    note_prefix = f"{actor_url}/activities/"

    object_ids = [dict(i).get("object_id", "") for i in items]
    local_rxns  = get_local_reactions(DB_PATH, current_user.username, object_ids)

    parsed_items = []
    for item in items:
        row = dict(item)
        try:
            row["attachments"] = json.loads(row.get("attachments_json") or "[]")
        except Exception:
            row["attachments"] = []
        object_id = row.get("object_id", "")
        if object_id.startswith(note_prefix):
            try:
                activity_id = int(object_id[len(note_prefix):].split("/")[0])
                row["reactions"] = get_reaction_counts(DB_PATH, activity_id)
            except (ValueError, IndexError):
                row["reactions"] = None
        else:
            row["reactions"] = None
        row["my_reactions"] = local_rxns.get(object_id, {"like": False, "boost": False})
        parsed_items.append(row)
    return render_template(
        "feed.html",
        items=parsed_items,
        page=page,
        total=total,
        per_page=per_page,
        has_prev=page > 1,
        has_next=(offset + per_page) < total,
    )


@app.route("/feed/react", methods=["POST"])
@login_required
def feed_react():
    from activitypub import send_like, send_unlike, send_boost, send_unboost
    object_id     = request.form.get("object_id", "").strip()
    actor_url     = request.form.get("actor_url", "").strip()
    reaction_type = request.form.get("type", "")
    if not object_id or not actor_url or reaction_type not in ("like", "boost"):
        abort(400)

    uid  = int(current_user.id)
    user = get_user_by_id(DB_PATH, uid)
    existing = get_local_reactions(DB_PATH, current_user.username, [object_id])
    already  = existing.get(object_id, {}).get(reaction_type, False)

    if already:
        remove_local_reaction(DB_PATH, current_user.username, object_id, reaction_type)
        if reaction_type == "like":
            send_unlike(current_user.username, user, object_id, actor_url, DB_PATH)
        else:
            send_unboost(current_user.username, user, object_id, actor_url, DB_PATH)
    else:
        add_local_reaction(DB_PATH, current_user.username, object_id, reaction_type)
        if reaction_type == "like":
            send_like(current_user.username, user, object_id, actor_url, DB_PATH)
        else:
            send_boost(current_user.username, user, object_id, actor_url, DB_PATH)

    return redirect(request.referrer or url_for("feed"))


@app.route("/feed/reply", methods=["POST"])
@login_required
def feed_reply():
    from activitypub import send_reply
    object_id = request.form.get("object_id", "").strip()
    actor_url = request.form.get("actor_url", "").strip()
    content   = request.form.get("content", "").strip()
    if not object_id or not actor_url or not content:
        abort(400)
    user = get_user_by_id(DB_PATH, int(current_user.id))
    send_reply(current_user.username, user, object_id, actor_url, content, DB_PATH)
    return redirect(request.referrer or url_for("feed"))


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
            if get_activity(DB_PATH, act["id"], user_id=uid):
                skipped += 1
                continue

            overlap_row = find_overlapping_activity(
                DB_PATH, uid,
                act.get("start_date"),
                act.get("elapsed_time") or act.get("moving_time"),
            )
            if overlap_row:
                files_dir = os.path.join(out_dir, "activity_files")
                path, sha256 = save_activity_file(
                    files_dir, overlap_row["id"], uid, content, f.filename
                )
                attach_source_file(DB_PATH, overlap_row["id"], uid, path, sha256)
                _render_and_track(overlap_row["id"], uid, cfg, out_dir)
                enriched += 1
                continue

            files_dir = os.path.join(out_dir, "activity_files")
            act["source_file"], act["source_file_sha256"] = \
                save_activity_file(files_dir, act["id"], uid, content, f.filename)
            act["source_file_type"] = "upload"
            upsert_activity(DB_PATH, act, user_id=uid, source="upload")
            _render_and_track(act["id"], uid, cfg, out_dir)
            imported += 1

    if imported or enriched:
        request_backfill(uid)

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
# You / user dashboard
# ---------------------------------------------------------------------------

@app.route("/me")
@login_required
def me():
    uid = int(current_user.id)
    tab = request.args.get("tab", "overview")

    stats = get_user_stats(DB_PATH, uid)
    cfg   = load_user_config(DB_PATH, uid, _base_cfg)

    daily_loads = get_daily_loads(DB_PATH, uid)
    pmc_rows    = compute_pmc(daily_loads, days=180)
    weekly_rows = weekly_load(daily_loads, weeks=26)

    pmc_latest = pmc_rows[-1] if pmc_rows else {"ctl": 0, "atl": 0, "tsb": 0}

    ftp         = cfg["charts"]["power"]["ftp"]
    body_weight = float(get_setting(DB_PATH, uid, "training", "body_weight_kg") or 0) or None
    all_peaks    = get_all_peak_powers(DB_PATH, uid)
    recent_peaks = get_all_peak_powers(DB_PATH, uid, days=90)
    curve_all    = aggregate_power_curve(all_peaks)
    curve_recent = aggregate_power_curve(recent_peaks)

    cp, w_prime = fit_critical_power(curve_all)
    if cp:
        set_setting(DB_PATH, uid, "inference", "cp",      str(cp))
        set_setting(DB_PATH, uid, "inference", "w_prime", str(w_prime))

    if body_weight:
        wpk_all    = {k: round(v / body_weight, 2) for k, v in curve_all.items()}
        wpk_recent = {k: round(v / body_weight, 2) for k, v in curve_recent.items()}
    else:
        wpk_all = wpk_recent = {}

    hr_zones    = get_zones(DB_PATH, uid, "hr")
    power_zones = get_zones(DB_PATH, uid, "power")
    hr_max      = cfg["charts"]["heart_rate"]["max_hr"]
    hr_rest     = float(get_setting(DB_PATH, uid, "training", "hr_rest") or 0) or None

    hr_totals, power_totals = get_zone_totals(DB_PATH, uid)

    def _zone_chart_data(zones, totals):
        data = [{"name": z["name"], "color": z["color"],
                 "secs": totals.get(z["name"], 0.0)} for z in zones]
        return json.dumps(data)

    hr_zone_chart_json    = _zone_chart_data(hr_zones,    hr_totals)
    power_zone_chart_json = _zone_chart_data(power_zones, power_totals)

    cp_history = get_cp_history(DB_PATH, uid)

    profile_row = get_user_by_id(DB_PATH, uid)

    followers = get_followers(DB_PATH, current_user.username)
    following = get_following(DB_PATH, current_user.username)
    following_urls = {f["actor_url"] for f in following}

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
        cp_history_json=json.dumps(cp_history),
        followers=followers,
        following=following,
        following_urls=following_urls,
        search_result=search_result,
        search_error=search_error,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
