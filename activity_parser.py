"""
Parse GPX, TCX, and FIT activity files into Bikeodon's internal activity dict.

Each parser returns a list of activity dicts (a file can contain multiple
activities, though GPX/FIT usually contain one). The dict structure matches
what _build_activity() produces from Strava data.
"""

import hashlib
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_file(filename: str, content: bytes) -> list[dict]:
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "gpx":
        return _parse_gpx(content)
    if ext == "tcx":
        return _parse_tcx(content)
    if ext == "fit":
        return _parse_fit(content)
    raise ValueError(f"Unsupported file type: .{ext}")


def stream_from_file(path: str) -> list[dict]:
    """Parse a FIT/GPX/TCX file and return a stream list in the same format as
    database.get_stream(): one dict per sample with keys lat, lon, ele, hr,
    power, elapsed_secs."""
    import os as _os
    with open(path, "rb") as f:
        content = f.read()
    acts = parse_file(_os.path.basename(path), content)
    if not acts:
        return []
    keys = ["lat", "lon", "ele", "hr", "power", "elapsed_secs"]
    return [dict(zip(keys, p)) for p in acts[0]["points"]]


def points_from_file(path: str) -> list[tuple[float, float]]:
    """Return (lat, lon) tuples from a FIT/GPX/TCX file, omitting samples
    without valid GPS coordinates."""
    return [
        (p["lat"], p["lon"])
        for p in stream_from_file(path)
        if p.get("lat") is not None and p.get("lon") is not None
    ]


# ---------------------------------------------------------------------------
# Stable ID generation
# ---------------------------------------------------------------------------

def _file_id(content: bytes) -> int:
    """Derive a stable integer ID from file content. Large enough to avoid
    collisions with Strava IDs (currently ~10^10); uses first 13 hex digits
    of SHA-256 (~54 bits, max ~4.5×10^15)."""
    return int(hashlib.sha256(content).hexdigest()[:13], 16)


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _haversine(lat1, lon1, lat2, lon2) -> float:
    """Distance in metres between two GPS coordinates."""
    R = 6_371_000
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _compute_stats(points: list) -> dict:
    """Derive summary stats from a stream of [lat, lon, ele, hr, power, secs]."""
    if not points:
        return {}

    distance = 0.0
    elev_gain = 0.0
    hr_vals, power_vals = [], []

    for i, p in enumerate(points):
        if i > 0:
            prev = points[i - 1]
            if None not in (p[0], p[1], prev[0], prev[1]):
                distance += _haversine(prev[0], prev[1], p[0], p[1])
            if p[2] is not None and prev[2] is not None:
                gain = p[2] - prev[2]
                if gain > 0:
                    elev_gain += gain
        if p[3] is not None:
            hr_vals.append(p[3])
        if p[4] is not None:
            power_vals.append(p[4])

    elapsed = points[-1][5] if points[-1][5] is not None else None

    return {
        "distance":             distance,
        "total_elevation_gain": elev_gain,
        "moving_time":          elapsed,
        "elapsed_time":         elapsed,
        "average_heartrate":    sum(hr_vals) / len(hr_vals) if hr_vals else None,
        "max_heartrate":        max(hr_vals) if hr_vals else None,
        "average_watts":        sum(power_vals) / len(power_vals) if power_vals else None,
        "max_watts":            max(power_vals) if power_vals else None,
    }


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# GPX
# ---------------------------------------------------------------------------

_GPX_NS = {
    "gpx":   "http://www.topografix.com/GPX/1/1",
    "gpxtpx": "http://www.garmin.com/xmlschemas/TrackPointExtension/v1",
    "ns3":   "http://www.garmin.com/xmlschemas/TrackPointExtension/v1",
}

def _gpx_ext(trkpt, tag: str):
    """Extract a value from Garmin TrackPointExtension elements."""
    for ns in ("gpxtpx", "ns3"):
        el = trkpt.find(f".//{{{_GPX_NS[ns]}}}{tag}")
        if el is not None and el.text:
            try:
                return float(el.text)
            except ValueError:
                pass
    return None


