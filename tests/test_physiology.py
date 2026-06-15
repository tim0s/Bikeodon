"""
Tests for the athlete_params / power_bests tables and the physiology pipeline
in tasks.py (_update_physiology, _estimate_derived_params, process_activity).
"""

import itertools
import json
import os
import sqlite3
import pytest
import yaml
from unittest.mock import patch
from werkzeug.security import generate_password_hash


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("physiology")
    cfg = {
        "database": {"path": str(tmp / "test.db")},
        "map":      {"output_dir": str(tmp / "output"), "tiles": {}},
    }
    cfg_path = str(tmp / "config.yaml")
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)
    os.environ["BIKEODON_CONFIG"] = cfg_path
    os.environ["FLASK_SECRET_KEY"] = "test-secret"

    import importlib, app as app_module
    importlib.reload(app_module)
    app_module.app.config["TESTING"] = True
    app_module.app.config["SERVER_NAME"] = "bikeodon.test"
    yield app_module.app
    os.environ.pop("BIKEODON_CONFIG", None)


@pytest.fixture(scope="module")
def db_path(app):
    import app as app_module
    return app_module.DB_PATH


_uid_counter = 0

@pytest.fixture()
def uid(db_path):
    global _uid_counter
    _uid_counter += 1
    from database import create_user
    return create_user(db_path, f"physuser{_uid_counter}",
                       generate_password_hash("pw"))


def _insert_activity(db_path, activity_id, uid, start_date,
                     max_hr=None, moving_time=3600):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO activities"
        " (id, user_id, start_date, moving_time, max_heartrate, name)"
        " VALUES (?,?,?,?,?,?)",
        (activity_id, uid, start_date, moving_time, max_hr, f"Act {activity_id}"),
    )
    conn.commit()
    conn.close()
    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    row = conn2.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
    conn2.close()
    return row


def _make_stream(peak_5min_watts=None, hr=None):
    """Minimal stream: 400s at constant power/HR."""
    pts = []
    for i in range(400):
        pt = {"elapsed_secs": i}
        if peak_5min_watts:
            pt["power"] = peak_5min_watts
        if hr:
            pt["hr"] = hr
        pts.append(pt)
    return pts


# ---------------------------------------------------------------------------
# athlete_params helpers
# ---------------------------------------------------------------------------

class TestGetSetAthleteParam:

    def test_returns_none_when_empty(self, db_path, uid):
        from database import get_athlete_param
        assert get_athlete_param(db_path, uid, "weight_kg") is None

    def test_set_and_get_current(self, db_path, uid):
        from database import set_athlete_param, get_athlete_param
        set_athlete_param(db_path, uid, "weight_kg", 75.0, source="manual")
        assert get_athlete_param(db_path, uid, "weight_kg") == 75.0

    def test_as_of_returns_correct_historical_value(self, db_path, uid):
        from database import set_athlete_param, get_athlete_param
        set_athlete_param(db_path, uid, "rest_hr", 50.0, source="manual", date="2025-01-01")
        set_athlete_param(db_path, uid, "rest_hr", 48.0, source="manual", date="2025-06-01")
        assert get_athlete_param(db_path, uid, "rest_hr", as_of="2025-03-01") == 50.0
        assert get_athlete_param(db_path, uid, "rest_hr", as_of="2025-06-01") == 48.0
        assert get_athlete_param(db_path, uid, "rest_hr", as_of="2024-12-31") is None

    def test_skips_insert_when_value_unchanged_as_of_date(self, db_path, uid):
        from database import set_athlete_param, get_athlete_param, _conn
        set_athlete_param(db_path, uid, "max_hr", 185.0, source="derived", date="2025-01-01")
        before = _conn(db_path).execute(
            "SELECT COUNT(*) FROM athlete_params WHERE user_id=? AND parameter='max_hr'",
            (uid,),
        ).fetchone()[0]
        # Same value as_of same date — should not insert
        result = set_athlete_param(db_path, uid, "max_hr", 185.0,
                                   source="derived", date="2025-01-01")
        after = _conn(db_path).execute(
            "SELECT COUNT(*) FROM athlete_params WHERE user_id=? AND parameter='max_hr'",
            (uid,),
        ).fetchone()[0]
        assert result is False
        assert after == before

    def test_inserts_when_value_differs(self, db_path, uid):
        from database import set_athlete_param, get_athlete_param
        set_athlete_param(db_path, uid, "ftp", 200.0, source="derived", date="2025-01-01")
        result = set_athlete_param(db_path, uid, "ftp", 210.0, source="derived", date="2025-03-01")
        assert result is True
        assert get_athlete_param(db_path, uid, "ftp") == 210.0


