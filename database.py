import json
import sqlite3
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Default settings seeded for every new user
# ---------------------------------------------------------------------------

_DEFAULT_SETTINGS = [
    ("mastodon",       "instance",                "https://mastodon.social"),
    ("mastodon",       "token",                   ""),
    ("mastodon",       "visibility",              "public"),
    ("mastodon",       "handle",                  ""),
    ("mastodon",       "post_template",
        "{name} 🚴\n📍 {distance_km:.1f} km  🏔 {elevation_m:.0f} m  ⏱ {moving_time}"
        "\n\n#cycling #strava\n\nPosted via Bikeodon → https://bikeodon.org"
        " (https://github.com/tim0s/Bikeodon)"),
    ("map",            "width",                   "1200"),
    ("map",            "height",                  "675"),
    ("map",            "zoom_offset",             "0"),
    ("map",            "max_tiles",               "100"),
    ("map",            "tiles_url",               "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
    ("map",            "tiles_user_agent",        "Bikeodon/0.1 (https://github.com/tim0s/Bikeodon)"),
    ("map",            "padding_top",             "0.05"),
    ("map",            "padding_bottom",          "0.12"),
    ("map",            "padding_left",            "0.05"),
    ("map",            "padding_right",           "0.05"),
    ("map",            "route_color",             "#FC4C02"),
    ("map",            "route_width",             "4"),
    ("map",            "route_opacity",           "0.9"),
    ("map",            "route_outline_color",     "#000000"),
    ("map",            "route_outline_width",     "1"),
    ("map",            "route_antialias_scale",   "2"),
    ("map",            "start_marker_enabled",    "true"),
    ("map",            "start_marker_color",      "#22CC44"),
    ("map",            "start_marker_radius",     "8"),
    ("map",            "start_marker_outline_color",  "#ffffff"),
    ("map",            "start_marker_outline_width",  "2"),
    ("map",            "end_marker_enabled",      "true"),
    ("map",            "end_marker_color",        "#CC2244"),
    ("map",            "end_marker_radius",       "8"),
    ("map",            "end_marker_outline_color",    "#ffffff"),
    ("map",            "end_marker_outline_width",    "2"),
    ("charts",         "style_background_color",  "#16161a"),
    ("charts",         "style_text_color",        "#dddddd"),
    ("charts",         "style_grid_color",        "#2e2e3a"),
    ("charts",         "style_line_color",        "#FC4C02"),
    ("charts",         "style_power_line_color",  "#4fc3f7"),
    ("charts",         "hr_enabled",              "true"),
    ("charts",         "power_enabled",           "true"),
    ("training",       "body_weight_kg",           ""),
    ("training",       "hr_rest",                 ""),
    ("training",       "lthr",                    ""),
    ("map",            "watopia_enabled",          "true"),
    ("stats",          "fields",                  "distance,elevation_gain"),
    ("stats_overlay",  "enabled",                 "true"),
    ("stats_overlay",  "background_color",        "#000000"),
    ("stats_overlay",  "background_opacity",      "0.55"),
    ("stats_overlay",  "padding",                 "24"),
    ("stats_overlay",  "gap",                     "36"),
    ("stats_overlay",  "font_size",               "48"),
    ("stats_overlay",  "font_color",              "#ffffff"),
    ("stats_overlay",  "icon_size",               "48"),
]

HR_ZONE_PRESETS = {
    "3zone": [
        (0, "Z1 Endurance",  75,  "#70ad47"),
        (1, "Z2 Tempo",      90,  "#ffc000"),
        (2, "Z3 Hard",       100, "#d32f2f"),
    ],
    "5zone": [
        (0, "Z1 Recovery",   60,  "#5b9bd5"),
        (1, "Z2 Endurance",  70,  "#70ad47"),
        (2, "Z3 Tempo",      80,  "#ffc000"),
        (3, "Z4 Threshold",  90,  "#ff7043"),
        (4, "Z5 VO2 Max",    100, "#d32f2f"),
    ],
}

POWER_ZONE_PRESETS = {
    "3zone": [
        (0, "Z1 Endurance",  75,  "#70ad47"),
        (1, "Z2 Tempo",      105, "#ffc000"),
        (2, "Z3 Hard",       999, "#d32f2f"),
    ],
    "5zone": [
        (0, "Z1 Recovery",   55,  "#5b9bd5"),
        (1, "Z2 Endurance",  75,  "#70ad47"),
        (2, "Z3 Tempo",      90,  "#ffc000"),
        (3, "Z4 Threshold",  105, "#ff7043"),
        (4, "Z5 VO2 Max",    999, "#d32f2f"),
    ],
    "7zone": [
        (0, "Z1 Recovery",       55,  "#5b9bd5"),
        (1, "Z2 Endurance",      75,  "#70ad47"),
        (2, "Z3 Tempo",          90,  "#ffc000"),
        (3, "Z4 Threshold",      105, "#ff7043"),
        (4, "Z5 VO2 Max",        120, "#d32f2f"),
        (5, "Z6 Anaerobic",      150, "#9c27b0"),
        (6, "Z7 Neuromuscular",  999, "#424242"),
    ],
}

_DEFAULT_HR_ZONES    = HR_ZONE_PRESETS["5zone"]
_DEFAULT_POWER_ZONES = POWER_ZONE_PRESETS["7zone"]


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db(db_path):
    conn = _conn(db_path)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE,
            password_hash TEXT,
            created_at    TEXT NOT NULL
        )
    """)
    for col, typedef in [
        ("username",      "TEXT"),
        ("password_hash", "TEXT"),
        ("is_admin",      "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS daemon_runs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at        TEXT NOT NULL,
            finished_at       TEXT NOT NULL,
            duration_secs     REAL NOT NULL,
            activities_synced INTEGER NOT NULL DEFAULT 0,
            error             TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            user_id  INTEGER NOT NULL REFERENCES users(id),
            area     TEXT NOT NULL,
            key      TEXT NOT NULL,
            value    TEXT,
            PRIMARY KEY (user_id, area, key)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS zones (
            user_id     INTEGER NOT NULL REFERENCES users(id),
            type        TEXT NOT NULL,
            zone_index  INTEGER NOT NULL,
            name        TEXT NOT NULL,
            max_pct     REAL NOT NULL,
            color       TEXT NOT NULL,
            PRIMARY KEY (user_id, type, zone_index)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS activities (
            id                   INTEGER NOT NULL,
            user_id              INTEGER NOT NULL REFERENCES users(id),
            name                 TEXT,
            sport_type           TEXT,
            start_date           TEXT,
            distance             REAL,
            moving_time          INTEGER,
            elapsed_time         INTEGER,
            total_elevation_gain REAL,
            max_speed            REAL,
            average_heartrate    REAL,
            max_heartrate        REAL,
            average_watts        REAL,
            max_watts            REAL,
            start_lat            REAL,
            start_lon            REAL,
            points_json          TEXT,
            fetched_at           TEXT,
            strava_url           TEXT,
            posted_at            TEXT,
            mastodon_post_url    TEXT,
            PRIMARY KEY (id, user_id)
        )
    """)

    # Migrate activities table for databases created before current schema
    for col, typedef in [
        ("user_id",              "INTEGER NOT NULL DEFAULT 1"),
        ("strava_url",           "TEXT"),
        ("posted_at",            "TEXT"),
        ("mastodon_post_url",    "TEXT"),
        ("scheduled_for_post",   "INTEGER NOT NULL DEFAULT 0"),
        ("map_rendered_at",      "TEXT"),
        ("charts_rendered_at",   "TEXT"),
        ("source",               "TEXT NOT NULL DEFAULT 'strava'"),
        ("render_error",         "TEXT"),
        ("post_error",           "TEXT"),
        ("tss",                  "REAL"),
        ("np_watts",             "REAL"),
        ("trimp",                "REAL"),
        ("peak_power_json",      "TEXT"),
        ("hr_zone_secs_json",    "TEXT"),
        ("power_zone_secs_json", "TEXT"),
        ("metrics_computed_at",  "TEXT"),
        ("hr_tss",               "REAL"),
        ("breakthroughs_json",   "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE activities ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS site_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            job_type    TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'running',
            details     TEXT
        )
    """)

    conn.commit()

    # Seed default user if none exists
    if not conn.execute("SELECT 1 FROM users").fetchone():
        conn.execute(
            "INSERT INTO users (created_at) VALUES (?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        _seed_defaults(conn, user_id=1)
        conn.commit()

    conn.close()


def seed_user_defaults(conn, user_id: int):
    _seed_defaults(conn, user_id)


def _seed_defaults(conn, user_id: int):
    for area, key, value in _DEFAULT_SETTINGS:
        conn.execute(
            "INSERT OR IGNORE INTO settings (user_id, area, key, value) VALUES (?,?,?,?)",
            (user_id, area, key, value),
        )
    for zone_index, name, max_pct, color in _DEFAULT_HR_ZONES:
        conn.execute(
            "INSERT OR IGNORE INTO zones (user_id, type, zone_index, name, max_pct, color)"
            " VALUES (?,?,?,?,?,?)",
            (user_id, "hr", zone_index, name, max_pct, color),
        )
    for zone_index, name, max_pct, color in _DEFAULT_POWER_ZONES:
        conn.execute(
            "INSERT OR IGNORE INTO zones (user_id, type, zone_index, name, max_pct, color)"
            " VALUES (?,?,?,?,?,?)",
            (user_id, "power", zone_index, name, max_pct, color),
        )


# ---------------------------------------------------------------------------
# Settings CRUD
# ---------------------------------------------------------------------------

def get_setting(db_path, user_id: int, area: str, key: str) -> str | None:
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT value FROM settings WHERE user_id=? AND area=? AND key=?",
        (user_id, area, key),
    ).fetchone()
    conn.close()
    return row["value"] if row else None


def set_setting(db_path, user_id: int, area: str, key: str, value: str):
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO settings (user_id, area, key, value) VALUES (?,?,?,?)"
        " ON CONFLICT(user_id, area, key) DO UPDATE SET value=excluded.value",
        (user_id, area, key, value),
    )
    conn.commit()
    conn.close()


def list_settings(db_path, user_id: int) -> list[sqlite3.Row]:
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT area, key, value FROM settings WHERE user_id=? ORDER BY area, key",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def get_site_setting(db_path, key: str) -> str | None:
    conn = _conn(db_path)
    row = conn.execute("SELECT value FROM site_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def set_site_setting(db_path, key: str, value: str):
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO site_settings (key, value) VALUES (?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_zones(db_path, user_id: int, zone_type: str) -> list[dict]:
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT name, max_pct, color FROM zones"
        " WHERE user_id=? AND type=? ORDER BY zone_index",
        (user_id, zone_type),
    ).fetchall()
    conn.close()
    return [{"name": r["name"], "max_pct": r["max_pct"], "color": r["color"]} for r in rows]


def apply_zone_preset(db_path, user_id: int, zone_type: str, preset_key: str):
    presets = HR_ZONE_PRESETS if zone_type == "hr" else POWER_ZONE_PRESETS
    rows = presets.get(preset_key)
    if not rows:
        raise ValueError(f"Unknown preset {preset_key!r} for zone_type {zone_type!r}")
    conn = _conn(db_path)
    conn.execute("DELETE FROM zones WHERE user_id=? AND type=?", (user_id, zone_type))
    for idx, name, max_pct, color in rows:
        conn.execute(
            "INSERT INTO zones (user_id, type, zone_index, name, max_pct, color) VALUES (?,?,?,?,?,?)",
            (user_id, zone_type, idx, name, float(max_pct), color),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Build cfg dict from DB (same structure expected by map_renderer / charts)
# ---------------------------------------------------------------------------

def load_user_config(db_path: str, user_id: int, base_cfg: dict) -> dict:
    """
    Read all settings and zones for a user and return a cfg dict in the same
    structure as the old config.yaml, merged with base_cfg (which provides
    server-level paths: output_dir, tiles.cache_dir, daemon).
    """
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT area, key, value FROM settings WHERE user_id=?", (user_id,)
    ).fetchall()
    hr_zones    = conn.execute(
        "SELECT name, max_pct, color FROM zones WHERE user_id=? AND type='hr' ORDER BY zone_index",
        (user_id,),
    ).fetchall()
    power_zones = conn.execute(
        "SELECT name, max_pct, color FROM zones WHERE user_id=? AND type='power' ORDER BY zone_index",
        (user_id,),
    ).fetchall()
    conn.close()

    s = {(r["area"], r["key"]): r["value"] for r in rows}

    def txt(area, key, default=""):
        return s.get((area, key), default) or default

    def num(area, key, default, cast=int):
        v = s.get((area, key))
        try:
            return cast(v) if v is not None else default
        except (ValueError, TypeError):
            return default

    def flag(area, key, default=True):
        v = s.get((area, key))
        if v is None:
            return default
        return v.strip().lower() in ("true", "1", "yes")

    def zones(rows):
        return [{"name": r["name"], "max_pct": r["max_pct"], "color": r["color"]} for r in rows]

    base_map = base_cfg.get("map", {})

    cfg = {
        "database": base_cfg.get("database", {}),
        "daemon":   base_cfg.get("daemon", {}),
        "strava": {},
        "mastodon": {
            "instance":      txt("mastodon", "instance",   "https://mastodon.social"),
            "token":         txt("mastodon", "token"),
            "visibility":    txt("mastodon", "visibility", "public"),
            "post_template": txt("mastodon", "post_template", "{name}\n#cycling"),
            "handle":        txt("mastodon", "handle"),
        },
        "map": {
            "output_dir":  base_map.get("output_dir", "output"),
            "width":       num("map", "width",       1200),
            "height":      num("map", "height",      675),
            "zoom_offset": num("map", "zoom_offset", -1),
            "max_tiles":   num("map", "max_tiles",   100),
            "padding": {
                "top":    num("map", "padding_top",    0.06, float),
                "bottom": num("map", "padding_bottom", 0.28, float),
                "left":   num("map", "padding_left",   0.06, float),
                "right":  num("map", "padding_right",  0.06, float),
            },
            "tiles": {
                "url":        txt("map", "tiles_url", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
                "user_agent": txt("map", "tiles_user_agent", "Bikeodon/0.1"),
                "size":       256,
                "cache_dir":  base_map.get("tiles", {}).get("cache_dir", ".tile_cache"),
            },
            "route": {
                "color":           txt("map", "route_color",           "#FC4C02"),
                "width":           num("map", "route_width",           4),
                "opacity":         num("map", "route_opacity",         0.9, float),
                "outline_color":   txt("map", "route_outline_color",   "#000000"),
                "outline_width":   num("map", "route_outline_width",   1),
                "antialias_scale": num("map", "route_antialias_scale", 2),
            },
            "start_marker": {
                "enabled":       flag("map", "start_marker_enabled", True),
                "color":         txt("map", "start_marker_color",         "#22CC44"),
                "radius":        num("map", "start_marker_radius",        8),
                "outline_color": txt("map", "start_marker_outline_color", "#ffffff"),
                "outline_width": num("map", "start_marker_outline_width", 2),
            },
            "end_marker": {
                "enabled":       flag("map", "end_marker_enabled", True),
                "color":         txt("map", "end_marker_color",         "#CC2244"),
                "radius":        num("map", "end_marker_radius",        8),
                "outline_color": txt("map", "end_marker_outline_color", "#ffffff"),
                "outline_width": num("map", "end_marker_outline_width", 2),
            },
            "watopia_enabled": flag("map", "watopia_enabled", True),
        },
        "charts": {
            "style": {
                "background_color": txt("charts", "style_background_color", "#16161a"),
                "text_color":       txt("charts", "style_text_color",       "#dddddd"),
                "grid_color":       txt("charts", "style_grid_color",       "#2e2e3a"),
                "line_color":       txt("charts", "style_line_color",       "#FC4C02"),
                "power_line_color": txt("charts", "style_power_line_color", "#4fc3f7"),
            },
            "heart_rate": {
                "enabled": flag("charts", "hr_enabled", True),
                "max_hr":  num("charts", "max_hr", None, float) if s.get(("charts", "max_hr")) else None,
                "zones":   zones(hr_zones),
            },
            "power": {
                "enabled": flag("charts", "power_enabled", True),
                "ftp":     num("charts", "ftp", None, float) if s.get(("charts", "ftp")) else None,
                "zones":   zones(power_zones),
            },
        },
        "user": {
            "mastodon_handle": txt("mastodon", "handle"),
            "stats": {
                "fields": [f.strip() for f in txt("stats", "fields", "distance,elevation_gain").split(",") if f.strip()],
            },
        },
        "training": {
            "body_weight_kg": txt("training", "body_weight_kg"),
            "hr_rest":        txt("training", "hr_rest"),
        },
        "stats_overlay": {
            "enabled":            flag("stats_overlay", "enabled", True),
            "background_color":   txt("stats_overlay", "background_color",   "#000000"),
            "background_opacity": num("stats_overlay", "background_opacity", 0.55, float),
            "padding":            num("stats_overlay", "padding",            24),
            "gap":                num("stats_overlay", "gap",                36),
            "font": {
                "size":  num("stats_overlay", "font_size",  48),
                "color": txt("stats_overlay", "font_color", "#ffffff"),
            },
            "icon": {
                "size": num("stats_overlay", "icon_size", 48),
                "activity_icons": {
                    "Ride": "🚴", "VirtualRide": "🚴",
                    "Run": "🏃", "VirtualRun": "🏃",
                    "Walk": "🚶", "Hike": "🥾",
                    "Swim": "🏊", "WeightTraining": "🏋️",
                    "Yoga": "🧘", "default": "🏅",
                },
            },
        },
    }
    return cfg


# ---------------------------------------------------------------------------
# Activities CRUD
# ---------------------------------------------------------------------------

def mark_rendered(db_path, activity_id: int, user_id: int, map: bool = False, charts: bool = False):
    now = datetime.now(timezone.utc).isoformat()
    fields = []
    if map:
        fields.append(f"map_rendered_at='{now}'")
    if charts:
        fields.append(f"charts_rendered_at='{now}'")
    if not fields:
        return
    conn = _conn(db_path)
    conn.execute(
        f"UPDATE activities SET {', '.join(fields)} WHERE id=? AND user_id=?",
        (activity_id, user_id),
    )
    conn.commit()
    conn.close()


def clear_rendered(db_path, activity_id: int, user_id: int, map: bool = True, charts: bool = True):
    fields = []
    if map:
        fields.append("map_rendered_at=NULL")
    if charts:
        fields.append("charts_rendered_at=NULL")
    if not fields:
        return
    conn = _conn(db_path)
    conn.execute(
        f"UPDATE activities SET {', '.join(fields)} WHERE id=? AND user_id=?",
        (activity_id, user_id),
    )
    conn.commit()
    conn.close()


def get_unrendered(db_path, user_id: int) -> list:
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT * FROM activities WHERE user_id=?"
        " AND (map_rendered_at IS NULL OR charts_rendered_at IS NULL)"
        " ORDER BY start_date DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def upsert_activity(db_path, data: dict, user_id: int, source: str = "strava"):
    conn = _conn(db_path)
    existing = conn.execute(
        "SELECT posted_at, mastodon_post_url, scheduled_for_post,"
        " map_rendered_at, charts_rendered_at"
        " FROM activities WHERE id=? AND user_id=?",
        (data["id"], user_id),
    ).fetchone()
    posted_at          = existing["posted_at"]          if existing else None
    mastodon_post_url  = existing["mastodon_post_url"]  if existing else None
    scheduled_for_post = existing["scheduled_for_post"] if existing else 0
    map_rendered_at    = existing["map_rendered_at"]    if existing else None
    charts_rendered_at = existing["charts_rendered_at"] if existing else None

    conn.execute("""
        INSERT OR REPLACE INTO activities
        (id, user_id, name, sport_type, start_date,
         distance, moving_time, elapsed_time, total_elevation_gain, max_speed,
         average_heartrate, max_heartrate, average_watts, max_watts,
         start_lat, start_lon, points_json, fetched_at,
         strava_url, posted_at, mastodon_post_url, scheduled_for_post,
         map_rendered_at, charts_rendered_at, source)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        data["id"], user_id,
        data.get("name"),
        data.get("sport_type"),
        data.get("start_date"),
        data.get("distance"),
        data.get("moving_time"),
        data.get("elapsed_time"),
        data.get("total_elevation_gain"),
        data.get("max_speed"),
        data.get("average_heartrate"),
        data.get("max_heartrate"),
        data.get("average_watts"),
        data.get("max_watts"),
        data.get("start_lat"),
        data.get("start_lon"),
        json.dumps(data.get("points") or []),
        datetime.now(timezone.utc).isoformat(),
        data.get("source_url"),
        posted_at,
        mastodon_post_url,
        scheduled_for_post,
        map_rendered_at,
        charts_rendered_at,
        source,
    ))
    conn.commit()
    conn.close()


