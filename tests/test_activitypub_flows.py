"""
Tests for new ActivityPub helper flows:
  - _is_local_actor: local vs remote actor detection
  - send_like / send_unlike / send_boost / send_unboost: skip delivery for local actors
  - send_reply: adds to local feed; skips delivery for local posts; federates for remote
"""

import json
import os
import pytest
import yaml
from unittest.mock import patch, MagicMock
from werkzeug.security import generate_password_hash


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ap_flows")
    cfg = {
        "database": {"path": str(tmp / "test.db")},
        "map":      {"output_dir": str(tmp / "output"), "tiles": {}},
    }
    cfg_path = str(tmp / "config.yaml")
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)

    os.environ["BIKEODON_CONFIG"] = cfg_path
    os.environ["FLASK_SECRET_KEY"] = "test-secret"

    import importlib
    import app as app_module
    importlib.reload(app_module)

    app_module.app.config["TESTING"] = True
    app_module.app.config["SERVER_NAME"] = "bikeodon.org"
    app_module.app.config["PREFERRED_URL_SCHEME"] = "https"

    yield app_module.app

    os.environ.pop("BIKEODON_CONFIG", None)


@pytest.fixture(scope="module")
def db_path(app):
    import app as app_module
    return app_module.DB_PATH


_counter = 0

@pytest.fixture()
def user(app, db_path):
    global _counter
    _counter += 1
    username = f"flowuser{_counter}"
    from database import create_user
    uid = create_user(db_path, username, generate_password_hash("pw"))
    return username, uid


def _delivery_count(db_path):
    from database import _conn
    c = _conn(db_path)
    n = c.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0]
    c.close()
    return n


def _deliveries_since(db_path, before_count):
    from database import _conn
    c = _conn(db_path)
    rows = c.execute(
        "SELECT activity_json FROM delivery_queue ORDER BY id DESC LIMIT ?",
        (max(_delivery_count(db_path) - before_count, 0),),
    ).fetchall()
    c.close()
    return [json.loads(r[0]) for r in rows]


# ---------------------------------------------------------------------------
# TestIsLocalActor
# ---------------------------------------------------------------------------

class TestIsLocalActor:

    def test_local_actor_is_detected(self, app):
        with app.app_context():
            from activitypub import _is_local_actor
            assert _is_local_actor("https://bikeodon.org/users/someone") is True

    def test_local_actor_with_subpath(self, app):
        with app.app_context():
            from activitypub import _is_local_actor
            assert _is_local_actor("https://bikeodon.org/users/x") is True

    def test_remote_actor_is_not_local(self, app):
        with app.app_context():
            from activitypub import _is_local_actor
            assert _is_local_actor("https://mastodon.social/users/alice") is False

    def test_similar_domain_is_not_local(self, app):
        with app.app_context():
            from activitypub import _is_local_actor
            assert _is_local_actor("https://notbikeodon.org/users/x") is False


# ---------------------------------------------------------------------------
# TestSendLikeSkipsLocalActors
# ---------------------------------------------------------------------------