def _parse_gpx(content: bytes) -> list[dict]:
    root = ET.fromstring(content)
    ns = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else _GPX_NS["gpx"]

    def _find(el, path):
        return el.find(path.replace("gpx:", f"{{{ns}}}"))

    def _findall(el, path):
        return el.findall(path.replace("gpx:", f"{{{ns}}}"))

    activities = []
    for trk in _findall(root, "gpx:trk"):
        name_el = _find(trk, "gpx:name")
        name = name_el.text.strip() if name_el is not None and name_el.text else "Uploaded activity"

        type_el = _find(trk, "gpx:type")
        sport = type_el.text.strip() if type_el is not None and type_el.text else "Ride"

        points = []
        start_time = None

        for trkpt in _findall(trk, "gpx:trkseg/gpx:trkpt"):
            lat = float(trkpt.get("lat", 0))
            lon = float(trkpt.get("lon", 0))
            ele_el = _find(trkpt, "gpx:ele")
            ele = float(ele_el.text) if ele_el is not None and ele_el.text else None
            time_el = _find(trkpt, "gpx:time")
            t = None
            if time_el is not None and time_el.text:
                try:
                    t = datetime.fromisoformat(time_el.text.replace("Z", "+00:00"))
                except ValueError:
                    pass

            hr    = _gpx_ext(trkpt, "hr")
            power = _gpx_ext(trkpt, "PowerInWatts") or _gpx_ext(trkpt, "watts")

            if start_time is None and t:
                start_time = t
            elapsed = int((t - start_time).total_seconds()) if t and start_time else None

            points.append([lat, lon, ele, hr, power, elapsed])

        if not points:
            continue

        stats = _compute_stats(points)
        activity_id = _file_id(content + name.encode())

        activities.append({
            "id":                   activity_id,
            "name":                 name,
            "sport_type":           sport,
            "start_date":           _iso(start_time) if start_time else None,
            "start_lat":            points[0][0],
            "start_lon":            points[0][1],
            "points":               points,
            "source_url":           None,
            **stats,
        })

    return activities


# ---------------------------------------------------------------------------
# TCX
# ---------------------------------------------------------------------------

_TCX_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
_TCX_EXT_NS = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"

def _parse_tcx(content: bytes) -> list[dict]:
    root = ET.fromstring(content)

    def _t(tag):
        return f"{{{_TCX_NS}}}{tag}"

    def _e(tag):
        return f"{{{_TCX_EXT_NS}}}{tag}"

    activities = []
    for act in root.iter(_t("Activity")):
        sport = act.get("Sport", "Ride")
        id_el = act.find(_t("Id"))
        start_date = None
        if id_el is not None and id_el.text:
            try:
                start_date = datetime.fromisoformat(id_el.text.replace("Z", "+00:00"))
            except ValueError:
                pass

        name_el = act.find(_t("Notes"))
        name = name_el.text.strip() if name_el is not None and name_el.text else (
            f"{sport} {start_date.date()}" if start_date else "Uploaded activity"
        )

        points = []
        ref_time = None

        for tp in act.iter(_t("Trackpoint")):
            time_el = tp.find(_t("Time"))
            t = None
            if time_el is not None and time_el.text:
                try:
                    t = datetime.fromisoformat(time_el.text.replace("Z", "+00:00"))
                except ValueError:
                    pass

            pos = tp.find(_t("Position"))
            lat = lon = None
            if pos is not None:
                lat_el = pos.find(_t("LatitudeDegrees"))
                lon_el = pos.find(_t("LongitudeDegrees"))
                if lat_el is not None and lat_el.text:
                    lat = float(lat_el.text)
                if lon_el is not None and lon_el.text:
                    lon = float(lon_el.text)

            ele_el = tp.find(_t("AltitudeMeters"))
            ele = float(ele_el.text) if ele_el is not None and ele_el.text else None

            hr_el = tp.find(f".//{_t('Value')}")
            hr = float(hr_el.text) if hr_el is not None and hr_el.text else None

            watts_el = tp.find(f".//{_e('Watts')}")
            power = float(watts_el.text) if watts_el is not None and watts_el.text else None

            if ref_time is None and t:
                ref_time = t
            elapsed = int((t - ref_time).total_seconds()) if t and ref_time else None

            # Include points even without GPS (indoor/virtual rides)
            if t is not None:
                points.append([lat, lon, ele, hr, power, elapsed])

        if not points:
            continue

        stats = _compute_stats(points)
        activity_id = _file_id(content)

        activities.append({
            "id":         activity_id,
            "name":       name,
            "sport_type": sport,
            "start_date": _iso(start_date) if start_date else None,
            "start_lat":  next((p[0] for p in points if p[0] is not None), None),
            "start_lon":  next((p[1] for p in points if p[1] is not None), None),
            "points":     points,
            "source_url": None,
            **stats,
        })

    return activities