def mark_posted(db_path, activity_id: int, mastodon_post_url: str, user_id: int):
    conn = _conn(db_path)
    conn.execute(
        "UPDATE activities SET posted_at=?, mastodon_post_url=?, scheduled_for_post=0, post_error=NULL"
        " WHERE id=? AND user_id=?",
        (datetime.now(timezone.utc).isoformat(), mastodon_post_url, activity_id, user_id),
    )
    conn.commit()
    conn.close()


def set_activity_error(db_path, activity_id: int, user_id: int, kind: str, error: str | None):
    """Set or clear render_error / post_error on an activity."""
    col = "render_error" if kind == "render" else "post_error"
    conn = _conn(db_path)
    conn.execute(f"UPDATE activities SET {col}=? WHERE id=? AND user_id=?",
                 (error, activity_id, user_id))
    conn.commit()
    conn.close()


def get_error_activities(db_path) -> dict:
    """Return activities with render or post errors, for the admin dashboard."""
    conn = _conn(db_path)
    render_errors = conn.execute(
        "SELECT a.id, a.name, a.user_id, u.username, a.render_error, a.start_date"
        " FROM activities a JOIN users u ON u.id = a.user_id"
        " WHERE a.render_error IS NOT NULL ORDER BY a.start_date DESC LIMIT 50"
    ).fetchall()
    post_errors = conn.execute(
        "SELECT a.id, a.name, a.user_id, u.username, a.post_error, a.start_date"
        " FROM activities a JOIN users u ON u.id = a.user_id"
        " WHERE a.post_error IS NOT NULL ORDER BY a.start_date DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return {
        "render_errors": [dict(r) for r in render_errors],
        "post_errors":   [dict(r) for r in post_errors],
    }


