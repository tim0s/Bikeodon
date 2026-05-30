import json
import sqlite3
from datetime import datetime, timezone


def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path):
    conn = _conn(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activities (
            id                   INTEGER PRIMARY KEY,
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
            fetched_at           TEXT
        )
    """)
    conn.commit()
    conn.close()


def upsert_activity(db_path, data: dict):
    conn = _conn(db_path)
    conn.execute("""
        INSERT OR REPLACE INTO activities
        (id, name, sport_type, start_date,
         distance, moving_time, elapsed_time, total_elevation_gain, max_speed,
         average_heartrate, max_heartrate, average_watts, max_watts,
         start_lat, start_lon, points_json, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        data["id"],
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
    ))
    conn.commit()
    conn.close()


def list_activities(db_path):
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT * FROM activities ORDER BY start_date DESC"
    ).fetchall()
    conn.close()
    return rows


def get_activity(db_path, activity_id):
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT * FROM activities WHERE id = ?", (activity_id,)
    ).fetchone()
    conn.close()
    return row


def get_points(row) -> list[tuple[float, float]]:
    """Return (lat, lon) pairs for map rendering."""
    raw = row["points_json"]
    if not raw:
        return []
    return [(p[0], p[1]) for p in json.loads(raw)]


def get_stream(row) -> list[dict]:
    """Return full per-point stream as dicts with lat, lon, ele, hr, power, elapsed_secs."""
    raw = row["points_json"]
    if not raw:
        return []
    keys = ["lat", "lon", "ele", "hr", "power", "elapsed_secs"]
    return [dict(zip(keys, p)) for p in json.loads(raw)]
