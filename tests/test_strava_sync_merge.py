"""
Regression test: a Strava-synced activity whose time window overlaps an
existing activity (e.g. a Bikeodon-recorded training session, later also
uploaded to Strava from a bike computer/watch) used to be inserted as a
second, separate row. /upload already de-dupes this way via
find_overlapping_activity() + attach_source_file() — tasks._strava_sync_user()
now does the same instead of creating a duplicate.
"""
import os
import pytest
import yaml
from werkzeug.security import generate_password_hash


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("strava_merge")
    path = str(tmp / "test.db")
    cfg = {
        "database": {"path": path},
        "map":      {"output_dir": str(tmp / "output"), "tiles": {}},
    }
    with open(str(tmp / "config.yaml"), "w") as f:
        yaml.dump(cfg, f)
    os.environ["BIKEODON_CONFIG"] = str(tmp / "config.yaml")

    import database
    database.init_db(path)
    return path


@pytest.fixture(scope="module")
def uid(db_path):
    from database import create_user
    return create_user(db_path, "sync_merge_user", generate_password_hash("pw"))


class _FakeStravaClient:
    def __init__(self, ids, activity_by_id):
        self._ids = ids
        self._activity_by_id = activity_by_id

    def get_activity_ids(self, n=20, page=1):
        return self._ids if page == 1 else []

    def get_activity(self, activity_id):
        return self._activity_by_id[activity_id]


def _patch_common(monkeypatch, db_path, fake_client):
    import tasks
    monkeypatch.setattr(tasks, "DB_PATH", db_path)
    monkeypatch.setattr(tasks, "_make_strava_client", lambda u: fake_client)
    monkeypatch.setattr(tasks, "generate_fit", lambda act, streams: b"FAKEFIT")
    monkeypatch.setattr(tasks, "save_activity_file", lambda *a, **k: ("/dev/null", "sha"))
    monkeypatch.setattr(tasks, "request_render", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "request_backfill", lambda *a, **k: None)
    return tasks


def test_overlapping_strava_activity_merges_into_existing_row(db_path, uid, monkeypatch):
    from database import get_activity, upsert_activity

    local_id = 91000001
    strava_id = 91000002

    # A Bikeodon-recorded training session already exists...
    upsert_activity(db_path, {
        "id": local_id, "name": "Sweet Spot", "sport_type": "Ride",
        "start_date": "2026-07-01T16:04:21Z",
        "distance": 0, "moving_time": 2527, "elapsed_time": 2527,
    }, user_id=uid, source="training")

    # ...and the same ride is then synced in from Strava (e.g. uploaded from
    # a bike computer afterward), starting 12s later with a similar duration.
    strava_activity = {
        "id": strava_id, "name": "Evening Ride", "sport_type": "Ride",
        "start_date": "2026-07-01T16:04:09Z",
        "distance": 15000.0, "moving_time": 2745, "elapsed_time": 2745,
    }
    fake_client = _FakeStravaClient([strava_id], {strava_id: (strava_activity, {"time": {"data": [0, 1]}})})
    tasks = _patch_common(monkeypatch, db_path, fake_client)

    tasks._strava_sync_user(uid)

    # No second row was created for the Strava id.
    assert get_activity(db_path, strava_id, user_id=uid) is None
    # The original Bikeodon-recorded row is untouched in identity/name...
    merged = get_activity(db_path, local_id, user_id=uid)
    assert merged is not None
    assert merged["name"] == "Sweet Spot"
    assert merged["source"] == "training"
    # ...but now has the richer Strava-derived file attached.
    assert merged["source_file"] == "/dev/null"
    assert merged["source_file_sha256"] == "sha"


def test_non_overlapping_strava_activity_is_still_imported_normally(db_path, uid, monkeypatch):
    from database import get_activity

    strava_id = 91000003
    strava_activity = {
        "id": strava_id, "name": "Unrelated Ride", "sport_type": "Ride",
        "start_date": "2026-06-01T09:00:00Z",  # far from any existing activity
        "distance": 20000.0, "moving_time": 3600, "elapsed_time": 3600,
    }
    fake_client = _FakeStravaClient([strava_id], {strava_id: (strava_activity, {"time": {"data": [0, 1]}})})
    tasks = _patch_common(monkeypatch, db_path, fake_client)

    tasks._strava_sync_user(uid)

    imported = get_activity(db_path, strava_id, user_id=uid)
    assert imported is not None
    assert imported["name"] == "Unrelated Ride"