_SORT_COLS = {
    "date":      "start_date",
    "type":      "sport_type",
    "name":      "name",
    "distance":  "distance",
    "elevation": "total_elevation_gain",
}

def list_activities(db_path, user_id: int, limit: int = 20, offset: int = 0,
                    sort: str = "date", direction: str = "desc"):
    col = _SORT_COLS.get(sort, "start_date")
    dir_ = "ASC" if direction == "asc" else "DESC"
    conn = _conn(db_path)
    rows = conn.execute(
        f"SELECT * FROM activities WHERE user_id=? ORDER BY {col} {dir_} LIMIT ? OFFSET ?",
        (user_id, limit, offset),
    ).fetchall()
    conn.close()
    return rows


def count_activities(db_path, user_id: int) -> int:
    conn = _conn(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM activities WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    conn.close()
    return n


def get_activity(db_path, activity_id, user_id: int):
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT * FROM activities WHERE id=? AND user_id=?", (activity_id, user_id)
    ).fetchone()
    conn.close()
    return row


def get_unposted(db_path, user_id: int) -> list:
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT * FROM activities WHERE user_id=? AND posted_at IS NULL"
        " AND scheduled_for_post=1 ORDER BY start_date ASC",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def set_scheduled(db_path, activity_id: int, user_id: int, value: bool):
    conn = _conn(db_path)
    conn.execute(
        "UPDATE activities SET scheduled_for_post=? WHERE id=? AND user_id=?",
        (1 if value else 0, activity_id, user_id),
    )
    conn.commit()
    conn.close()


