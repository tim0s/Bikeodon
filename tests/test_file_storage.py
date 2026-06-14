"""
Tests for original activity file storage.

Covers:
  - save_activity_file() writes bytes to the expected path
  - upsert_activity() stores and preserves source_file via COALESCE
  - /upload route saves raw bytes to disk and records source_file in DB
  - _handle_webhook_event() fetches and saves the original file from Strava
  - /sync route fetches and saves the original file for new activities
"""

import io
import os
import threading
import pytest
import yaml
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal GPX fixture (produces a deterministic activity ID)
# ---------------------------------------------------------------------------

_GPX = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Test Ride</name>
    <type>Ride</type>
    <trkseg>
      <trkpt lat="48.0" lon="11.0">
        <ele>500</ele>
        <time>2026-01-15T08:00:00Z</time>
      </trkpt>
      <trkpt lat="48.01" lon="11.01">
        <ele>510</ele>
        <time>2026-01-15T08:30:00Z</time>
      </trkpt>
    </trkseg>
  </trk>
</gpx>
"""


def _gpx_activity_id() -> int:
    """Return the stable ID that parse_file will assign to _GPX.
    Must match _parse_gpx: _file_id(content + name.encode())."""
    import hashlib
    name = "Test Ride"  # must match <name> in _GPX above
    return int(hashlib.sha256(_GPX + name.encode()).hexdigest()[:13], 16)


# ---------------------------------------------------------------------------
# Shared DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db_env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("file_storage")
    out_dir = str(tmp / "output")
    cfg = {
        "database": {"path": str(tmp / "test.db")},
        "daemon":   {"interval_minutes": 15},
        "map":      {"output_dir": out_dir, "tiles": {}},
    }
    cfg_path = str(tmp / "config.yaml")
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)

    os.environ["BIKEODON_CONFIG"] = cfg_path
    os.environ["FLASK_SECRET_KEY"] = "test-secret"

    import importlib
    import database
    importlib.reload(database)
    database.init_db(cfg["database"]["path"])

    yield {
        "db_path":  cfg["database"]["path"],
        "out_dir":  out_dir,
        "tmp":      tmp,
    }

    os.environ.pop("BIKEODON_CONFIG", None)


@pytest.fixture(scope="module")
def uid(db_env):
    from database import create_user
    from werkzeug.security import generate_password_hash
    return create_user(db_env["db_path"], "storage_tester", generate_password_hash("pw"))


# ---------------------------------------------------------------------------
# Flask test client fixture (reloads app once per module)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def flask_app(db_env):
    import importlib
    # Reload in dependency order so every module picks up the new DB_PATH
    # (each does `from config import DB_PATH` at module level).
    import config as config_module
    importlib.reload(config_module)
    import database as db_module
    importlib.reload(db_module)
    db_module.init_db(config_module.DB_PATH)
    import tasks as tasks_module
    importlib.reload(tasks_module)
    import strava_routes as sr_module
    importlib.reload(sr_module)
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config["TESTING"] = True
    app_module.app.config["SERVER_NAME"] = "bikeodon.org"
    app_module.app.config["PREFERRED_URL_SCHEME"] = "https"
    return app_module.app


@pytest.fixture(scope="module")
def app_cfg(flask_app):
    """Return (DB_PATH, out_dir) as the reloaded app actually sees them.
    Depends on flask_app so the module reload happens before we read config."""
    import config as config_module
    out_dir = config_module._base_cfg["map"].get("output_dir", "output")
    return {"db_path": config_module.DB_PATH, "out_dir": out_dir}


@pytest.fixture()
def client(flask_app):
    return flask_app.test_client()


def _login(client, username="storage_tester", password="pw"):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=True)


# ---------------------------------------------------------------------------
# Unit tests — save_activity_file
# ---------------------------------------------------------------------------

class TestSaveActivityFile:

    def test_creates_file_at_expected_path(self, db_env):
        from database import save_activity_file
        files_dir = os.path.join(db_env["out_dir"], "activity_files")
        path, sha256 = save_activity_file(files_dir, 12345, 1, b"FIT_CONTENT", "12345.fit")
        assert os.path.isfile(path)
        assert open(path, "rb").read() == b"FIT_CONTENT"

    def test_returns_sha256_hex(self, db_env):
        import hashlib
        from database import save_activity_file
        files_dir = os.path.join(db_env["out_dir"], "activity_files")
        content = b"FIT_CONTENT"
        _, sha256 = save_activity_file(files_dir, 12345, 1, content, "12345.fit")
        assert sha256 == hashlib.sha256(content).hexdigest()

    def test_path_includes_user_subdir(self, db_env):
        from database import save_activity_file
        files_dir = os.path.join(db_env["out_dir"], "activity_files")
        path, _ = save_activity_file(files_dir, 99999, 7, b"DATA", "99999.gpx")
        parts = path.replace("\\", "/").split("/")
        assert "7" in parts

    def test_extension_derived_from_filename(self, db_env):
        from database import save_activity_file
        files_dir = os.path.join(db_env["out_dir"], "activity_files")
        path, _ = save_activity_file(files_dir, 11111, 1, b"TCX", "11111.tcx")
        assert path.endswith(".tcx")

    def test_overwrites_existing_file(self, db_env):
        from database import save_activity_file
        files_dir = os.path.join(db_env["out_dir"], "activity_files")
        save_activity_file(files_dir, 22222, 1, b"OLD", "22222.fit")
        save_activity_file(files_dir, 22222, 1, b"NEW", "22222.fit")
        path = os.path.join(files_dir, "1", "22222.fit")
        assert open(path, "rb").read() == b"NEW"


# ---------------------------------------------------------------------------
# Unit tests — upsert_activity source_file handling
# ---------------------------------------------------------------------------

class TestUpsertSourceFile:

    def test_source_file_stored_on_insert(self, db_env, uid):
        from database import upsert_activity, _conn
        upsert_activity(db_env["db_path"], {
            "id": 55001, "name": "File Ride", "sport_type": "Ride",
            "start_date": "2026-01-20T07:00:00Z",
            "source_file": "/data/activity_files/1/55001.fit",
        }, user_id=uid)
        conn = _conn(db_env["db_path"])
        row = conn.execute("SELECT source_file FROM activities WHERE id=55001 AND user_id=?",
                           (uid,)).fetchone()
        conn.close()
        assert row["source_file"] == "/data/activity_files/1/55001.fit"

    def test_source_file_preserved_on_metadata_update(self, db_env, uid):
        """COALESCE: re-upsert without source_file must not wipe the stored path."""
        from database import upsert_activity, _conn
        upsert_activity(db_env["db_path"], {
            "id": 55001, "name": "Renamed Ride", "sport_type": "Ride",
            "start_date": "2026-01-20T07:00:00Z",
        }, user_id=uid)
        conn = _conn(db_env["db_path"])
        row = conn.execute("SELECT source_file FROM activities WHERE id=55001 AND user_id=?",
                           (uid,)).fetchone()
        conn.close()
        assert row["source_file"] == "/data/activity_files/1/55001.fit"

    def test_source_file_updated_when_provided(self, db_env, uid):
        from database import upsert_activity, _conn
        upsert_activity(db_env["db_path"], {
            "id": 55001, "name": "Renamed Ride", "sport_type": "Ride",
            "start_date": "2026-01-20T07:00:00Z",
            "source_file": "/data/activity_files/1/55001.gpx",
        }, user_id=uid)
        conn = _conn(db_env["db_path"])
        row = conn.execute("SELECT source_file FROM activities WHERE id=55001 AND user_id=?",
                           (uid,)).fetchone()
        conn.close()
        assert row["source_file"] == "/data/activity_files/1/55001.gpx"


# ---------------------------------------------------------------------------
# Upload route
# ---------------------------------------------------------------------------

class TestUploadRoute:

    def test_upload_saves_file_to_disk(self, client, app_cfg, uid):
        _login(client)
        activity_id = _gpx_activity_id()
        r = client.post("/upload", data={
            "files": (io.BytesIO(_GPX), "test_ride.gpx"),
        }, content_type="multipart/form-data", follow_redirects=True)
        assert r.status_code == 200
        assert b"Imported" in r.data, f"Expected import flash, got: {r.data[r.data.find(b'<main'):r.data.find(b'<main')+500]}"

        files_root = os.path.join(app_cfg["out_dir"], "activity_files")
        saved = []
        if os.path.isdir(files_root):
            for dirpath, _, filenames in os.walk(files_root):
                for fn in filenames:
                    if str(activity_id) in fn:
                        saved.append(os.path.join(dirpath, fn))
        assert saved, f"No saved file found for activity {activity_id} under {files_root}"
        assert open(saved[0], "rb").read() == _GPX

    def test_upload_sets_source_file_in_db(self, client, app_cfg, uid):
        import hashlib
        from database import _conn
        activity_id = _gpx_activity_id()
        conn = _conn(app_cfg["db_path"])
        row = conn.execute(
            "SELECT source_file, source_file_sha256 FROM activities WHERE id=? AND user_id=?",
            (activity_id, uid),
        ).fetchone()
        conn.close()
        assert row is not None, "Activity not found in DB after upload"
        assert row["source_file"] is not None
        assert os.path.isfile(row["source_file"])
        assert row["source_file_sha256"] == hashlib.sha256(_GPX).hexdigest()

    def test_upload_second_time_skips(self, client, uid):
        """Re-uploading the same file should report 'already in your library'."""
        _login(client)
        r = client.post("/upload", data={
            "files": (io.BytesIO(_GPX), "test_ride.gpx"),
        }, content_type="multipart/form-data", follow_redirects=True)
        assert b"already in your library" in r.data


# ---------------------------------------------------------------------------
# Strava webhook — _handle_webhook_event
# ---------------------------------------------------------------------------

class TestWebhookFileStorage:

    def _make_mock_client(self, activity_id=77001):
        client = MagicMock()
        client.get_activity.return_value = {
            "id": activity_id,
            "name": "Webhook Ride",
            "sport_type": "Ride",
            "start_date": "2026-02-01T08:00:00Z",
            "distance": 25000.0,
            "moving_time": 3600,
            "elapsed_time": 3700,
        }
        client.get_original_file.return_value = (b"FIT_BYTES", f"{activity_id}.fit")
        return client

    def test_webhook_fetches_original_file(self, db_env, uid, flask_app):
        from strava_routes import _handle_webhook_event
        from database import get_user_by_id

        mock_client = self._make_mock_client(77001)

        with flask_app.app_context():
            with patch("strava_routes._make_strava_client", return_value=mock_client), \
                 patch("strava_routes.get_user_by_athlete_id") as mock_get_user, \
                 patch("strava_routes._render_and_track"), \
                 patch("strava_routes.request_backfill"):
                # Simulate the user lookup returning our test user
                mock_get_user.return_value = {"id": uid}

                _handle_webhook_event({
                    "object_type": "activity",
                    "aspect_type": "create",
                    "object_id": 77001,
                    "owner_id": 99,
                })

        mock_client.get_original_file.assert_called_once_with(77001)

    def test_webhook_saves_file_to_disk(self, db_env, uid, flask_app):
        from strava_routes import _handle_webhook_event

        mock_client = self._make_mock_client(77002)

        with flask_app.app_context():
            with patch("strava_routes._make_strava_client", return_value=mock_client), \
                 patch("strava_routes.get_user_by_athlete_id", return_value={"id": uid}), \
                 patch("strava_routes._render_and_track"), \
                 patch("strava_routes.request_backfill"):
                _handle_webhook_event({
                    "object_type": "activity",
                    "aspect_type": "create",
                    "object_id": 77002,
                    "owner_id": 99,
                })

        files_root = os.path.join(db_env["out_dir"], "activity_files")
        saved = []
        if os.path.isdir(files_root):
            for dirpath, _, filenames in os.walk(files_root):
                for fn in filenames:
                    if "77002" in fn:
                        saved.append(os.path.join(dirpath, fn))
        assert saved, "Original file not saved after webhook event"
        assert open(saved[0], "rb").read() == b"FIT_BYTES"

    def test_webhook_tolerates_missing_original_file(self, db_env, uid, flask_app):
        """get_original_file returning None must not crash the webhook handler."""
        from strava_routes import _handle_webhook_event

        mock_client = self._make_mock_client(77003)
        mock_client.get_original_file.return_value = None

        with flask_app.app_context():
            with patch("strava_routes._make_strava_client", return_value=mock_client), \
                 patch("strava_routes.get_user_by_athlete_id", return_value={"id": uid}), \
                 patch("strava_routes._render_and_track"), \
                 patch("strava_routes.request_backfill"):
                # Should not raise
                _handle_webhook_event({
                    "object_type": "activity",
                    "aspect_type": "create",
                    "object_id": 77003,
                    "owner_id": 99,
                })

    def test_webhook_tolerates_strava_error(self, db_env, uid, flask_app):
        """get_original_file raising must not crash the webhook handler."""
        from strava_routes import _handle_webhook_event

        mock_client = self._make_mock_client(77004)
        mock_client.get_original_file.side_effect = Exception("network error")

        with flask_app.app_context():
            with patch("strava_routes._make_strava_client", return_value=mock_client), \
                 patch("strava_routes.get_user_by_athlete_id", return_value={"id": uid}), \
                 patch("strava_routes._render_and_track"), \
                 patch("strava_routes.request_backfill"):
                _handle_webhook_event({
                    "object_type": "activity",
                    "aspect_type": "create",
                    "object_id": 77004,
                    "owner_id": 99,
                })


# ---------------------------------------------------------------------------
# Manual sync route
# ---------------------------------------------------------------------------

class _SyncThread:
    """Runs thread target synchronously so patches stay active during execution."""
    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class TestManualSyncFileStorage:

    def _make_mock_client(self, activity_id=88001):
        c = MagicMock()
        c.get_activity_ids.return_value = [activity_id]
        c.get_activity.return_value = {
            "id": activity_id,
            "name": "Sync Ride",
            "sport_type": "Ride",
            "start_date": "2026-03-01T07:00:00Z",
            "distance": 40000.0,
            "moving_time": 4500,
            "elapsed_time": 4600,
        }
        c.get_original_file.return_value = (b"SYNC_FIT", f"{activity_id}.fit")
        return c

    def test_manual_sync_saves_original_file(self, client, app_cfg, uid):
        _login(client)
        mock_strava = self._make_mock_client(88001)

        with patch("strava_routes._make_strava_client", return_value=mock_strava), \
             patch("strava_routes._sync_cooldown_remaining", return_value=0), \
             patch("strava_routes._render_and_track"), \
             patch("strava_routes.request_backfill"), \
             patch("strava_routes.threading.Thread", _SyncThread):
            r = client.post("/sync", follow_redirects=True)

        assert r.status_code == 200
        mock_strava.get_original_file.assert_called_once_with(88001)

    def test_manual_sync_file_on_disk(self, client, app_cfg, uid):
        _login(client)
        mock_strava = self._make_mock_client(88002)

        with patch("strava_routes._make_strava_client", return_value=mock_strava), \
             patch("strava_routes._sync_cooldown_remaining", return_value=0), \
             patch("strava_routes._render_and_track"), \
             patch("strava_routes.request_backfill"), \
             patch("strava_routes.threading.Thread", _SyncThread):
            client.post("/sync", follow_redirects=True)

        files_root = os.path.join(app_cfg["out_dir"], "activity_files")
        saved = []
        if os.path.isdir(files_root):
            for dirpath, _, filenames in os.walk(files_root):
                for fn in filenames:
                    if "88002" in fn:
                        saved.append(os.path.join(dirpath, fn))
        assert saved, "Original file not saved after manual sync"
        assert open(saved[0], "rb").read() == b"SYNC_FIT"