# ---------------------------------------------------------------------------
# power_bests helpers
# ---------------------------------------------------------------------------

class TestSetPowerBest:

    def test_first_entry_is_always_inserted(self, db_path, uid):
        _insert_activity(db_path, 9000001, uid, "2025-01-01")
        from database import set_power_best, get_power_best
        assert set_power_best(db_path, uid, "5min", 250.0, 9000001, "2025-01-01") is True
        assert get_power_best(db_path, uid, "5min")["power_watts"] == 250.0

    def test_higher_value_on_later_date_inserts(self, db_path, uid):
        _insert_activity(db_path, 9000002, uid, "2025-01-01")
        _insert_activity(db_path, 9000003, uid, "2025-06-01")
        from database import set_power_best, get_power_best
        set_power_best(db_path, uid, "20min", 200.0, 9000002, "2025-01-01")
        assert set_power_best(db_path, uid, "20min", 220.0, 9000003, "2025-06-01") is True

    def test_lower_value_does_not_insert(self, db_path, uid):
        _insert_activity(db_path, 9000004, uid, "2025-01-01")
        _insert_activity(db_path, 9000005, uid, "2025-06-01")
        from database import set_power_best, get_power_best
        set_power_best(db_path, uid, "1min", 350.0, 9000004, "2025-01-01")
        assert set_power_best(db_path, uid, "1min", 300.0, 9000005, "2025-06-01") is False

    def test_value_between_two_entries_inserts_correctly(self, db_path, uid):
        """Processing order independence: a 3 Jan PR between 1 Jan and 5 Jan entries."""
        _insert_activity(db_path, 9000010, uid, "2025-01-01")
        _insert_activity(db_path, 9000011, uid, "2025-01-03")
        _insert_activity(db_path, 9000012, uid, "2025-01-05")
        from database import set_power_best, get_power_best
        # Simulate: 1 Jan best = 100W, 5 Jan best = 200W already in DB
        set_power_best(db_path, uid, "30min", 100.0, 9000010, "2025-01-01")
        set_power_best(db_path, uid, "30min", 200.0, 9000012, "2025-01-05")
        # Now process 3 Jan with 150W — should insert because 150 > 100 (best as_of 2025-01-03)
        inserted = set_power_best(db_path, uid, "30min", 150.0, 9000011, "2025-01-03")
        assert inserted is True
        # Best as_of 2025-01-03 should now be 150W
        assert get_power_best(db_path, uid, "30min", as_of="2025-01-03")["power_watts"] == 150.0
        # Best overall (5 Jan entry) unchanged
        assert get_power_best(db_path, uid, "30min")["power_watts"] == 200.0

    def test_value_not_better_than_as_of_does_not_insert(self, db_path, uid):
        """A weak sandwiched activity should not produce a new entry."""
        _insert_activity(db_path, 9000020, uid, "2025-01-01")
        _insert_activity(db_path, 9000021, uid, "2025-01-03")
        from database import set_power_best
        set_power_best(db_path, uid, "2min", 400.0, 9000020, "2025-01-01")
        assert set_power_best(db_path, uid, "2min", 350.0, 9000021, "2025-01-03") is False


# ---------------------------------------------------------------------------
# get_mmp_as_of
# ---------------------------------------------------------------------------