def get_points(row) -> list[tuple[float, float]]:
    raw = row["points_json"]
    if not raw:
        return []
    return [(p[0], p[1]) for p in json.loads(raw) if p[0] is not None and p[1] is not None]


def get_all_users(db_path) -> list:
    """Return all users that have a Strava access token (i.e. are connected)."""
    conn = _conn(db_path)
    rows = conn.execute("""
        SELECT u.id, u.username
        FROM users u
        JOIN settings s ON s.user_id = u.id
          AND s.area = 'strava' AND s.key = 'access_token'
          AND s.value IS NOT NULL AND s.value != ''
    """).fetchall()
    conn.close()
    return rows


def get_latest_activity_date(db_path, user_id: int) -> str | None:
    """Return the start_date of the most recent activity for this user, or None."""
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT start_date FROM activities WHERE user_id=? ORDER BY start_date DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.close()
    return row["start_date"] if row else None


def get_user_by_athlete_id(db_path, athlete_id: str):
    conn = _conn(db_path)
    row = conn.execute("""
        SELECT u.id, u.username FROM users u
        JOIN settings s ON s.user_id = u.id
          AND s.area = 'strava' AND s.key = 'athlete_id' AND s.value = ?
    """, (str(athlete_id),)).fetchone()
    conn.close()
    return row


