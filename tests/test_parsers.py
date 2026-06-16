"""
Tests for activity_parser: GPX, TCX, and edge-case coverage.

FIT parsing is exercised in test_performance.py and test_rendering.py via
real .fit fixtures; those tests already cover the happy path. This file
focuses on GPX and TCX (zero coverage before), plus edge cases that apply
to all formats.
"""

import hashlib
import pytest
from activity_parser import parse_file, stream_from_file, points_from_file, _file_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gpx(name="Morning Ride", sport="Ride", trackpoints=None, extensions="") -> bytes:
    """Build a minimal GPX 1.1 file. trackpoints is a list of dicts with keys
    lat, lon, ele, time, hr, power (all optional except lat/lon/time)."""
    if trackpoints is None:
        trackpoints = [
            {"lat": 48.0, "lon": 11.0, "ele": 500, "time": "2024-06-01T07:00:00Z"},
            {"lat": 48.01, "lon": 11.01, "ele": 510, "time": "2024-06-01T07:30:00Z"},
        ]
    trkpts = []
    for tp in trackpoints:
        ext_block = ""
        if tp.get("hr") or tp.get("power"):
            inner = ""
            if tp.get("hr"):
                inner += f"<gpxtpx:hr>{tp['hr']}</gpxtpx:hr>"
            if tp.get("power"):
                inner += f"<gpxtpx:PowerInWatts>{tp['power']}</gpxtpx:PowerInWatts>"
            ext_block = f"<extensions><gpxtpx:TrackPointExtension>{inner}</gpxtpx:TrackPointExtension></extensions>"
        ele_block = f"<ele>{tp['ele']}</ele>" if tp.get("ele") is not None else ""
        trkpts.append(
            f'<trkpt lat="{tp["lat"]}" lon="{tp["lon"]}">'
            f"{ele_block}<time>{tp['time']}</time>{ext_block}</trkpt>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1"'
        ' xmlns="http://www.topografix.com/GPX/1/1"'
        ' xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
        f"<trk><name>{name}</name><type>{sport}</type>"
        f"<trkseg>{''.join(trkpts)}</trkseg></trk>"
        f"{extensions}</gpx>"
    ).encode()


def _tcx(name=None, sport="Biking", trackpoints=None) -> bytes:
    """Build a minimal TCX file."""
    if trackpoints is None:
        trackpoints = [
            {"lat": 48.0,  "lon": 11.0,  "ele": 500, "time": "2024-06-01T07:00:00Z", "hr": 140},
            {"lat": 48.01, "lon": 11.01, "ele": 510, "time": "2024-06-01T07:30:00Z", "hr": 150},
        ]
    ns  = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
    ext = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"
    start = trackpoints[0]["time"] if trackpoints else "2024-06-01T07:00:00Z"
    notes_block = f"<Notes xmlns=\"{ns}\">{name}</Notes>" if name else ""
    tps = []
    for tp in trackpoints:
        pos = (
            f"<Position xmlns=\"{ns}\">"
            f"<LatitudeDegrees>{tp['lat']}</LatitudeDegrees>"
            f"<LongitudeDegrees>{tp['lon']}</LongitudeDegrees>"
            f"</Position>"
        ) if tp.get("lat") is not None else ""
        ele = f"<AltitudeMeters xmlns=\"{ns}\">{tp['ele']}</AltitudeMeters>" if tp.get("ele") is not None else ""
        hr  = (
            f"<HeartRateBpm xmlns=\"{ns}\"><Value>{tp['hr']}</Value></HeartRateBpm>"
        ) if tp.get("hr") is not None else ""
        pwr = (
            f"<Extensions xmlns=\"{ns}\"><TPX xmlns=\"{ext}\"><Watts>{tp['power']}</Watts></TPX></Extensions>"
        ) if tp.get("power") is not None else ""
        tps.append(
            f"<Trackpoint xmlns=\"{ns}\">"
            f"<Time xmlns=\"{ns}\">{tp['time']}</Time>"
            f"{pos}{ele}{hr}{pwr}"
            f"</Trackpoint>"
        )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<TrainingCenterDatabase xmlns="{ns}">'
        f'<Activities>'
        f'<Activity Sport="{sport}">'
        f'<Id xmlns="{ns}">{start}</Id>'
        f'{notes_block}'
        f"{''.join(tps)}"
        f'</Activity>'
        f'</Activities>'
        f'</TrainingCenterDatabase>'
    ).encode()


# ---------------------------------------------------------------------------
# GPX — basic structure
# ---------------------------------------------------------------------------

