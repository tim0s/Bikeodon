"""
Regression test: deleting an activity used to leave no record of the
deletion, so the next Strava sync run treated it as never-seen and
silently re-imported it. tasks._strava_sync_user() now also checks
database.was_deleted() before importing.

Uses a fake StravaClient (no real network) and monkeypatches tasks.DB_PATH
directly rather than the config/env-reload dance other test files use, since
tasks.py reads DB_PATH from its own module globals at call time.
"""
import os
import pytest
import yaml
from werkzeug.security import generate_password_hash


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("strava_tombstone")
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
    return create_user(db_path, "sync_tombstone_user", generate_password_hash("pw"))


class _FakeStravaClient:
    """Returns a fixed page of activity ids, then a fake detail/streams pair
    for whichever id is actually fetched — no real network call."""

    def __init__(self, ids):
        self._ids = ids

    def get_activity_ids(self, n=20, page=1):
        return self._ids if page == 1 else []

    def get_activity(self, activity_id):
        activity = {
            "id": activity_id,
            "name": f"Synced Ride {activity_id}",
            "sport_type": "Ride",
            "start_date": "2026-02-01T08:00:00Z",
            "distance": 10000.0,
            "moving_time": 1800,
            "elapsed_time": 1800,
        }
        streams = {"time": {"data": [0, 1, 2]}, "watts": {"data": [100, 110, 120]}}
        return activity, streams


def test_sync_skips_tombstoned_activity_but_imports_new_one(db_path, uid, monkeypatch):
    import tasks
    from database import delete_activity, get_activity, was_deleted, upsert_activity

    monkeypatch.setattr(tasks, "DB_PATH", db_path)

    deleted_id = 88880001
    new_id = 88880002

    # Seed and delete one activity — this is what creates the tombstone.
    upsert_activity(db_path, {
        "id": deleted_id, "name": "To Be Deleted", "sport_type": "Ride",
        "start_date": "2026-01-20T08:00:00Z", "distance": 5000.0,
        "moving_time": 900, "elapsed_time": 900,
    }, user_id=uid)
    delete_activity(db_path, deleted_id, uid)
    assert was_deleted(db_path, uid, deleted_id) is True
    assert get_activity(db_path, deleted_id, user_id=uid) is None

    fake_client = _FakeStravaClient([deleted_id, new_id])
    monkeypatch.setattr(tasks, "_make_strava_client", lambda u: fake_client)
    # generate_fit isn't the point of this test — keep it from touching real FIT encoding.
    monkeypatch.setattr(tasks, "generate_fit", lambda act, streams: b"")
    monkeypatch.setattr(tasks, "save_activity_file", lambda *a, **k: ("/dev/null", "sha"))
    monkeypatch.setattr(tasks, "request_render", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "request_backfill", lambda *a, **k: None)

    imported_count = tasks._strava_sync_user(uid)

    assert imported_count == 1
    assert get_activity(db_path, deleted_id, user_id=uid) is None, \
        "tombstoned activity must not be resurrected by sync"
    assert get_activity(db_path, new_id, user_id=uid) is not None, \
        "a genuinely new activity must still be imported"