def get_user_by_username(db_path, username: str):
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT id, username, password_hash FROM users WHERE username=?", (username,)
    ).fetchone()
    conn.close()
    return row


def get_user_by_id(db_path, user_id: int):
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT id, username, is_admin FROM users WHERE id=?", (user_id,)
    ).fetchone()
    conn.close()
    return row


def create_user(db_path, username: str, password_hash: str) -> int:
    """
    Create a new user account. If user_id=1 has no username yet (first run),
    claim it so existing activities/settings are preserved. Otherwise insert new.
    Returns the user_id.
    """
    conn = _conn(db_path)
    first = conn.execute("SELECT id, username FROM users WHERE id=1").fetchone()
    if first and not first["username"]:
        conn.execute(
            "UPDATE users SET username=?, password_hash=? WHERE id=1",
            (username, password_hash),
        )
        user_id = 1
    else:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
            (username, password_hash, datetime.now(timezone.utc).isoformat()),
        )
        user_id = cur.lastrowid
        _seed_defaults(conn, user_id)
    conn.commit()
    conn.close()
    return user_id


def log_daemon_run(db_path, started_at: str, finished_at: str,
                   duration_secs: float, activities_synced: int, error: str = None):
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO daemon_runs (started_at, finished_at, duration_secs, activities_synced, error)"
        " VALUES (?,?,?,?,?)",
        (started_at, finished_at, duration_secs, activities_synced, error),
    )
    # Keep only the last 100 runs
    conn.execute(
        "DELETE FROM daemon_runs WHERE id NOT IN"
        " (SELECT id FROM daemon_runs ORDER BY id DESC LIMIT 100)"
    )
    conn.commit()
    conn.close()


