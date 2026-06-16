"""
Performance benchmarks for map rendering, chart generation, and metrics computation.

Exercises both the real-world (outdoor GPS) and Zwift (virtual GPS) paths.
Tiles are cached under the project's .tile_cache after the first run.
Run with: pytest tests/test_performance.py -v -s
"""

import json
import os
import tempfile
import time

import pytest

FIXTURES   = os.path.join(os.path.dirname(__file__), "fixtures")
OUTDOOR    = os.path.join(FIXTURES, "18729043328.fit")   # Zurich road ride — GPS+HR, no power
ZWIFT      = os.path.join(FIXTURES, "18813819954.fit")   # Zwift Coast Crusher — GPS+HR+power

# Wall-clock thresholds (generous to accommodate cold tile cache on first run)
MAP_LIMIT_S     = 120.0
CHART_LIMIT_S   =  10.0
METRICS_LIMIT_S =   5.0


# ---------------------------------------------------------------------------
# Shared cfg — reuse the project tile cache so repeated runs are fast
# ---------------------------------------------------------------------------

def _make_cfg(out_dir, tile_cache=None):
    if tile_cache is None:
        tile_cache = os.path.join(os.path.dirname(__file__), "..", ".tile_cache")
    return {
        "database": {"path": "bikeodon.db"},
        "mastodon": {}, "strava": {},
        "map": {
            "output_dir": out_dir,
            "width": 600, "height": 338,
            "zoom_offset": -1, "max_tiles": 64,
            "padding": {"top": 0.06, "bottom": 0.28, "left": 0.06, "right": 0.06},
            "tiles": {
                "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                "user_agent": "Bikeodon-test/0.1",
                "size": 256,
                "cache_dir": tile_cache,
            },
            "route": {
                "color": "#FC4C02", "width": 4, "opacity": 0.9,
                "outline_color": "#000000", "outline_width": 1, "antialias_scale": 1,
            },
            "start_marker": {"enabled": True, "color": "#22CC44", "radius": 6,
                             "outline_color": "#ffffff", "outline_width": 2},
            "end_marker":   {"enabled": True, "color": "#CC2244", "radius": 6,
                             "outline_color": "#ffffff", "outline_width": 2},
            "watopia_enabled": True,
        },
        "charts": {
            "style": {
                "background_color": "#16161a", "text_color": "#dddddd",
                "grid_color": "#2e2e3a", "line_color": "#FC4C02",
                "power_line_color": "#4fc3f7",
            },
            "heart_rate": {
                "enabled": True, "max_hr": 185,
                "zones": [
                    {"name": "Z1", "max_pct": 60,  "color": "#5b9bd5"},
                    {"name": "Z2", "max_pct": 70,  "color": "#70ad47"},
                    {"name": "Z3", "max_pct": 80,  "color": "#ffc000"},
                    {"name": "Z4", "max_pct": 90,  "color": "#ff7043"},
                    {"name": "Z5", "max_pct": 100, "color": "#d32f2f"},
                ],
            },
            "power": {
                "enabled": True, "ftp": 250,
                "zones": [
                    {"name": "Z1", "max_pct": 55,  "color": "#5b9bd5"},
                    {"name": "Z2", "max_pct": 75,  "color": "#70ad47"},
                    {"name": "Z3", "max_pct": 90,  "color": "#ffc000"},
                    {"name": "Z4", "max_pct": 105, "color": "#ff7043"},
                    {"name": "Z5", "max_pct": 120, "color": "#d32f2f"},
                    {"name": "Z6", "max_pct": 150, "color": "#9c27b0"},
                    {"name": "Z7", "max_pct": 999, "color": "#424242"},
                ],
            },
        },
        "user": {"mastodon_handle": "", "stats": {"fields": ["distance"]}},
        "training": {"body_weight_kg": "", "hr_rest": ""},
        "stats_overlay": {"enabled": False, "background_color": "#000000",
                          "background_opacity": 0.55, "padding": 24, "gap": 36,
                          "font": {"size": 32, "color": "#ffffff"},
                          "icon": {"size": 32, "activity_icons": {}}},
    }


def _elapsed(label, t0):
    elapsed = time.perf_counter() - t0
    print(f"\n  {label}: {elapsed:.2f}s")
    return elapsed


# ---------------------------------------------------------------------------
# Map rendering
# ---------------------------------------------------------------------------

class TestMapRenderingPerformance:

    def _render(self, fit_path, activity_id, out_dir):
        from activity_parser import points_from_file
        from map_renderer import render_activity_map

        cfg = _make_cfg(out_dir)
        pts = points_from_file(fit_path)
        row = {"id": activity_id, "name": "Test", "sport_type": "Ride",
               "distance": 50000, "moving_time": 3600, "elapsed_time": 3600,
               "total_elevation_gain": 500, "average_speed": 13.0,
               "average_heartrate": None, "average_watts": None}

        t0 = time.perf_counter()
        img = render_activity_map(pts, row, cfg)
        elapsed = _elapsed(f"map render {os.path.basename(fit_path)}", t0)

        assert img is not None, "render_activity_map returned None"
        img.save(os.path.join(out_dir, f"{activity_id}.png"))
        return elapsed

    def test_outdoor_map_render_time(self, tmp_path):
        elapsed = self._render(OUTDOOR, 18729043328, str(tmp_path))
        assert elapsed < MAP_LIMIT_S, f"Outdoor map took {elapsed:.1f}s > {MAP_LIMIT_S}s"

    def test_zwift_map_render_time(self, tmp_path):
        elapsed = self._render(ZWIFT, 18813819954, str(tmp_path))
        assert elapsed < MAP_LIMIT_S, f"Zwift map took {elapsed:.1f}s > {MAP_LIMIT_S}s"


