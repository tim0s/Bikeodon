"""
Tests for map and chart rendering using real FIT fixture files.

These tests exercise the full rendering pipeline end-to-end using files
copied from production. The map tests for real outdoor rides hit the OSM
tile server (tiles are cached under .tile_cache after the first run).
Zwift/Watopia rides use the local static map and never touch the network.
"""

import os
import tempfile

import pytest

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
OUTDOOR_FIT  = os.path.join(FIXTURES, "18729043328.fit")   # Morning Ride, Zurich — GPS+HR, no power
WATOPIA_FIT1 = os.path.join(FIXTURES, "18813819954.fit")   # Zwift Coast Crusher — GPS+HR+power
WATOPIA_FIT2 = os.path.join(FIXTURES, "18853021065.fit")   # Zwift Hilltop Hustle — GPS+HR+power


# ---------------------------------------------------------------------------
# Minimal config fixture — mirrors the structure produced by load_user_config()
# ---------------------------------------------------------------------------

_DEFAULT_HR_ZONES = [
    {"name": "Z1 Recovery",   "max_pct": 60,  "color": "#5b9bd5"},
    {"name": "Z2 Endurance",  "max_pct": 70,  "color": "#70ad47"},
    {"name": "Z3 Tempo",      "max_pct": 80,  "color": "#ffc000"},
    {"name": "Z4 Threshold",  "max_pct": 90,  "color": "#ff7043"},
    {"name": "Z5 VO2 Max",    "max_pct": 100, "color": "#d32f2f"},
]

_DEFAULT_POWER_ZONES = [
    {"name": "Z1 Recovery",      "max_pct": 55,  "color": "#5b9bd5"},
    {"name": "Z2 Endurance",     "max_pct": 75,  "color": "#70ad47"},
    {"name": "Z3 Tempo",         "max_pct": 90,  "color": "#ffc000"},
    {"name": "Z4 Threshold",     "max_pct": 105, "color": "#ff7043"},
    {"name": "Z5 VO2 Max",       "max_pct": 120, "color": "#d32f2f"},
    {"name": "Z6 Anaerobic",     "max_pct": 150, "color": "#9c27b0"},
    {"name": "Z7 Neuromuscular", "max_pct": 999, "color": "#424242"},
]


def _make_cfg(tmp_path, hr_zones=None, power_zones=None):
    """Build a minimal but complete cfg dict for rendering tests."""
    cache_dir = str(tmp_path / ".tile_cache")
    return {
        "database": {"path": "bikeodon.db"},
        "daemon": {},
        "mastodon": {},
        "strava": {},
        "map": {
            "output_dir": str(tmp_path / "output"),
            "width": 600,
            "height": 338,
            "zoom_offset": -1,
            "max_tiles": 64,
            "padding": {"top": 0.06, "bottom": 0.28, "left": 0.06, "right": 0.06},
            "tiles": {
                "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                "user_agent": "Bikeodon-test/0.1",
                "size": 256,
                "cache_dir": cache_dir,
            },
            "route": {
                "color": "#FC4C02",
                "width": 4,
                "opacity": 0.9,
                "outline_color": "#000000",
                "outline_width": 1,
                "antialias_scale": 1,
            },
            "start_marker": {
                "enabled": True, "color": "#22CC44", "radius": 6,
                "outline_color": "#ffffff", "outline_width": 2,
            },
            "end_marker": {
                "enabled": True, "color": "#CC2244", "radius": 6,
                "outline_color": "#ffffff", "outline_width": 2,
            },
            "watopia_enabled": True,
        },
        "charts": {
            "style": {
                "background_color": "#16161a",
                "text_color": "#dddddd",
                "grid_color": "#2e2e3a",
                "line_color": "#FC4C02",
                "power_line_color": "#4fc3f7",
            },
            "heart_rate": {
                "enabled": True,
                "max_hr": 185,
                "zones": hr_zones if hr_zones is not None else _DEFAULT_HR_ZONES,
            },
            "power": {
                "enabled": True,
                "ftp": 250,
                "zones": power_zones if power_zones is not None else _DEFAULT_POWER_ZONES,
            },
        },
        "user": {"mastodon_handle": "", "stats": {"fields": ["distance"]}},
        "training": {"body_weight_kg": "", "hr_rest": ""},
        "stats_overlay": {
            "enabled": False,
            "background_color": "#000000",
            "background_opacity": 0.55,
            "padding": 24,
            "gap": 36,
            "font": {"size": 32, "color": "#ffffff"},
            "icon": {
                "size": 32,
                "activity_icons": {
                    "Ride": "🚴", "VirtualRide": "🚴",
                    "Run": "🏃", "default": "🏅",
                },
            },
        },
    }


@pytest.fixture
def cfg(tmp_path):
    """Config with default zones — represents a normal configured user."""
    return _make_cfg(tmp_path)


@pytest.fixture
def cfg_no_zones(tmp_path):
    """Config with empty zone lists — represents a brand-new user on their first ride."""
    return _make_cfg(tmp_path, hr_zones=[], power_zones=[])