class TestGPXParsing:

    def test_returns_one_activity(self):
        acts = parse_file("ride.gpx", _gpx())
        assert len(acts) == 1

    def test_name_and_sport(self):
        acts = parse_file("ride.gpx", _gpx(name="Evening Run", sport="Run"))
        assert acts[0]["name"] == "Evening Run"
        assert acts[0]["sport_type"] == "Run"

    def test_start_date(self):
        acts = parse_file("ride.gpx", _gpx())
        assert acts[0]["start_date"].startswith("2024-06-01")

    def test_points_count(self):
        acts = parse_file("ride.gpx", _gpx())
        assert len(acts[0]["points"]) == 2

    def test_lat_lon_ele(self):
        acts = parse_file("ride.gpx", _gpx())
        p0 = acts[0]["points"][0]
        assert p0[0] == pytest.approx(48.0)
        assert p0[1] == pytest.approx(11.0)
        assert p0[2] == pytest.approx(500.0)

    def test_elapsed_secs(self):
        acts = parse_file("ride.gpx", _gpx())
        points = acts[0]["points"]
        assert points[0][5] == 0
        assert points[1][5] == 1800  # 30 min

    def test_distance_nonzero(self):
        acts = parse_file("ride.gpx", _gpx())
        assert acts[0]["distance"] > 0

    def test_elevation_gain(self):
        acts = parse_file("ride.gpx", _gpx())
        assert acts[0]["total_elevation_gain"] == pytest.approx(10.0)

    def test_stable_id(self):
        content = _gpx(name="Test")
        id1 = parse_file("ride.gpx", content)[0]["id"]
        id2 = parse_file("ride.gpx", content)[0]["id"]
        assert id1 == id2

    def test_id_differs_by_name(self):
        id1 = parse_file("a.gpx", _gpx(name="Ride A"))[0]["id"]
        id2 = parse_file("b.gpx", _gpx(name="Ride B"))[0]["id"]
        assert id1 != id2

    def test_default_name_when_missing(self):
        content = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
            b"<trk><trkseg>"
            b'<trkpt lat="48.0" lon="11.0"><time>2024-06-01T07:00:00Z</time></trkpt>'
            b"</trkseg></trk></gpx>"
        )
        acts = parse_file("ride.gpx", content)
        assert acts[0]["name"] == "Uploaded activity"

    def test_default_sport_when_missing(self):
        content = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
            b"<trk><name>X</name><trkseg>"
            b'<trkpt lat="48.0" lon="11.0"><time>2024-06-01T07:00:00Z</time></trkpt>'
            b"</trkseg></trk></gpx>"
        )
        acts = parse_file("ride.gpx", content)
        assert acts[0]["sport_type"] == "Ride"

    def test_no_points_skips_track(self):
        content = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
            b"<trk><name>Empty</name><trkseg></trkseg></trk></gpx>"
        )
        acts = parse_file("ride.gpx", content)
        assert acts == []

    def test_multiple_tracks(self):
        content = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
            b"<trk><name>A</name><trkseg>"
            b'<trkpt lat="48.0" lon="11.0"><time>2024-06-01T07:00:00Z</time></trkpt>'
            b"</trkseg></trk>"
            b"<trk><name>B</name><trkseg>"
            b'<trkpt lat="49.0" lon="12.0"><time>2024-06-02T07:00:00Z</time></trkpt>'
            b"</trkseg></trk></gpx>"
        )
        acts = parse_file("ride.gpx", content)
        assert len(acts) == 2
        assert {a["name"] for a in acts} == {"A", "B"}