def get_admin_stats(db_path) -> dict:
    conn = _conn(db_path)
    user_count     = conn.execute("SELECT COUNT(*) FROM users WHERE username IS NOT NULL").fetchone()[0]
    activity_count = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    recent_runs    = conn.execute(
        "SELECT started_at, finished_at, duration_secs, activities_synced, error"
        " FROM daemon_runs ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()
    interval_minutes = 15
    return {
        "user_count":      user_count,
        "activity_count":  activity_count,
        "recent_runs":     [dict(r) for r in recent_runs],
        "interval_minutes": interval_minutes,
    }


def set_admin(db_path, username: str, is_admin: bool):
    conn = _conn(db_path)
    conn.execute("UPDATE users SET is_admin=? WHERE username=?", (1 if is_admin else 0, username))
    conn.commit()
    conn.close()


def get_user_stats(db_path, user_id: int) -> dict:
    conn = _conn(db_path)
    from datetime import datetime, timezone
    year = datetime.now(timezone.utc).year

    total = conn.execute(
        "SELECT COUNT(*) AS n, SUM(distance) AS dist, SUM(total_elevation_gain) AS elev"
        " FROM activities WHERE user_id=?", (user_id,)
    ).fetchone()

    this_year = conn.execute(
        "SELECT COUNT(*) AS n, SUM(distance) AS dist, SUM(total_elevation_gain) AS elev"
        " FROM activities WHERE user_id=? AND start_date >= ?",
        (user_id, f"{year}-01-01"),
    ).fetchone()

    by_sport = conn.execute(
        "SELECT sport_type, COUNT(*) AS n FROM activities"
        " WHERE user_id=? AND sport_type IS NOT NULL"
        " GROUP BY sport_type ORDER BY n DESC",
        (user_id,),
    ).fetchall()

    conn.close()
    return {
        "total_count":      total["n"] or 0,
        "total_distance":   (total["dist"] or 0) / 1000,
        "total_elevation":  total["elev"] or 0,
        "year":             year,
        "year_count":       this_year["n"] or 0,
        "year_distance":    (this_year["dist"] or 0) / 1000,
        "year_elevation":   this_year["elev"] or 0,
        "by_sport":         [{"sport": r["sport_type"], "count": r["n"]} for r in by_sport],
    }


def get_stream(row) -> list[dict]:
    raw = row["points_json"]
    if not raw:
        return []
    keys = ["lat", "lon", "ele", "hr", "power", "elapsed_secs"]
    return [dict(zip(keys, p)) for p in json.loads(raw)]


def find_overlapping_activity(db_path, user_id: int, start_date_iso: str | None,
                               elapsed_secs: int | None,
                               start_window_secs: int = 1800,
                               overlap_threshold: float = 0.8):
    """
    Find an existing activity whose time window significantly overlaps with the given one.
    Candidates must start within ±start_window_secs and have overlap/min_duration >= threshold.
    Returns the best-matching activity row, or None.
    """
    if not start_date_iso or not elapsed_secs:
        return None

    try:
        from datetime import timedelta
        start    = datetime.fromisoformat(start_date_iso.replace("Z", "+00:00"))
        start_ts = start.timestamp()
        end_ts   = start_ts + elapsed_secs

        window_lo = (start - timedelta(seconds=start_window_secs)).isoformat()
        window_hi = (start + timedelta(seconds=start_window_secs)).isoformat()
    except (ValueError, AttributeError):
        return None

    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT * FROM activities WHERE user_id=? AND start_date >= ? AND start_date <= ?",
        (user_id, window_lo, window_hi),
    ).fetchall()
    conn.close()

    best       = None
    best_ratio = 0.0

    for row in rows:
        try:
            row_start_ts = datetime.fromisoformat(
                row["start_date"].replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, AttributeError):
            continue

        row_elapsed = row["elapsed_time"] or row["moving_time"] or 0
        row_end_ts  = row_start_ts + row_elapsed

        overlap  = max(0.0, min(end_ts, row_end_ts) - max(start_ts, row_start_ts))
        min_dur  = min(elapsed_secs, max(row_elapsed, 1))
        ratio    = overlap / min_dur if min_dur > 0 else 0.0

        if ratio >= overlap_threshold and ratio > best_ratio:
            best_ratio = ratio
            best       = row

    return best


