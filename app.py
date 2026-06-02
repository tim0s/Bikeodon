"""
Bikeodon web frontend.

Run:  flask --app app run
  or: python app.py
"""

import os

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

from database import (
    _conn, create_user, get_activity, get_admin_stats, get_setting, get_site_setting,
    get_user_by_id, get_user_by_username, get_user_stats, get_zones, init_db,
    list_activities, list_settings, load_user_config, set_scheduled, set_setting,
)
from strava import StravaClient, exchange_code, strava_auth_url

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
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-key-change-me-in-production")

init_db(DB_PATH)

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

@app.route("/")
@login_required
def index():
    rows = list_activities(DB_PATH, user_id=int(current_user.id))
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
        })
    strava_connected = bool(get_setting(DB_PATH, int(current_user.id), "strava", "access_token"))
    return render_template("index.html", activities=activities, strava_connected=strava_connected)


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
        "strava_url": row["strava_url"] or "",
        "post_url":   row["mastodon_post_url"] or "",
        "scheduled":  bool(row["scheduled_for_post"]),
    }

    mastodon_configured = bool(get_setting(DB_PATH, uid, "mastodon", "token"))
    return render_template("activity.html", activity=act, map_url=map_url,
                           chart_urls=chart_urls, mastodon_configured=mastodon_configured)


@app.route("/activity/<int:activity_id>/schedule", methods=["POST"])
@login_required
def schedule_activity(activity_id):
    uid = int(current_user.id)
    row = get_activity(DB_PATH, activity_id, user_id=uid)
    if not row:
        flash("Activity not found.", "error")
        return redirect(url_for("index"))
    new_state = not bool(row["scheduled_for_post"])
    set_scheduled(DB_PATH, activity_id, uid, new_state)
    return redirect(request.referrer or url_for("activity", activity_id=activity_id))


@app.route("/output/<path:filename>")
@login_required
def output_file(filename):
    out_dir = os.path.abspath(_base_cfg["map"].get("output_dir", "output"))
    return send_from_directory(out_dir, filename)


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------

@app.route("/admin")
@login_required
@admin_required
def admin():
    stats = get_admin_stats(DB_PATH)
    interval = _base_cfg.get("daemon", {}).get("interval_minutes", 15)
    return render_template("admin.html", stats=stats, interval_minutes=interval)


# ---------------------------------------------------------------------------
# You / user dashboard
# ---------------------------------------------------------------------------

@app.route("/me")
@login_required
def me():
    uid   = int(current_user.id)
    stats = get_user_stats(DB_PATH, uid)
    return render_template("me.html", stats=stats, username=current_user.username)


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
    uid = int(current_user.id)
    cfg = load_user_config(DB_PATH, uid, _base_cfg)
    hr_zones    = get_zones(DB_PATH, uid, "hr")
    power_zones = get_zones(DB_PATH, uid, "power")
    strava_connected   = bool(get_setting(DB_PATH, uid, "strava", "access_token"))
    strava_configured  = bool(STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET)
    active_fields = [
        f.strip()
        for f in (get_setting(DB_PATH, uid, "stats", "fields") or "").split(",")
        if f.strip()
    ]
    return render_template(
        "settings.html",
        cfg=cfg,
        hr_zones=hr_zones,
        power_zones=power_zones,
        stat_fields=_STAT_FIELDS,
        active_fields=active_fields,
        strava_connected=strava_connected,
        strava_configured=strava_configured,
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
    return redirect(url_for("settings") + "#mastodon")


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
    for key in ("start_marker_enabled", "end_marker_enabled"):
        set_setting(DB_PATH, uid, "map", key, "true" if request.form.get(key) else "false")
    flash("Map settings saved.", "success")
    return redirect(url_for("settings") + "#map")


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
    return redirect(url_for("settings") + "#charts")


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
    return redirect(url_for("settings") + "#stats")


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
    return render_template("zones.html", hr_zones=hr_zones, power_zones=power_zones,
                           active_type=zone_type)


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
