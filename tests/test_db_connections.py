"""
Tests that DB connections are always closed after use.

Uses the conn_tracker fixture (conftest.py) which wraps _conn/_db_conn
and asserts every opened connection was closed at the end of the test.
"""
import os
import pytest
import yaml
from werkzeug.security import generate_password_hash


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    """Initialised DB for connection-leak tests — no Flask app needed."""
    tmp = tmp_path_factory.mktemp("connleak")
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


@pytest.fixture()
def uid(db_path):
    """Create a fresh user for each test."""
    from database import create_user
    import time
    username = f"leaktest_{int(time.time() * 1000)}"
    return create_user(db_path, username, generate_password_hash("pw"))


# ---------------------------------------------------------------------------
# activitypub.get_or_create_keypair
# ---------------------------------------------------------------------------

class TestGetOrCreateKeypairLeaks:

    def test_no_leak_when_keypair_already_exists(self, db_path, uid, conn_tracker):
        from activitypub import get_or_create_keypair
        # First call generates the keypair
        get_or_create_keypair(db_path, uid)
        conn_tracker.clear()
        # Second call takes the early-return path — this was the original leak
        get_or_create_keypair(db_path, uid)
        # conn_tracker fixture asserts all connections closed on teardown

    def test_no_leak_when_generating_new_keypair(self, db_path, uid, conn_tracker):
        from activitypub import get_or_create_keypair
        get_or_create_keypair(db_path, uid)


# ---------------------------------------------------------------------------
# database functions with early-return paths
# ---------------------------------------------------------------------------

class TestDatabaseFunctionLeaks:

    def test_get_user_by_username_missing(self, db_path, conn_tracker):
        from database import get_user_by_username
        result = get_user_by_username(db_path, "nobody")
        assert result is None

    def test_get_user_by_username_found(self, db_path, uid, conn_tracker):
        from database import get_user_by_username, get_user_by_id
        user = get_user_by_id(db_path, uid)
        result = get_user_by_username(db_path, user["username"])
        assert result is not None

    def test_get_activity_missing(self, db_path, uid, conn_tracker):
        from database import get_activity
        result = get_activity(db_path, 99999, user_id=uid)
        assert result is None

    def test_get_setting_missing(self, db_path, uid, conn_tracker):
        from database import get_setting
        result = get_setting(db_path, uid, "strava", "nonexistent_key")
        assert result is None

    def test_set_and_get_setting(self, db_path, uid, conn_tracker):
        from database import set_setting, get_setting
        set_setting(db_path, uid, "strava", "test_key", "hello")
        result = get_setting(db_path, uid, "strava", "test_key")
        assert result == "hello"

    def test_upsert_activity(self, db_path, uid, conn_tracker):
        from database import upsert_activity
        upsert_activity(db_path, {
            "id": 42001, "name": "Leak Test Ride", "sport_type": "Ride",
            "start_date": "2026-01-01T10:00:00Z",
        }, user_id=uid)

    def test_get_followers_empty(self, db_path, uid, conn_tracker):
        from database import get_followers, get_user_by_id
        user = get_user_by_id(db_path, uid)
        result = get_followers(db_path, user["username"])
        assert result == []

    def test_get_following_empty(self, db_path, uid, conn_tracker):
        from database import get_following, get_user_by_id
        user = get_user_by_id(db_path, uid)
        result = get_following(db_path, user["username"])
        assert result == []