def enrich_activity_stream(db_path, activity_id: int, user_id: int, points: list):
    """Replace points_json for an existing activity and clear charts_rendered_at."""
    conn = _conn(db_path)
    conn.execute(
        "UPDATE activities SET points_json=?, charts_rendered_at=NULL"
        " WHERE id=? AND user_id=?",
        (json.dumps(points), activity_id, user_id),
    )
    conn.commit()
    conn.close()


def update_activity_metrics(
    db_path, activity_id: int, user_id: int,
    tss: float | None, np_watts: float | None,
    trimp: float | None, peak_power_json: str | None,
    hr_zone_secs_json: str | None = None,
    power_zone_secs_json: str | None = None,
    hr_tss: float | None = None,
    breakthroughs_json: str | None = None,
):
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn(db_path)
    conn.execute(
        "UPDATE activities"
        " SET tss=?, np_watts=?, trimp=?, peak_power_json=?,"
        "     hr_zone_secs_json=?, power_zone_secs_json=?, metrics_computed_at=?,"
        "     hr_tss=?"
        " WHERE id=? AND user_id=?",
        (tss, np_watts, trimp, peak_power_json,
         hr_zone_secs_json, power_zone_secs_json, now,
         hr_tss, activity_id, user_id),
    )
    if breakthroughs_json is not None:
        conn.execute(
            "UPDATE activities SET breakthroughs_json=? WHERE id=? AND user_id=?",
            (breakthroughs_json, activity_id, user_id),
        )
    conn.commit()
    conn.close()


