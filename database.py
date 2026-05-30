import json
import sqlite3
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Default settings seeded for every new user
# ---------------------------------------------------------------------------

_DEFAULT_SETTINGS = [
    ("strava",         "session_cookie",          ""),
    ("mastodon",       "instance",                "https://mastodon.social"),
    ("mastodon",       "token",                   ""),
    ("mastodon",       "visibility",              "public"),
    ("mastodon",       "handle",                  ""),
    ("mastodon",       "post_template",
        "{name} 🚴\n📍 {distance_km:.1f} km  🏔 {elevation_m:.0f} m  ⏱ {moving_time}"
        "\n\n#cycling #strava\n\nConnect Strava to the fediverse using Bikeodon"
        " [tim0s.github.io/Bikeodon]"),
    ("map",            "width",                   "1200"),
    ("map",            "height",                  "675"),
    ("map",            "zoom_offset",             "-1"),
    ("map",            "max_tiles",               "100"),
    ("map",            "tiles_url",               "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
    ("map",            "tiles_user_agent",        "Bikeodon/0.1 (https://github.com/tim0s/Bikeodon)"),
    ("map",            "padding_top",             "0.06"),
    ("map",            "padding_bottom",          "0.28"),
    ("map",            "padding_left",            "0.06"),
    ("map",            "padding_right",           "0.06"),
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

_DEFAULT_HR_ZONES = [
    (0, "Z1 Recovery",   60,  "#5b9bd5"),
    (1, "Z2 Endurance",  70,  "#70ad47"),
    (2, "Z3 Tempo",      80,  "#ffc000"),
    (3, "Z4 Threshold",  90,  "#ff7043"),
    (4, "Z5 VO2 Max",    100, "#d32f2f"),
]

_DEFAULT_POWER_ZONES = [
    (0, "Z1 Recovery",       55,  "#5b9bd5"),
    (1, "Z2 Endurance",      75,  "#70ad47"),
    (2, "Z3 Tempo",          90,  "#ffc000"),
    (3, "Z4 Threshold",      105, "#ff7043"),
    (4, "Z5 VO2 Max",        120, "#d32f2f"),
    (5, "Z6 Anaerobic",      150, "#9c27b0"),
    (6, "Z7 Neuromuscular",  999, "#424242"),
]


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
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL
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
        ("user_id",           "INTEGER NOT NULL DEFAULT 1"),
        ("strava_url",        "TEXT"),
        ("posted_at",         "TEXT"),
        ("mastodon_post_url", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE activities ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass

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


def get_zones(db_path, user_id: int, zone_type: str) -> list[dict]:
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT name, max_pct, color FROM zones"
        " WHERE user_id=? AND type=? ORDER BY zone_index",
        (user_id, zone_type),
    ).fetchall()
    conn.close()
    return [{"name": r["name"], "max_pct": r["max_pct"], "color": r["color"]} for r in rows]


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
        "strava": {
            "session_cookie": txt("strava", "session_cookie"),
        },
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

def upsert_activity(db_path, data: dict, user_id: int = 1):
    conn = _conn(db_path)
    existing = conn.execute(
        "SELECT posted_at, mastodon_post_url FROM activities WHERE id=? AND user_id=?",
        (data["id"], user_id),
    ).fetchone()
    posted_at         = existing["posted_at"]         if existing else None
    mastodon_post_url = existing["mastodon_post_url"] if existing else None

    conn.execute("""
        INSERT OR REPLACE INTO activities
        (id, user_id, name, sport_type, start_date,
         distance, moving_time, elapsed_time, total_elevation_gain, max_speed,
         average_heartrate, max_heartrate, average_watts, max_watts,
         start_lat, start_lon, points_json, fetched_at,
         strava_url, posted_at, mastodon_post_url)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        f"https://www.strava.com/activities/{data['id']}",
        posted_at,
        mastodon_post_url,
    ))
    conn.commit()
    conn.close()


def mark_posted(db_path, activity_id: int, mastodon_post_url: str, user_id: int = 1):
    conn = _conn(db_path)
    conn.execute(
        "UPDATE activities SET posted_at=?, mastodon_post_url=? WHERE id=? AND user_id=?",
        (datetime.now(timezone.utc).isoformat(), mastodon_post_url, activity_id, user_id),
    )
    conn.commit()
    conn.close()


def list_activities(db_path, user_id: int = 1):
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT * FROM activities WHERE user_id=? ORDER BY start_date DESC", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def get_activity(db_path, activity_id, user_id: int = 1):
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT * FROM activities WHERE id=? AND user_id=?", (activity_id, user_id)
    ).fetchone()
    conn.close()
    return row


def get_unposted(db_path, user_id: int = 1) -> list:
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT * FROM activities WHERE user_id=? AND posted_at IS NULL ORDER BY start_date ASC",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def get_points(row) -> list[tuple[float, float]]:
    raw = row["points_json"]
    if not raw:
        return []
    return [(p[0], p[1]) for p in json.loads(raw)]


def get_stream(row) -> list[dict]:
    raw = row["points_json"]
    if not raw:
        return []
    keys = ["lat", "lon", "ele", "hr", "power", "elapsed_secs"]
    return [dict(zip(keys, p)) for p in json.loads(raw)]
