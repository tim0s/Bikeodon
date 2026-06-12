"""
Tests for upsert_activity metric-preservation behaviour.

Key invariants:
  - A rename (name-only change) must preserve all computed metrics.
  - A physical edit (sport_type, time, distance, power, HR) must null
    metrics_computed_at so the daemon reprocesses.
  - Social/render fields (posted_at, map_rendered_at, ap_posted_at, etc.)
    are never overwritten by an upsert.
"""

import os
import pytest
import yaml
from datetime import datetime, timezone


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("upsert")
    path = str(tmp / "test.db")
    cfg = {
        "database": {"path": path},
        "daemon":   {"interval_minutes": 15},
        "map":      {"output_dir": str(tmp / "output"), "tiles": {}},
    }
    cfg_path = str(tmp / "config.yaml")
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)
    os.environ["BIKEODON_CONFIG"] = cfg_path
    os.environ["FLASK_SECRET_KEY"] = "test-secret"

    import importlib
    import database
    importlib.reload(database)
    database.init_db(path)
    return path


@pytest.fixture(scope="module")
def uid(db_path):
    from database import create_user
    from werkzeug.security import generate_password_hash
    return create_user(db_path, "upsert_tester", generate_password_hash("pw"))


_BASE = {
    "id": 99001,
    "name": "Morning Ride",
    "sport_type": "Ride",
    "start_date": "2026-01-10T07:00:00Z",
    "distance": 30000.0,
    "moving_time": 3600,
    "elapsed_time": 3700,
    "average_watts": 200.0,
    "average_heartrate": 140.0,
}


def _insert(db_path, uid, overrides=None):
    from database import upsert_activity
    data = {**_BASE, **(overrides or {})}
    upsert_activity(db_path, data, user_id=uid)


def _row(db_path, uid):
    from database import _conn
    conn = _conn(db_path)
    try:
        return conn.execute(
            "SELECT * FROM activities WHERE id=? AND user_id=?",
            (_BASE["id"], uid),
        ).fetchone()
    finally:
        conn.close()


def _seed_metrics(db_path, uid):
    """Manually write fake computed metrics so we can check they survive."""
    from database import _conn
    conn = _conn(db_path)
    try:
        conn.execute(
            "UPDATE activities SET tss=80.0, np_watts=210.0, trimp=55.0,"
            " hr_tss=75.0, metrics_computed_at=?, peak_power_json=?,"
            " posted_at=?, ap_posted_at=?, map_rendered_at=?"
            " WHERE id=? AND user_id=?",
            (
                "2026-01-10T08:00:00Z",
                '{"5s": 350}',
                "2026-01-10T09:00:00Z",
                "2026-01-10T09:01:00Z",
                "2026-01-10T08:30:00Z",
                _BASE["id"], uid,
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestRenamePreservesMetrics:

    def test_rename_keeps_tss(self, db_path, uid):
        _insert(db_path, uid)
        _seed_metrics(db_path, uid)
        _insert(db_path, uid, {"name": "Evening Ride"})
        assert _row(db_path, uid)["tss"] == 80.0

    def test_rename_keeps_np_watts(self, db_path, uid):
        assert _row(db_path, uid)["np_watts"] == 210.0

    def test_rename_keeps_trimp(self, db_path, uid):
        assert _row(db_path, uid)["trimp"] == 55.0

    def test_rename_keeps_hr_tss(self, db_path, uid):
        assert _row(db_path, uid)["hr_tss"] == 75.0

    def test_rename_keeps_peak_power_json(self, db_path, uid):
        assert _row(db_path, uid)["peak_power_json"] == '{"5s": 350}'

    def test_rename_keeps_metrics_computed_at(self, db_path, uid):
        assert _row(db_path, uid)["metrics_computed_at"] == "2026-01-10T08:00:00Z"

    def test_rename_keeps_posted_at(self, db_path, uid):
        assert _row(db_path, uid)["posted_at"] == "2026-01-10T09:00:00Z"

    def test_rename_keeps_ap_posted_at(self, db_path, uid):
        assert _row(db_path, uid)["ap_posted_at"] == "2026-01-10T09:01:00Z"

    def test_rename_keeps_map_rendered_at(self, db_path, uid):
        assert _row(db_path, uid)["map_rendered_at"] == "2026-01-10T08:30:00Z"

    def test_name_is_updated(self, db_path, uid):
        assert _row(db_path, uid)["name"] == "Evening Ride"


class TestPhysicalEditInvalidatesMetrics:

    def _setup(self, db_path, uid):
        _insert(db_path, uid)
        _seed_metrics(db_path, uid)

    def test_sport_type_change_nulls_metrics_computed_at(self, db_path, uid):
        self._setup(db_path, uid)
        _insert(db_path, uid, {"sport_type": "VirtualRide"})
        assert _row(db_path, uid)["metrics_computed_at"] is None

    def test_elapsed_time_change_nulls_metrics_computed_at(self, db_path, uid):
        self._setup(db_path, uid)
        _insert(db_path, uid, {"elapsed_time": 4000})
        assert _row(db_path, uid)["metrics_computed_at"] is None

    def test_moving_time_change_nulls_metrics_computed_at(self, db_path, uid):
        self._setup(db_path, uid)
        _insert(db_path, uid, {"moving_time": 3500})
        assert _row(db_path, uid)["metrics_computed_at"] is None

    def test_distance_change_nulls_metrics_computed_at(self, db_path, uid):
        self._setup(db_path, uid)
        _insert(db_path, uid, {"distance": 35000.0})
        assert _row(db_path, uid)["metrics_computed_at"] is None

    def test_avg_watts_change_nulls_metrics_computed_at(self, db_path, uid):
        self._setup(db_path, uid)
        _insert(db_path, uid, {"average_watts": 220.0})
        assert _row(db_path, uid)["metrics_computed_at"] is None

    def test_avg_hr_change_nulls_metrics_computed_at(self, db_path, uid):
        self._setup(db_path, uid)
        _insert(db_path, uid, {"average_heartrate": 150.0})
        assert _row(db_path, uid)["metrics_computed_at"] is None

    def test_physical_edit_preserves_posted_at(self, db_path, uid):
        """Social fields must survive even when metrics are invalidated."""
        self._setup(db_path, uid)
        _insert(db_path, uid, {"distance": 40000.0})
        assert _row(db_path, uid)["posted_at"] == "2026-01-10T09:00:00Z"

    def test_physical_edit_preserves_ap_posted_at(self, db_path, uid):
        self._setup(db_path, uid)
        _insert(db_path, uid, {"distance": 40000.0})
        assert _row(db_path, uid)["ap_posted_at"] == "2026-01-10T09:01:00Z"


class TestFirstInsert:

    def test_new_activity_has_null_metrics_computed_at(self, db_path, uid):
        from database import upsert_activity
        upsert_activity(db_path, {
            "id": 99002, "name": "New Ride", "sport_type": "Ride",
            "start_date": "2026-02-01T08:00:00Z",
        }, user_id=uid)
        from database import _conn
        conn = _conn(db_path)
        try:
            row = conn.execute(
                "SELECT metrics_computed_at FROM activities WHERE id=99002 AND user_id=?",
                (uid,),
            ).fetchone()
        finally:
            conn.close()
        assert row["metrics_computed_at"] is None