class TestGetMmpAsOf:

    def test_returns_empty_when_no_bests(self, db_path, uid):
        from database import get_mmp_as_of
        assert get_mmp_as_of(db_path, uid) == {}

    def test_returns_best_per_duration(self, db_path, uid):
        _insert_activity(db_path, 9000030, uid, "2025-01-01")
        _insert_activity(db_path, 9000031, uid, "2025-03-01")
        from database import set_power_best, get_mmp_as_of
        set_power_best(db_path, uid, "5min", 250.0, 9000030, "2025-01-01")
        set_power_best(db_path, uid, "5min", 270.0, 9000031, "2025-03-01")
        set_power_best(db_path, uid, "20min", 210.0, 9000030, "2025-01-01")
        mmp = get_mmp_as_of(db_path, uid)
        assert mmp["5min"] == 270.0
        assert mmp["20min"] == 210.0

    def test_as_of_filters_future_entries(self, db_path, uid):
        _insert_activity(db_path, 9000035, uid, "2025-01-01")
        _insert_activity(db_path, 9000036, uid, "2025-06-01")
        from database import set_power_best, get_mmp_as_of
        set_power_best(db_path, uid, "10min", 230.0, 9000035, "2025-01-01")
        set_power_best(db_path, uid, "10min", 260.0, 9000036, "2025-06-01")
        mmp = get_mmp_as_of(db_path, uid, as_of="2025-03-01")
        assert mmp["10min"] == 230.0


# ---------------------------------------------------------------------------
# _update_physiology
# ---------------------------------------------------------------------------

class TestUpdatePhysiology:

    def _run(self, app, db_path, uid, activity_id, date, peaks, max_hr=None):
        row = {"max_heartrate": max_hr}
        with patch("tasks.DB_PATH", db_path):
            from tasks import _update_physiology
            return _update_physiology(uid, activity_id, date, peaks, row)

    def test_power_best_recorded_and_breakthrough_returned(self, app, db_path, uid):
        _insert_activity(db_path, 9001001, uid, "2025-01-01")
        bts = self._run(app, db_path, uid, 9001001, "2025-01-01",
                        {"5min": 255.0, "20min": 210.0})
        labels = [b["label"] for b in bts if b["type"] == "mmp"]
        assert "5min" in labels
        assert "20min" in labels

    def test_no_breakthrough_when_not_a_new_best(self, app, db_path, uid):
        _insert_activity(db_path, 9001002, uid, "2025-01-01")
        _insert_activity(db_path, 9001003, uid, "2025-06-01")
        from database import set_power_best
        set_power_best(db_path, uid, "5min", 300.0, 9001002, "2025-01-01")
        bts = self._run(app, db_path, uid, 9001003, "2025-06-01", {"5min": 280.0})
        assert not any(b["type"] == "mmp" and b["label"] == "5min" for b in bts)

    def test_max_hr_recorded(self, app, db_path, uid):
        _insert_activity(db_path, 9001004, uid, "2025-01-01", max_hr=182)
        self._run(app, db_path, uid, 9001004, "2025-01-01", None, max_hr=182)
        from database import get_athlete_param
        assert get_athlete_param(db_path, uid, "max_hr") == 182

    def test_max_hr_above_220_ignored(self, app, db_path, uid):
        _insert_activity(db_path, 9001005, uid, "2025-01-01", max_hr=180)
        _insert_activity(db_path, 9001006, uid, "2025-06-01", max_hr=225)
        from database import set_athlete_param, get_athlete_param
        set_athlete_param(db_path, uid, "max_hr", 180.0, source="derived",
                          activity_id=9001005, date="2025-01-01")
        self._run(app, db_path, uid, 9001006, "2025-06-01", None, max_hr=225)
        # 225 exceeds the hard cap of 220 and should be rejected
        assert get_athlete_param(db_path, uid, "max_hr") == 180.0

    def test_sandwich_breakthrough_detected_correctly(self, app, db_path, uid):
        """Processing a sandwiched activity detects breakthrough vs as_of value."""
        _insert_activity(db_path, 9001010, uid, "2025-01-01")
        _insert_activity(db_path, 9001011, uid, "2025-03-01")
        _insert_activity(db_path, 9001012, uid, "2025-06-01")
        from database import set_power_best
        set_power_best(db_path, uid, "20min", 200.0, 9001010, "2025-01-01")
        set_power_best(db_path, uid, "20min", 250.0, 9001012, "2025-06-01")
        # Process March activity with 220W — new best as_of March (prev was 200W)
        bts = self._run(app, db_path, uid, 9001011, "2025-03-01", {"20min": 220.0})
        mmp_bts = [b for b in bts if b["type"] == "mmp" and b["label"] == "20min"]
        assert len(mmp_bts) == 1
        assert mmp_bts[0]["prev"] == 200