class TestSendLikeSkipsLocalActors:

    def test_local_like_does_not_enqueue_delivery(self, app, db_path, user):
        username, uid = user
        before = _delivery_count(db_path)
        with app.app_context():
            from database import get_user_by_id
            from activitypub import send_like
            u = get_user_by_id(db_path, uid)
            local_actor = f"https://bikeodon.org/users/{username}"
            local_note  = f"https://bikeodon.org/users/{username}/activities/12345"
            send_like(username, u, local_note, local_actor, db_path)
        assert _delivery_count(db_path) == before

    def test_local_unlike_does_not_enqueue_delivery(self, app, db_path, user):
        username, uid = user
        before = _delivery_count(db_path)
        with app.app_context():
            from database import get_user_by_id
            from activitypub import send_unlike
            u = get_user_by_id(db_path, uid)
            local_actor = f"https://bikeodon.org/users/{username}"
            local_note  = f"https://bikeodon.org/users/{username}/activities/12345"
            send_unlike(username, u, local_note, local_actor, db_path)
        assert _delivery_count(db_path) == before

    def test_local_boost_does_not_enqueue_delivery(self, app, db_path, user):
        username, uid = user
        before = _delivery_count(db_path)
        with app.app_context():
            from database import get_user_by_id
            from activitypub import send_boost
            u = get_user_by_id(db_path, uid)
            local_actor = f"https://bikeodon.org/users/{username}"
            local_note  = f"https://bikeodon.org/users/{username}/activities/12345"
            send_boost(username, u, local_note, local_actor, db_path)
        assert _delivery_count(db_path) == before

    def test_local_unboost_does_not_enqueue_delivery(self, app, db_path, user):
        username, uid = user
        before = _delivery_count(db_path)
        with app.app_context():
            from database import get_user_by_id
            from activitypub import send_unboost
            u = get_user_by_id(db_path, uid)
            local_actor = f"https://bikeodon.org/users/{username}"
            local_note  = f"https://bikeodon.org/users/{username}/activities/12345"
            send_unboost(username, u, local_note, local_actor, db_path)
        assert _delivery_count(db_path) == before

    def test_remote_like_enqueues_delivery(self, app, db_path, user):
        username, uid = user
        before = _delivery_count(db_path)
        remote_actor = "https://mastodon.social/users/alice"
        remote_note  = "https://mastodon.social/users/alice/statuses/99"
        remote_inbox = "https://mastodon.social/users/alice/inbox"

        with app.app_context():
            from database import get_user_by_id
            from activitypub import send_like
            u = get_user_by_id(db_path, uid)
            with patch("activitypub.requests.get") as mock_get:
                mock_get.return_value = MagicMock(
                    ok=True,
                    json=lambda: {"inbox": remote_inbox},
                )
                send_like(username, u, remote_note, remote_actor, db_path)

        assert _delivery_count(db_path) > before
        new_deliveries = _deliveries_since(db_path, before)
        types = [d.get("type") for d in new_deliveries]
        assert "Like" in types

    def test_remote_like_targets_correct_inbox(self, app, db_path, user):
        username, uid = user
        before = _delivery_count(db_path)
        remote_actor = "https://mastodon.social/users/bob"
        remote_inbox = "https://mastodon.social/users/bob/inbox"
        remote_note  = "https://mastodon.social/users/bob/statuses/88"

        with app.app_context():
            from database import get_user_by_id, _conn
            from activitypub import send_like
            u = get_user_by_id(db_path, uid)
            with patch("activitypub.requests.get") as mock_get:
                mock_get.return_value = MagicMock(ok=True, json=lambda: {"inbox": remote_inbox})
                send_like(username, u, remote_note, remote_actor, db_path)

        c = _conn(db_path)
        row = c.execute(
            "SELECT inbox_url FROM delivery_queue ORDER BY id DESC LIMIT 1"
        ).fetchone()
        c.close()
        assert row["inbox_url"] == remote_inbox


# ---------------------------------------------------------------------------
# TestSendReply
# ---------------------------------------------------------------------------