class TestGPXExtensions:

    def test_hr_parsed(self):
        tps = [
            {"lat": 48.0, "lon": 11.0, "ele": 500, "time": "2024-06-01T07:00:00Z", "hr": 145},
            {"lat": 48.01, "lon": 11.01, "ele": 510, "time": "2024-06-01T07:01:00Z", "hr": 155},
        ]
        acts = parse_file("ride.gpx", _gpx(trackpoints=tps))
        points = acts[0]["points"]
        assert points[0][3] == pytest.approx(145.0)
        assert points[1][3] == pytest.approx(155.0)

    def test_hr_stats(self):
        tps = [
            {"lat": 48.0, "lon": 11.0, "time": "2024-06-01T07:00:00Z", "hr": 140},
            {"lat": 48.01, "lon": 11.01, "time": "2024-06-01T07:01:00Z", "hr": 160},
        ]
        acts = parse_file("ride.gpx", _gpx(trackpoints=tps))
        act = acts[0]
        assert act["average_heartrate"] == pytest.approx(150.0)
        assert act["max_heartrate"] == pytest.approx(160.0)

    def test_power_parsed(self):
        tps = [
            {"lat": 48.0, "lon": 11.0, "time": "2024-06-01T07:00:00Z", "power": 200},
            {"lat": 48.01, "lon": 11.01, "time": "2024-06-01T07:01:00Z", "power": 250},
        ]
        acts = parse_file("ride.gpx", _gpx(trackpoints=tps))
        points = acts[0]["points"]
        assert points[0][4] == pytest.approx(200.0)
        assert points[1][4] == pytest.approx(250.0)

    def test_power_stats(self):
        tps = [
            {"lat": 48.0, "lon": 11.0, "time": "2024-06-01T07:00:00Z", "power": 200},
            {"lat": 48.01, "lon": 11.01, "time": "2024-06-01T07:01:00Z", "power": 300},
        ]
        acts = parse_file("ride.gpx", _gpx(trackpoints=tps))
        act = acts[0]
        assert act["average_watts"] == pytest.approx(250.0)
        assert act["max_watts"] == pytest.approx(300.0)

    def test_no_extensions_yields_none(self):
        acts = parse_file("ride.gpx", _gpx())
        p = acts[0]["points"][0]
        assert p[3] is None  # hr
        assert p[4] is None  # power


# ---------------------------------------------------------------------------
# TCX — basic structure
# ---------------------------------------------------------------------------

class TestTCXParsing:

    def test_returns_one_activity(self):
        acts = parse_file("ride.tcx", _tcx())
        assert len(acts) == 1

    def test_sport_attribute(self):
        acts = parse_file("ride.tcx", _tcx(sport="Running"))
        assert acts[0]["sport_type"] == "Running"

    def test_start_date(self):
        acts = parse_file("ride.tcx", _tcx())
        assert acts[0]["start_date"].startswith("2024-06-01")

    def test_name_from_notes(self):
        acts = parse_file("ride.tcx", _tcx(name="Thursday Club Ride"))
        assert acts[0]["name"] == "Thursday Club Ride"

    def test_name_fallback_to_sport_date(self):
        acts = parse_file("ride.tcx", _tcx(name=None))
        # No <Notes> → "Sport YYYY-MM-DD"
        assert "Biking" in acts[0]["name"] or "2024-06-01" in acts[0]["name"]

    def test_points_count(self):
        acts = parse_file("ride.tcx", _tcx())
        assert len(acts[0]["points"]) == 2

    def test_lat_lon(self):
        acts = parse_file("ride.tcx", _tcx())
        p0 = acts[0]["points"][0]
        assert p0[0] == pytest.approx(48.0)
        assert p0[1] == pytest.approx(11.0)

    def test_elevation(self):
        acts = parse_file("ride.tcx", _tcx())
        assert acts[0]["points"][0][2] == pytest.approx(500.0)
        assert acts[0]["points"][1][2] == pytest.approx(510.0)

    def test_elapsed_secs(self):
        acts = parse_file("ride.tcx", _tcx())
        points = acts[0]["points"]
        assert points[0][5] == 0
        assert points[1][5] == 1800

    def test_hr_parsed(self):
        acts = parse_file("ride.tcx", _tcx())
        assert acts[0]["points"][0][3] == pytest.approx(140.0)
        assert acts[0]["points"][1][3] == pytest.approx(150.0)

    def test_hr_stats(self):
        acts = parse_file("ride.tcx", _tcx())
        assert acts[0]["average_heartrate"] == pytest.approx(145.0)
        assert acts[0]["max_heartrate"] == pytest.approx(150.0)

    def test_power_parsed(self):
        tps = [
            {"lat": 48.0,  "lon": 11.0,  "ele": 500, "time": "2024-06-01T07:00:00Z", "hr": 140, "power": 220},
            {"lat": 48.01, "lon": 11.01, "ele": 510, "time": "2024-06-01T07:01:00Z", "hr": 150, "power": 260},
        ]
        acts = parse_file("ride.tcx", _tcx(trackpoints=tps))
        points = acts[0]["points"]
        assert points[0][4] == pytest.approx(220.0)
        assert points[1][4] == pytest.approx(260.0)

    def test_power_stats(self):
        tps = [
            {"lat": 48.0,  "lon": 11.0,  "time": "2024-06-01T07:00:00Z", "hr": 140, "power": 200},
            {"lat": 48.01, "lon": 11.01, "time": "2024-06-01T07:01:00Z", "hr": 150, "power": 300},
        ]
        acts = parse_file("ride.tcx", _tcx(trackpoints=tps))
        assert acts[0]["average_watts"] == pytest.approx(250.0)
        assert acts[0]["max_watts"] == pytest.approx(300.0)

    def test_distance_nonzero(self):
        acts = parse_file("ride.tcx", _tcx())
        assert acts[0]["distance"] > 0

    def test_elevation_gain(self):
        acts = parse_file("ride.tcx", _tcx())
        assert acts[0]["total_elevation_gain"] == pytest.approx(10.0)

    def test_stable_id(self):
        content = _tcx()
        id1 = parse_file("ride.tcx", content)[0]["id"]
        id2 = parse_file("ride.tcx", content)[0]["id"]
        assert id1 == id2

    def test_no_points_skips_activity(self):
        ns = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
        content = (
            f'<?xml version="1.0"?>'
            f'<TrainingCenterDatabase xmlns="{ns}">'
            f'<Activities><Activity Sport="Biking">'
            f'<Id xmlns="{ns}">2024-06-01T07:00:00Z</Id>'
            f'</Activity></Activities>'
            f'</TrainingCenterDatabase>'
        ).encode()
        acts = parse_file("ride.tcx", content)
        assert acts == []

    def test_indoor_no_gps(self):
        """TCX indoor ride: points have no Position but still have time/HR."""
        tps = [
            {"time": "2024-06-01T07:00:00Z", "hr": 130},
            {"time": "2024-06-01T07:01:00Z", "hr": 140},
        ]
        acts = parse_file("ride.tcx", _tcx(trackpoints=tps))
        assert len(acts[0]["points"]) == 2
        assert acts[0]["points"][0][0] is None  # lat
        assert acts[0]["points"][0][1] is None  # lon
        assert acts[0]["distance"] == 0.0