# ---------------------------------------------------------------------------
# Chart generation
# ---------------------------------------------------------------------------

class TestChartGenerationPerformance:

    def _generate(self, fit_path, activity_id, out_dir):
        from activity_parser import stream_from_file
        from charts import generate_charts

        cfg = _make_cfg(out_dir)
        stream = stream_from_file(fit_path)

        t0 = time.perf_counter()
        charts = generate_charts(activity_id, stream, cfg, out_dir)
        elapsed = _elapsed(f"chart gen {os.path.basename(fit_path)}", t0)

        assert isinstance(charts, list)
        return elapsed

    def test_outdoor_chart_gen_time(self, tmp_path):
        elapsed = self._generate(OUTDOOR, 18729043328, str(tmp_path))
        assert elapsed < CHART_LIMIT_S, f"Outdoor charts took {elapsed:.1f}s > {CHART_LIMIT_S}s"

    def test_zwift_chart_gen_time(self, tmp_path):
        elapsed = self._generate(ZWIFT, 18813819954, str(tmp_path))
        assert elapsed < CHART_LIMIT_S, f"Zwift charts took {elapsed:.1f}s > {CHART_LIMIT_S}s"


# ---------------------------------------------------------------------------
# Metrics computation (same path as the backfill)
# ---------------------------------------------------------------------------

class TestMetricsComputationPerformance:

    def _compute(self, fit_path, activity_id, tmp_path):
        import sqlite3
        from unittest.mock import patch

        from activity_parser import stream_from_file
        from database import init_db, upsert_activity
        import tasks

        db_path = str(tmp_path / "perf.db")
        init_db(db_path)

        # Seed hr_rest so hrTSS can be computed for HR-only rides
        from database import set_athlete_param
        set_athlete_param(db_path, 1, "rest_hr", 50, source="manual", date="2024-01-01")

        activity = {
            "id": activity_id, "name": "Perf test", "sport_type": "Ride",
            "start_date": "2024-01-01T10:00:00Z",
            "distance": 50000, "moving_time": 3600, "elapsed_time": 3600,
            "total_elevation_gain": 500, "average_speed": 13.0,
            "average_heartrate": 155, "max_heartrate": 175,
            "average_watts": 200, "max_watts": 600,
            "source_file": fit_path, "source_file_sha256": "abc",
            "source_file_type": "upload",
        }
        upsert_activity(db_path, activity, user_id=1, source="upload")

        stream = stream_from_file(fit_path)
        cfg = _make_cfg(str(tmp_path))

        row = sqlite3.connect(db_path)
        row.row_factory = sqlite3.Row
        row_data = row.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        row.close()

        t0 = time.perf_counter()
        with patch.object(tasks, "DB_PATH", db_path):
            tasks.process_activity(activity_id, 1, cfg, stream, row_data)
        elapsed = _elapsed(f"metrics {os.path.basename(fit_path)}", t0)

        # Verify results were written
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        result = conn.execute(
            "SELECT tss, hr_tss, np_watts, peak_power_json, metrics_computed_at"
            " FROM activities WHERE id=?", (activity_id,)
        ).fetchone()
        conn.close()

        assert result["metrics_computed_at"] is not None, "metrics_computed_at not set"

        return elapsed, dict(result)

    def test_outdoor_metrics_time(self, tmp_path):
        elapsed, result = self._compute(OUTDOOR, 18729043328, tmp_path)
        print(f"    hr_tss={result['hr_tss']}, np={result['np_watts']}")
        assert elapsed < METRICS_LIMIT_S, f"Outdoor metrics took {elapsed:.1f}s > {METRICS_LIMIT_S}s"
        # Outdoor ride has HR but no power — expect hrTSS, no power TSS
        assert result["hr_tss"] is not None, "Expected hrTSS for HR-only outdoor ride"
        assert result["tss"] is None, "Did not expect power TSS for ride without power meter"

    def test_zwift_metrics_time(self, tmp_path):
        elapsed, result = self._compute(ZWIFT, 18813819954, tmp_path)
        print(f"    tss={result['tss']}, np={result['np_watts']}")
        assert elapsed < METRICS_LIMIT_S, f"Zwift metrics took {elapsed:.1f}s > {METRICS_LIMIT_S}s"
        # Zwift ride has power — expect TSS and NP
        assert result["tss"] is not None, "Expected power TSS for Zwift ride"
        assert result["np_watts"] is not None, "Expected NP for Zwift ride"

    def test_metrics_peak_powers_computed(self, tmp_path):
        _, result = self._compute(ZWIFT, 18813819954, tmp_path)
        peaks = json.loads(result["peak_power_json"])
        assert len(peaks) > 10, f"Expected dense MMP curve, got {len(peaks)} entries"
        assert any(v is not None for v in peaks.values()), "All MMP entries are None"