# ---------------------------------------------------------------------------
# activity_parser helpers
# ---------------------------------------------------------------------------

class TestStreamFromFile:
    def test_outdoor_returns_stream_with_gps_and_hr(self):
        from activity_parser import stream_from_file
        stream = stream_from_file(OUTDOOR_FIT)
        assert len(stream) > 100
        sample = stream[0]
        assert set(sample.keys()) == {"lat", "lon", "ele", "hr", "power", "elapsed_secs"}
        assert sample["lat"] is not None and 47.0 < sample["lat"] < 48.0
        assert sample["lon"] is not None and 8.0 < sample["lon"] < 9.0
        assert sample["hr"] is not None

    def test_outdoor_no_power(self):
        from activity_parser import stream_from_file
        stream = stream_from_file(OUTDOOR_FIT)
        assert all(s["power"] is None for s in stream)

    def test_zwift_has_power(self):
        from activity_parser import stream_from_file
        stream = stream_from_file(WATOPIA_FIT1)
        watts = [s["power"] for s in stream if s["power"] is not None]
        assert len(watts) > 100

    def test_zwift_watopia_coordinates(self):
        from activity_parser import stream_from_file
        stream = stream_from_file(WATOPIA_FIT1)
        lats = [s["lat"] for s in stream if s["lat"] is not None]
        assert all(-11.8 < lat < -11.5 for lat in lats)

    def test_elapsed_secs_monotonic(self):
        from activity_parser import stream_from_file
        stream = stream_from_file(OUTDOOR_FIT)
        secs = [s["elapsed_secs"] for s in stream if s["elapsed_secs"] is not None]
        assert secs == sorted(secs)
        assert secs[0] == 0


class TestPointsFromFile:
    def test_outdoor_returns_lat_lon_tuples(self):
        from activity_parser import points_from_file
        pts = points_from_file(OUTDOOR_FIT)
        assert len(pts) > 100
        assert all(len(p) == 2 for p in pts)
        assert all(47.0 < p[0] < 48.0 for p in pts)

    def test_omits_none_coordinates(self):
        from activity_parser import points_from_file
        pts = points_from_file(OUTDOOR_FIT)
        assert all(p[0] is not None and p[1] is not None for p in pts)

    def test_zwift_watopia_points(self):
        from activity_parser import points_from_file
        pts = points_from_file(WATOPIA_FIT1)
        assert len(pts) > 100
        assert all(-11.8 < p[0] < -11.5 for p in pts)


# ---------------------------------------------------------------------------
# Map rendering
# ---------------------------------------------------------------------------

class TestRenderActivityMap:
    def test_outdoor_renders_to_pil_image(self, cfg):
        from activity_parser import points_from_file
        from map_renderer import render_activity_map
        pts = points_from_file(OUTDOOR_FIT)
        activity = {"sport_type": "Ride", "name": "Morning Ride", "distance": 30000,
                    "total_elevation_gain": 200, "moving_time": 3600}
        img = render_activity_map(pts, activity, cfg)
        assert img is not None
        assert img.size == (cfg["map"]["width"], cfg["map"]["height"])

    def test_outdoor_image_is_not_blank(self, cfg):
        from activity_parser import points_from_file
        from map_renderer import render_activity_map
        pts = points_from_file(OUTDOOR_FIT)
        activity = {"sport_type": "Ride", "name": "Morning Ride", "distance": 30000,
                    "total_elevation_gain": 200, "moving_time": 3600}
        img = render_activity_map(pts, activity, cfg)
        pixels = list(img.convert("RGB").getdata())
        unique = set(pixels)
        assert len(unique) > 10

    def test_watopia_uses_static_map(self, cfg, tmp_path, capsys):
        from activity_parser import points_from_file
        from map_renderer import render_activity_map
        pts = points_from_file(WATOPIA_FIT1)
        activity = {"sport_type": "VirtualRide", "name": "Coast Crusher", "distance": 40000,
                    "total_elevation_gain": 500, "moving_time": 4200}
        img = render_activity_map(pts, activity, cfg)
        assert img is not None
        captured = capsys.readouterr()
        assert "Watopia" in captured.out

    def test_empty_points_returns_none(self, cfg):
        from map_renderer import render_activity_map
        result = render_activity_map([], {}, cfg)
        assert result is None


# ---------------------------------------------------------------------------
# Chart rendering
# ---------------------------------------------------------------------------

