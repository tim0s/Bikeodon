import os
import re

import gpxpy
import requests

_BASE = "https://www.strava.com"


class StravaClient:
    def __init__(self):
        session_cookie = os.environ.get("STRAVA_SESSION", "").strip()
        if not session_cookie:
            raise ValueError(
                "STRAVA_SESSION not set.\n"
                "  1. Log in to strava.com\n"
                "  2. DevTools → Application → Cookies → strava.com\n"
                "  3. Copy _strava4_session value\n"
                "  4. export STRAVA_SESSION=<value>"
            )
        self._s = requests.Session()
        self._s.cookies.set("_strava4_session", session_cookie, domain="www.strava.com")
        self._s.headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        self._cached_athlete_id = None

    def _athlete_id(self):
        if self._cached_athlete_id:
            return self._cached_athlete_id
        resp = self._s.get(f"{_BASE}/dashboard")
        m = re.search(r'"id"\s*:\s*(\d+)\s*,\s*"logged_in"\s*:\s*true', resp.text)
        if not m:
            raise RuntimeError("Could not find athlete ID — is STRAVA_SESSION valid?")
        self._cached_athlete_id = int(m.group(1))
        return self._cached_athlete_id

    def get_activity_ids(self, n: int = 10) -> list[int]:
        aid = self._athlete_id()
        resp = self._s.get(f"{_BASE}/athletes/{aid}")
        resp.raise_for_status()
        ids = re.findall(r'/activities/(\d{6,12})', resp.text)
        return [int(i) for i in dict.fromkeys(ids)][:n]

    def download_gpx(self, activity_id: int) -> bytes:
        resp = self._s.get(f"{_BASE}/activities/{activity_id}/export_gpx")
        resp.raise_for_status()
        return resp.content

    def get_activity(self, activity_id: int) -> dict:
        gpx_bytes = self.download_gpx(activity_id)
        return _parse_gpx(activity_id, gpx_bytes)


def _ext_values(pt) -> tuple[float | None, float | None]:
    """Extract (heart_rate, power) from a GPX trackpoint's extensions."""
    hr = power = None
    for ext in pt.extensions:
        tag = ext.tag.split("}")[-1].lower()
        if tag == "power":
            try:
                power = float(ext.text)
            except (TypeError, ValueError):
                pass
        # Garmin/Strava TrackPointExtension block
        for child in ext:
            ctag = child.tag.split("}")[-1].lower()
            if ctag == "hr":
                try:
                    hr = float(child.text)
                except (TypeError, ValueError):
                    pass
            elif ctag == "power":
                try:
                    power = float(child.text)
                except (TypeError, ValueError):
                    pass
    return hr, power


def _parse_gpx(activity_id: int, gpx_bytes: bytes) -> dict:
    gpx = gpxpy.parse(gpx_bytes.decode("utf-8"))

    # Build point list: [lat, lon, elevation_m, heart_rate, power, elapsed_secs]
    # None where data is not available.
    points = []
    t0 = None
    for track in gpx.tracks:
        for segment in track.segments:
            for pt in segment.points:
                hr, power = _ext_values(pt)
                if pt.time is not None:
                    if t0 is None:
                        t0 = pt.time
                    elapsed = (pt.time - t0).total_seconds()
                else:
                    elapsed = None
                points.append([pt.latitude, pt.longitude, pt.elevation, hr, power, elapsed])

    moving   = gpx.get_moving_data()
    updown   = gpx.get_uphill_downhill()
    duration = gpx.get_duration()

    first_pt = next(
        (pt for track in gpx.tracks for seg in track.segments for pt in seg.points if pt.time),
        None,
    )

    # Aggregate HR and power
    hrs    = [p[3] for p in points if p[3] is not None]
    powers = [p[4] for p in points if p[4] is not None]

    sport_type = (gpx.tracks[0].type or "Ride") if gpx.tracks else "Ride"
    name       = gpx.name or (gpx.tracks[0].name if gpx.tracks else f"Activity {activity_id}")

    return {
        "id":                   activity_id,
        "name":                 name,
        "sport_type":           sport_type,
        "start_date":           first_pt.time.isoformat() if first_pt else None,
        "distance":             moving.moving_distance if moving else None,
        "moving_time":          moving.moving_time if moving else None,
        "elapsed_time":         duration,
        "total_elevation_gain": updown.uphill if updown else None,
        "max_speed":            moving.max_speed if moving else None,
        "average_heartrate":    sum(hrs) / len(hrs) if hrs else None,
        "max_heartrate":        max(hrs) if hrs else None,
        "average_watts":        sum(powers) / len(powers) if powers else None,
        "max_watts":            max(powers) if powers else None,
        "start_lat":            points[0][0] if points else None,
        "start_lon":            points[0][1] if points else None,
        # Full stream: [[lat, lon, ele, hr, power], ...]
        "points":               points,
    }