# ---------------------------------------------------------------------------
# stream_from_file / points_from_file (tmp file round-trips)
# ---------------------------------------------------------------------------

class TestStreamAndPoints:

    def test_stream_from_gpx(self, tmp_path):
        p = tmp_path / "ride.gpx"
        p.write_bytes(_gpx())
        stream = stream_from_file(str(p))
        assert len(stream) == 2
        assert stream[0]["lat"] == pytest.approx(48.0)
        assert stream[0]["elapsed_secs"] == 0

    def test_stream_from_tcx(self, tmp_path):
        p = tmp_path / "ride.tcx"
        p.write_bytes(_tcx())
        stream = stream_from_file(str(p))
        assert len(stream) == 2
        assert stream[0]["hr"] == pytest.approx(140.0)

    def test_points_from_gpx_omits_no_gps(self, tmp_path):
        tps = [
            {"lat": 48.0, "lon": 11.0, "time": "2024-06-01T07:00:00Z"},
        ]
        p = tmp_path / "ride.gpx"
        p.write_bytes(_gpx(trackpoints=tps))
        pts = points_from_file(str(p))
        assert len(pts) == 1
        assert pts[0] == (pytest.approx(48.0), pytest.approx(11.0))

    def test_points_from_tcx_indoor_is_empty(self, tmp_path):
        tps = [{"time": "2024-06-01T07:00:00Z", "hr": 130}]
        p = tmp_path / "ride.tcx"
        p.write_bytes(_tcx(trackpoints=tps))
        pts = points_from_file(str(p))
        assert pts == []


# ---------------------------------------------------------------------------
# parse_file dispatch
# ---------------------------------------------------------------------------

class TestParseFileDispatch:

    def test_unsupported_extension_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            parse_file("ride.csv", b"garbage")

    def test_gpx_extension_dispatches(self):
        acts = parse_file("ride.gpx", _gpx())
        assert len(acts) == 1

    def test_tcx_extension_dispatches(self):
        acts = parse_file("ride.tcx", _tcx())
        assert len(acts) == 1

    def test_extension_case_insensitive(self):
        acts = parse_file("ride.GPX", _gpx())
        assert len(acts) == 1


# ---------------------------------------------------------------------------
# _file_id
# ---------------------------------------------------------------------------

class TestFileId:

    def test_deterministic(self):
        b = b"hello world"
        assert _file_id(b) == _file_id(b)

    def test_differs_for_different_content(self):
        assert _file_id(b"aaa") != _file_id(b"bbb")

    def test_is_integer(self):
        assert isinstance(_file_id(b"x"), int)

    def test_large_enough(self):
        # 13 hex digits → up to 16^13 ≈ 4.5×10^15, well above Strava IDs (~10^10)
        assert _file_id(b"x") < 16 ** 13