class TestGenerateCharts:
    def test_outdoor_generates_hr_chart(self, cfg, tmp_path):
        from activity_parser import stream_from_file
        from charts import generate_charts
        stream = stream_from_file(OUTDOOR_FIT)
        out = str(tmp_path / "charts")
        paths = generate_charts(18729043328, stream, cfg, out)
        hr_paths = [p for p in paths if "_hr" in p]
        assert hr_paths, "Expected an HR chart to be generated"
        assert os.path.exists(hr_paths[0])

    def test_outdoor_no_power_chart(self, cfg, tmp_path):
        from activity_parser import stream_from_file
        from charts import generate_charts
        stream = stream_from_file(OUTDOOR_FIT)
        out = str(tmp_path / "charts")
        paths = generate_charts(18729043328, stream, cfg, out)
        power_paths = [p for p in paths if "_power" in os.path.basename(p)]
        assert not power_paths, "No power chart expected for a ride without power data"

    def test_zwift_generates_power_chart(self, cfg, tmp_path):
        from activity_parser import stream_from_file
        from charts import generate_charts
        stream = stream_from_file(WATOPIA_FIT1)
        out = str(tmp_path / "charts")
        paths = generate_charts(18813819954, stream, cfg, out)
        power_paths = [p for p in paths if "_power" in p]
        assert power_paths, "Expected a power chart for a Zwift ride with watts"
        assert os.path.exists(power_paths[0])

    def test_zwift_chart_is_valid_image(self, cfg, tmp_path):
        from activity_parser import stream_from_file
        from charts import generate_charts
        stream = stream_from_file(WATOPIA_FIT2)
        out = str(tmp_path / "charts")
        paths = generate_charts(18853021065, stream, cfg, out)
        assert paths, "Expected at least one chart"
        with open(paths[0], "rb") as f:
            magic = f.read(4)
        is_png  = magic[:4] == b"\x89PNG"
        is_jpeg = magic[:2] == b"\xff\xd8"
        assert is_png or is_jpeg, "Chart file is not a valid PNG or JPEG"

    def test_empty_stream_produces_no_charts(self, cfg, tmp_path):
        from charts import generate_charts
        out = str(tmp_path / "charts")
        paths = generate_charts(99999, [], cfg, out)
        assert paths == []

    def test_no_zones_hr_chart_does_not_crash(self, cfg_no_zones, tmp_path):
        """First ride — user has no zones configured at all, not even autodetected."""
        from activity_parser import stream_from_file
        from charts import generate_charts
        stream = stream_from_file(OUTDOOR_FIT)
        out = str(tmp_path / "charts")
        # Must not raise; we just want a clean result (chart may or may not render)
        paths = generate_charts(18729043328, stream, cfg_no_zones, out)
        hr_paths = [p for p in paths if "_hr" in p]
        assert hr_paths, "HR chart should still render even without configured zones"

    def test_no_zones_power_chart_does_not_crash(self, cfg_no_zones, tmp_path):
        """First Zwift ride — user has no zones configured at all."""
        from activity_parser import stream_from_file
        from charts import generate_charts
        stream = stream_from_file(WATOPIA_FIT1)
        out = str(tmp_path / "charts")
        paths = generate_charts(18813819954, stream, cfg_no_zones, out)
        power_paths = [p for p in paths if "_power" in p]
        assert power_paths, "Power chart should still render even without configured zones"


class TestImageFormat:
    """Rendering pipeline honours the img_format parameter."""

    def test_generate_charts_jpeg_default(self, cfg, tmp_path):
        from activity_parser import stream_from_file
        from charts import generate_charts
        stream = stream_from_file(WATOPIA_FIT1)
        out = str(tmp_path / "charts")
        paths = generate_charts(18813819954, stream, cfg, out)
        assert all(p.endswith(".jpg") for p in paths), "Default format should produce .jpg files"
        for p in paths:
            with open(p, "rb") as f:
                assert f.read(2) == b"\xff\xd8", f"{p} is not a valid JPEG"

    def test_generate_charts_png_format(self, cfg, tmp_path):
        from activity_parser import stream_from_file
        from charts import generate_charts
        stream = stream_from_file(WATOPIA_FIT1)
        out = str(tmp_path / "charts")
        paths = generate_charts(18813819954, stream, cfg, out, img_format="png")
        assert all(p.endswith(".png") for p in paths), "png format should produce .png files"
        for p in paths:
            with open(p, "rb") as f:
                assert f.read(4) == b"\x89PNG", f"{p} is not a valid PNG"

    def test_render_map_jpeg(self, cfg, tmp_path):
        from activity_parser import points_from_file
        from map_renderer import render_activity_map
        from PIL import Image
        pts = points_from_file(OUTDOOR_FIT)
        img = render_activity_map(pts, {"sport_type": "Ride"}, cfg)
        assert img is not None
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        out = str(tmp_path / "map.jpg")
        img.save(out, "JPEG", quality=85)
        with open(out, "rb") as f:
            assert f.read(2) == b"\xff\xd8"

    def test_render_map_png(self, cfg, tmp_path):
        from activity_parser import points_from_file
        from map_renderer import render_activity_map
        pts = points_from_file(OUTDOOR_FIT)
        img = render_activity_map(pts, {"sport_type": "Ride"}, cfg)
        assert img is not None
        out = str(tmp_path / "map.png")
        img.save(out, "PNG")
        with open(out, "rb") as f:
            assert f.read(4) == b"\x89PNG"