# ---------------------------------------------------------------------------
# FIT
# ---------------------------------------------------------------------------

_FIT_SPORT_MAP = {
    "cycling": "Ride",
    "running": "Run",
    "walking": "Walk",
    "swimming": "Swim",
    "generic": "Workout",
}

def _parse_fit(content: bytes) -> list[dict]:
    try:
        from fitparse import FitFile
    except ImportError:
        raise ImportError("fitparse is required for FIT files: pip install fitparse")

    fit = FitFile(content)

    # Collect session summary
    session = {}
    for msg in fit.get_messages("session"):
        for field in msg:
            session[field.name] = field.value
        break  # one session per file typically

    sport_raw = session.get("sport", "cycling")
    sport = _FIT_SPORT_MAP.get(str(sport_raw).lower(), str(sport_raw).capitalize())

    start_time = session.get("start_time")
    name = f"{sport} {start_time.date()}" if start_time else "Uploaded activity"

    points = []
    ref_time = None

    for msg in fit.get_messages("record"):
        data = {f.name: f.value for f in msg}

        # FIT stores lat/lon as semicircles (32-bit integer)
        lat = data.get("position_lat")
        lon = data.get("position_long")
        if lat is not None:
            lat = lat * (180 / 2**31)
        if lon is not None:
            lon = lon * (180 / 2**31)

        ele   = data.get("altitude") or data.get("enhanced_altitude")
        hr    = data.get("heart_rate")
        power = data.get("power")
        t     = data.get("timestamp")

        if ref_time is None and t:
            ref_time = t
        elapsed = int((t - ref_time).total_seconds()) if t and ref_time else None

        # Include points even without GPS (indoor/virtual rides)
        if t is not None:
            points.append([lat, lon, ele,
                           float(hr) if hr else None,
                           float(power) if power else None,
                           elapsed])

    if not points:
        return []

    stats = _compute_stats(points)
    # Prefer session-level stats where available
    if session.get("total_distance"):
        stats["distance"] = float(session["total_distance"])
    if session.get("total_ascent"):
        stats["total_elevation_gain"] = float(session["total_ascent"])
    if session.get("total_elapsed_time"):
        stats["elapsed_time"] = int(session["total_elapsed_time"])
    if session.get("total_timer_time"):
        stats["moving_time"] = int(session["total_timer_time"])
    if session.get("avg_heart_rate"):
        stats["average_heartrate"] = float(session["avg_heart_rate"])
    if session.get("max_heart_rate"):
        stats["max_heartrate"] = float(session["max_heart_rate"])
    if session.get("avg_power"):
        stats["average_watts"] = float(session["avg_power"])
    if session.get("max_power"):
        stats["max_watts"] = float(session["max_power"])

    return [{
        "id":         _file_id(content),
        "name":       name,
        "sport_type": sport,
        "start_date": _iso(start_time) if start_time else None,
        "start_lat":  next((p[0] for p in points if p[0] is not None), None),
        "start_lon":  next((p[1] for p in points if p[1] is not None), None),
        "points":     points,
        "source_url": None,
        **stats,
    }]