def get_daily_loads(db_path, user_id: int) -> dict:
    """
    Return {ISO-date: tss} using power TSS where available, hr_tss otherwise.
    Activities with neither contribute nothing to the PMC.
    """
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT start_date, tss, hr_tss FROM activities"
        " WHERE user_id=? AND (tss IS NOT NULL OR hr_tss IS NOT NULL)"
        " AND start_date IS NOT NULL",
        (user_id,),
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        ds = (r["start_date"] or "")[:10]
        if ds:
            load = r["tss"] if r["tss"] is not None else r["hr_tss"]
            result[ds] = result.get(ds, 0.0) + (load or 0.0)
    return result


def get_all_peak_powers(db_path, user_id: int, days: int | None = None,
                        exclude_id: int | None = None) -> list:
    """
    Return list of parsed peak_power dicts for all activities (or last `days` days).
    exclude_id: omit a specific activity (used for breakthrough detection).
    """
    conn = _conn(db_path)
    excl = f" AND id != {int(exclude_id)}" if exclude_id is not None else ""
    if days is not None:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = conn.execute(
            f"SELECT peak_power_json FROM activities"
            f" WHERE user_id=? AND peak_power_json IS NOT NULL AND start_date >= ?{excl}",
            (user_id, cutoff),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT peak_power_json FROM activities"
            f" WHERE user_id=? AND peak_power_json IS NOT NULL{excl}",
            (user_id,),
        ).fetchall()
    conn.close()
    result = []
    for r in rows:
        try:
            result.append(json.loads(r["peak_power_json"]))
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def reset_metrics_computed(db_path, user_id: int):
    """Clear metrics_computed_at so the next backfill reprocesses all activities."""
    conn = _conn(db_path)
    conn.execute(
        "UPDATE activities SET metrics_computed_at=NULL WHERE user_id=?", (user_id,)
    )
    conn.commit()
    conn.close()


def get_activities_without_metrics(db_path, user_id: int) -> list:
    """Return activities that have a stream but have never had metrics computed."""
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT * FROM activities"
        " WHERE user_id=? AND metrics_computed_at IS NULL"
        " AND points_json IS NOT NULL AND points_json != '[]'",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def job_start(db_path, job_type: str, details: str = "") -> int:
    """Log the start of a background job. Returns the job id."""
    conn = _conn(db_path)
    cur = conn.execute(
        "INSERT INTO job_log (started_at, job_type, status, details) VALUES (?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), job_type, "running", details),
    )
    job_id = cur.lastrowid
    conn.commit()
    conn.close()
    return job_id


def job_finish(db_path, job_id: int, status: str = "done", details: str = ""):
    conn = _conn(db_path)
    conn.execute(
        "UPDATE job_log SET finished_at=?, status=?, details=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), status, details, job_id),
    )
    # Keep only last 200 entries
    conn.execute(
        "DELETE FROM job_log WHERE id NOT IN (SELECT id FROM job_log ORDER BY id DESC LIMIT 200)"
    )
    conn.commit()
    conn.close()


def get_recent_jobs(db_path, limit: int = 30) -> list:
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT id, started_at, finished_at, job_type, status, details"
        " FROM job_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_zone_totals(db_path, user_id: int) -> tuple:
    """
    Aggregate zone times across all activities.
    Returns (hr_totals, power_totals): each is {zone_name: total_seconds}.
    """
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT hr_zone_secs_json, power_zone_secs_json FROM activities"
        " WHERE user_id=? AND (hr_zone_secs_json IS NOT NULL OR power_zone_secs_json IS NOT NULL)",
        (user_id,),
    ).fetchall()
    conn.close()

    hr_totals    = {}
    power_totals = {}
    for r in rows:
        for col, dest in ((r["hr_zone_secs_json"], hr_totals),
                          (r["power_zone_secs_json"], power_totals)):
            if col:
                try:
                    for k, v in json.loads(col).items():
                        dest[k] = dest.get(k, 0.0) + v
                except (json.JSONDecodeError, TypeError):
                    pass
    return hr_totals, power_totals