# ---------------------------------------------------------------------------
# _estimate_derived_params
# ---------------------------------------------------------------------------

class TestEstimateDerivedParams:

    def test_cp_and_ftp_written_from_power_bests(self, app, db_path, uid):
        for aid, date, w5, w20 in [
            (9002001, "2025-01-01", 300, 250),
            (9002002, "2025-03-01", 320, 260),
        ]:
            _insert_activity(db_path, aid, uid, date)
            from database import set_power_best
            set_power_best(db_path, uid, "5min",  w5,  aid, date)
            set_power_best(db_path, uid, "20min", w20, aid, date)

        with patch("tasks.DB_PATH", db_path):
            from tasks import _estimate_derived_params
            result = _estimate_derived_params(uid, 9002002, "2025-03-01")

        from database import get_athlete_param
        assert result["cp"] is not None
        assert result["ftp"] is not None
        assert get_athlete_param(db_path, uid, "cp_watts") is not None
        assert get_athlete_param(db_path, uid, "ftp") is not None

    def test_ftp_is_95pct_of_20min(self, app, db_path, uid):
        _insert_activity(db_path, 9002010, uid, "2025-05-01")
        from database import set_power_best
        set_power_best(db_path, uid, "20min", 200.0, 9002010, "2025-05-01")
        # Need at least 2 points for CP fit — add another duration
        set_power_best(db_path, uid, "5min",  240.0, 9002010, "2025-05-01")

        with patch("tasks.DB_PATH", db_path):
            from tasks import _estimate_derived_params
            result = _estimate_derived_params(uid, 9002010, "2025-05-01")

        assert result["ftp"] == pytest.approx(200.0 * 0.95, abs=1.0)

    def test_cp_changed_flag(self, app, db_path, uid):
        _insert_activity(db_path, 9002020, uid, "2025-01-01")
        _insert_activity(db_path, 9002021, uid, "2025-06-01")
        from database import set_power_best
        set_power_best(db_path, uid, "5min",  280.0, 9002020, "2025-01-01")
        set_power_best(db_path, uid, "20min", 230.0, 9002020, "2025-01-01")
        with patch("tasks.DB_PATH", db_path):
            from tasks import _estimate_derived_params
            r1 = _estimate_derived_params(uid, 9002020, "2025-01-01")
        assert r1["cp_changed"] is True

        # New activity with better power
        set_power_best(db_path, uid, "5min",  310.0, 9002021, "2025-06-01")
        set_power_best(db_path, uid, "20min", 260.0, 9002021, "2025-06-01")
        with patch("tasks.DB_PATH", db_path):
            r2 = _estimate_derived_params(uid, 9002021, "2025-06-01")
        assert r2["cp_changed"] is True
        assert r2["cp"] > r1["cp"]


# ---------------------------------------------------------------------------
# Processing order idempotence
# ---------------------------------------------------------------------------

# Five activities with monotonically increasing power on successive dates.
# Correct final state:
#   5min  best overall = 350W (activity 5 on Jan 25)
#   20min best overall = 280W (activity 5 on Jan 25)
#   max_hr overall     = 183 (activity 4 on Jan 20)
#
# Per-date bests after all activities are processed (regardless of order):
#   Jan  5:  5min=250, 20min=200
#   Jan 10:  5min=270, 20min=215
#   Jan 15:  5min=290, 20min=230
#   Jan 20:  5min=320, 20min=255   max_hr=183
#   Jan 25:  5min=350, 20min=280   max_hr=183 (unchanged, not higher)

_ORDER_ACTIVITIES = [
    # (activity_id_base, date, 5min_W, 20min_W, max_hr)
    (9003001, "2025-01-05",  250, 200, 170),
    (9003002, "2025-01-10",  270, 215, 173),
    (9003003, "2025-01-15",  290, 230, 177),
    (9003004, "2025-01-20",  320, 255, 183),
    (9003005, "2025-01-25",  350, 280, 181),  # HR lower than activity 4
]

