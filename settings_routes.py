from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from config import DB_PATH, _base_cfg, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET
from database import (
    apply_zone_preset, get_athlete_param, get_setting, get_zones,
    HR_ZONE_PRESETS, POWER_ZONE_PRESETS,
    load_user_config, set_athlete_param, set_setting, _conn,
)

_STAT_FIELDS = [
    "distance", "elevation_gain", "moving_time", "elapsed_time",
    "average_speed", "max_speed", "average_heartrate", "max_heartrate",
    "average_watts", "max_watts",
]


def register_routes(app):

    @app.route("/settings")
    @login_required
    def settings():
        uid     = int(current_user.id)
        section = request.args.get("section", "strava")
        cfg     = load_user_config(DB_PATH, uid, _base_cfg)
        hr_zones    = get_zones(DB_PATH, uid, "hr")
        power_zones = get_zones(DB_PATH, uid, "power")
        strava_connected  = bool(get_setting(DB_PATH, uid, "strava", "access_token"))
        strava_configured = bool(STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET)
        active_fields = [
            f.strip()
            for f in (get_setting(DB_PATH, uid, "stats", "fields") or "").split(",")
            if f.strip()
        ]
        inferred_ftp    = get_athlete_param(DB_PATH, uid, "ftp")
        inferred_max_hr = get_athlete_param(DB_PATH, uid, "max_hr")
        _hr_max_v = cfg["charts"]["heart_rate"]["max_hr"] or inferred_max_hr or None
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
            # Mirror to athlete_params for time-series tracking
            if val:
                try:
                    param = "max_hr" if key == "max_hr" else "ftp"
                    set_athlete_param(DB_PATH, uid, param, float(val), source="manual")
                except ValueError:
                    pass
        flash("Chart settings saved.", "success")
        return redirect(url_for("settings", section="charts"))

    @app.route("/settings/training", methods=["POST"])
    @login_required
    def save_training():
        uid = int(current_user.id)
        for key in ("body_weight_kg", "hr_rest", "lthr"):
            val = request.form.get(key, "").strip()
            set_setting(DB_PATH, uid, "training", key, val)
        # Mirror to athlete_params for time-series tracking
        _param_map = {"body_weight_kg": "weight_kg", "hr_rest": "rest_hr"}
        for form_key, param in _param_map.items():
            val = request.form.get(form_key, "").strip()
            if val:
                try:
                    set_athlete_param(DB_PATH, uid, param, float(val), source="manual")
                except ValueError:
                    pass
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