class TestSendReply:

    def test_local_reply_adds_to_feed(self, app, db_path, user):
        username, uid = user
        local_actor = f"https://bikeodon.org/users/{username}"
        local_note  = f"https://bikeodon.org/users/{username}/activities/55555"
        with app.app_context():
            from database import get_user_by_id, get_feed_items
            from activitypub import send_reply
            u = get_user_by_id(db_path, uid)
            send_reply(username, u, local_note, local_actor, "great ride!", db_path)
            # The reply should appear as a feed item linked to the parent
            from database import _conn
            c = _conn(db_path)
            row = c.execute(
                "SELECT * FROM feed_items WHERE local_username=? AND in_reply_to=?",
                (username, local_note),
            ).fetchone()
            c.close()
        assert row is not None
        assert "great ride!" in (row["content"] or "")

    def test_local_reply_does_not_enqueue_delivery(self, app, db_path, user):
        username, uid = user
        before = _delivery_count(db_path)
        local_actor = f"https://bikeodon.org/users/{username}"
        local_note  = f"https://bikeodon.org/users/{username}/activities/55556"
        with app.app_context():
            from database import get_user_by_id
            from activitypub import send_reply
            u = get_user_by_id(db_path, uid)
            send_reply(username, u, local_note, local_actor, "nice!", db_path)
        assert _delivery_count(db_path) == before

    def test_remote_reply_enqueues_delivery(self, app, db_path, user):
        username, uid = user
        before = _delivery_count(db_path)
        remote_actor = "https://mastodon.social/users/carol"
        remote_note  = "https://mastodon.social/users/carol/statuses/77"
        remote_inbox = "https://mastodon.social/users/carol/inbox"
        with app.app_context():
            from database import get_user_by_id
            from activitypub import send_reply
            u = get_user_by_id(db_path, uid)
            with patch("activitypub.requests.get") as mock_get:
                mock_get.return_value = MagicMock(ok=True, json=lambda: {"inbox": remote_inbox})
                send_reply(username, u, remote_note, remote_actor, "cool post", db_path)
        assert _delivery_count(db_path) > before
        new = _deliveries_since(db_path, before)
        types = [d.get("type") for d in new]
        assert "Create" in types

    def test_remote_reply_also_adds_to_feed(self, app, db_path, user):
        username, uid = user
        remote_actor = "https://mastodon.social/users/dave"
        remote_note  = "https://mastodon.social/users/dave/statuses/66"
        remote_inbox = "https://mastodon.social/users/dave/inbox"
        with app.app_context():
            from database import get_user_by_id, _conn
            from activitypub import send_reply
            u = get_user_by_id(db_path, uid)
            with patch("activitypub.requests.get") as mock_get:
                mock_get.return_value = MagicMock(ok=True, json=lambda: {"inbox": remote_inbox})
                send_reply(username, u, remote_note, remote_actor, "nice one", db_path)
            c = _conn(db_path)
            row = c.execute(
                "SELECT * FROM feed_items WHERE local_username=? AND in_reply_to=?",
                (username, remote_note),
            ).fetchone()
            c.close()
        assert row is not None

    def test_reply_note_has_in_reply_to_field(self, app, db_path, user):
        username, uid = user
        remote_actor = "https://mastodon.social/users/eve"
        remote_note  = "https://mastodon.social/users/eve/statuses/55"
        remote_inbox = "https://mastodon.social/users/eve/inbox"
        with app.app_context():
            from database import get_user_by_id, _conn
            from activitypub import send_reply
            u = get_user_by_id(db_path, uid)
            with patch("activitypub.requests.get") as mock_get:
                mock_get.return_value = MagicMock(ok=True, json=lambda: {"inbox": remote_inbox})
                send_reply(username, u, remote_note, remote_actor, "thread check", db_path)
            # Check the queued Create activity's object has inReplyTo
            c = _conn(db_path)
            row = c.execute(
                "SELECT activity_json FROM delivery_queue ORDER BY id DESC LIMIT 1"
            ).fetchone()
            c.close()
        act = json.loads(row["activity_json"])
        assert act["object"]["inReplyTo"] == remote_note

    def test_reply_content_is_sanitised(self, app, db_path, user):
        username, uid = user
        local_actor = f"https://bikeodon.org/users/{username}"
        local_note  = f"https://bikeodon.org/users/{username}/activities/55560"
        with app.app_context():
            from database import get_user_by_id, _conn
            from activitypub import send_reply
            u = get_user_by_id(db_path, uid)
            send_reply(username, u, local_note, local_actor,
                       '<script>alert(1)</script>clean text', db_path)
            c = _conn(db_path)
            row = c.execute(
                "SELECT content FROM feed_items WHERE local_username=? AND in_reply_to=?",
                (username, local_note),
            ).fetchone()
            c.close()
        assert "<script>" not in (row["content"] or "")
        assert "clean text" in (row["content"] or "")