# Test a representative subset of all 120 permutations to keep runtime sane
_ORDERINGS = list(itertools.permutations(range(5)))
# Pick 12 evenly-spaced orderings: forward, reverse, and 10 others
_step = max(1, len(_ORDERINGS) // 10)
_SAMPLED_ORDERINGS = [_ORDERINGS[0], _ORDERINGS[-1]] + _ORDERINGS[_step::_step][:10]

_uid_order_counter = 0


def _fresh_uid_for_order(db_path):
    global _uid_order_counter
    _uid_order_counter += 1
    from database import create_user
    return create_user(db_path, f"orderuser{_uid_order_counter}",
                       generate_password_hash("pw"))


def _run_process_activity_order(db_path, uid, ordering, cfg):
    """Insert activities in DB order, then process them in the given ordering."""
    for base_aid, date, w5, w20, hr in _ORDER_ACTIVITIES:
        _insert_activity(db_path, base_aid + uid * 100, uid, date, max_hr=hr)

    with patch("tasks.DB_PATH", db_path):
        import tasks
        from database import clear_athlete_params

        # Reset so each ordering starts clean
        clear_athlete_params(db_path, uid)
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM power_bests WHERE user_id=?", (uid,))
        conn.commit()
        conn.close()

        for idx in ordering:
            base_aid, date, w5, w20, hr = _ORDER_ACTIVITIES[idx]
            aid = base_aid + uid * 100
            row = _insert_activity(db_path, aid, uid, date, max_hr=hr)
            stream = _make_stream(peak_5min_watts=w5, hr=hr)
            with patch("tasks.compute_peak_powers",
                       return_value={"5min": float(w5), "20min": float(w20)}):
                tasks.process_activity(aid, uid, cfg, stream, row)


@pytest.fixture(scope="module")
def _order_cfg(app):
    """Minimal cfg for process_activity in ordering tests."""
    return {
        "charts": {
            "power": {"ftp": None, "zones": []},
            "heart_rate": {"max_hr": None, "zones": []},
        },
        "map": {"output_dir": "/tmp/bikeodon_test_order"},
        "training": {},
    }


class TestProcessingOrderIdempotence:

    @pytest.mark.parametrize("ordering", _SAMPLED_ORDERINGS,
                             ids=[f"ord{i}" for i in range(len(_SAMPLED_ORDERINGS))])
    def test_final_power_bests_identical_for_all_orderings(
            self, app, db_path, _order_cfg, ordering):
        uid = _fresh_uid_for_order(db_path)
        _run_process_activity_order(db_path, uid, ordering, _order_cfg)

        from database import get_power_best
        assert get_power_best(db_path, uid, "5min")["power_watts"]  == 350.0
        assert get_power_best(db_path, uid, "20min")["power_watts"] == 280.0

    @pytest.mark.parametrize("ordering", _SAMPLED_ORDERINGS,
                             ids=[f"ord{i}" for i in range(len(_SAMPLED_ORDERINGS))])
    def test_as_of_date_bests_identical_for_all_orderings(
            self, app, db_path, _order_cfg, ordering):
        uid = _fresh_uid_for_order(db_path)
        _run_process_activity_order(db_path, uid, ordering, _order_cfg)

        from database import get_power_best
        expected = [
            ("2025-01-05",  250, 200),
            ("2025-01-10",  270, 215),
            ("2025-01-15",  290, 230),
            ("2025-01-20",  320, 255),
            ("2025-01-25",  350, 280),
        ]
        for date, exp5, exp20 in expected:
            assert get_power_best(db_path, uid, "5min",  as_of=date)["power_watts"] == float(exp5),  f"5min  on {date}"
            assert get_power_best(db_path, uid, "20min", as_of=date)["power_watts"] == float(exp20), f"20min on {date}"

    @pytest.mark.parametrize("ordering", _SAMPLED_ORDERINGS,
                             ids=[f"ord{i}" for i in range(len(_SAMPLED_ORDERINGS))])
    def test_max_hr_correct_for_all_orderings(
            self, app, db_path, _order_cfg, ordering):
        uid = _fresh_uid_for_order(db_path)
        _run_process_activity_order(db_path, uid, ordering, _order_cfg)

        from database import get_athlete_param
        # Activity 4 (Jan 20) sets max_hr=183; activity 5 (Jan 25) has 181 which is lower
        assert get_athlete_param(db_path, uid, "max_hr") == 183
