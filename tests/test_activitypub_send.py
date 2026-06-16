"""
Tests for the ActivityPub send-side and delivery machinery.

No real HTTP calls are made:
  - send_* functions: requests.get is mocked to return a fake actor document
  - _process_due_deliveries: _do_http_post is mocked
  - _verify_http_signature: _fetch_public_key_pem is mocked to return the real pub key

Layers covered:
  1. Pure functions (no mocking)
  2. Crypto keypair + sign/verify round-trip
  3. send_follow / send_unfollow / send_profile_update
  4. send_like / send_unlike / send_boost / send_unboost / send_reply
  5. Delivery worker: success, retry backoff, aged-out failure
"""

import base64
import hashlib
import json
import sqlite3
import os
import pytest
import yaml
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from werkzeug.security import generate_password_hash


# ---------------------------------------------------------------------------
# App + DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ap_send")
    cfg = {
        "database": {"path": str(tmp / "test.db")},
        "daemon":   {"interval_minutes": 15},
        "map":      {"output_dir": str(tmp / "output"), "tiles": {}},
    }
    cfg_path = str(tmp / "config.yaml")
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)

    os.environ["BIKEODON_CONFIG"] = cfg_path
    os.environ["FLASK_SECRET_KEY"] = "ap-send-test-secret"

    import importlib
    import config as config_mod
    importlib.reload(config_mod)
    import app as app_mod
    importlib.reload(app_mod)

    app_mod.app.config["TESTING"] = True
    app_mod.app.config["SERVER_NAME"] = "bikeodon.org"
    app_mod.app.config["PREFERRED_URL_SCHEME"] = "https"
    app_mod.app.config["_TEST_DB_PATH"] = str(tmp / "test.db")

    yield app_mod.app
    os.environ.pop("BIKEODON_CONFIG", None)


@pytest.fixture(scope="module")
def db_path(app):
    return app.config["_TEST_DB_PATH"]


_uid_counter = 0

@pytest.fixture()
def user(app, db_path):
    """Create a unique test user; return the DB row as a dict."""
    global _uid_counter
    _uid_counter += 1
    username = f"aptest{_uid_counter}"
    from database import create_user, get_user_by_username
    with app.app_context():
        create_user(db_path, username, generate_password_hash("pw"))
        return dict(get_user_by_username(db_path, username))


@pytest.fixture()
def keypair(app, db_path, user):
    """Generate (or fetch) the RSA keypair for the test user."""
    from activitypub import get_or_create_keypair
    with app.app_context():
        return get_or_create_keypair(db_path, user["id"])


def _queue_rows(db_path):
    """Return all delivery_queue rows as dicts."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM delivery_queue ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _latest_queue(db_path):
    rows = _queue_rows(db_path)
    return rows[-1] if rows else None


def _fake_actor(inbox="https://remote.example/inbox", name="Remote Bob"):
    """Build a minimal actor document dict for mocking requests.get."""
    m = MagicMock()
    m.ok = True
    m.json.return_value = {
        "id":                "https://remote.example/users/bob",
        "type":              "Person",
        "inbox":             inbox,
        "name":              name,
        "preferredUsername": "bob",
        "icon":              {"type": "Image", "url": "https://remote.example/bob.png"},
    }
    return m


# ---------------------------------------------------------------------------
# 1. Pure functions
# ---------------------------------------------------------------------------

class TestParseSigHeader:

    def test_basic(self):
        from activitypub import _parse_signature_header
        h = 'keyId="https://ex.com/user#key",algorithm="rsa-sha256",signature="abc123"'
        d = _parse_signature_header(h)
        assert d["keyId"] == "https://ex.com/user#key"
        assert d["algorithm"] == "rsa-sha256"
        assert d["signature"] == "abc123"

    def test_headers_field(self):
        from activitypub import _parse_signature_header
        h = 'keyId="k",headers="(request-target) host date digest",signature="s"'
        d = _parse_signature_header(h)
        assert d["headers"] == "(request-target) host date digest"

    def test_empty_string(self):
        from activitypub import _parse_signature_header
        assert _parse_signature_header("") == {}


class TestAvatarMediaType:

    def test_png(self):
        from activitypub import _avatar_media_type
        assert _avatar_media_type("avatar.png") == "image/png"

    def test_jpg(self):
        from activitypub import _avatar_media_type
        assert _avatar_media_type("photo.jpg") == "image/jpeg"

    def test_jpeg(self):
        from activitypub import _avatar_media_type
        assert _avatar_media_type("photo.jpeg") == "image/jpeg"

    def test_gif(self):
        from activitypub import _avatar_media_type
        assert _avatar_media_type("anim.gif") == "image/gif"

    def test_webp(self):
        from activitypub import _avatar_media_type
        assert _avatar_media_type("img.webp") == "image/webp"

    def test_none_defaults_to_png(self):
        from activitypub import _avatar_media_type
        assert _avatar_media_type(None) == "image/png"

    def test_unknown_extension_defaults_to_png(self):
        from activitypub import _avatar_media_type
        assert _avatar_media_type("file.bmp") == "image/png"


class TestHashtagsForActivity:

    def test_ride(self):
        from activitypub import _hashtags_for_activity
        tags = _hashtags_for_activity("Ride")
        assert "cycling" in tags
        assert "strava" in tags
        assert "bikeodon" in tags

    def test_virtual_ride_includes_zwift(self):
        from activitypub import _hashtags_for_activity
        tags = _hashtags_for_activity("VirtualRide")
        assert "cycling" in tags
        assert "zwift" in tags

    def test_run(self):
        from activitypub import _hashtags_for_activity
        tags = _hashtags_for_activity("Run")
        assert "running" in tags

    def test_unknown_sport_still_has_base_tags(self):
        from activitypub import _hashtags_for_activity
        tags = _hashtags_for_activity("Unicycling")
        assert "strava" in tags
        assert "bikeodon" in tags

    def test_none_sport_has_base_tags(self):
        from activitypub import _hashtags_for_activity
        tags = _hashtags_for_activity(None)
        assert "strava" in tags


class TestActivityIdFromNoteUrl:

    def test_valid_url(self):
        from activitypub import _activity_id_from_note_url
        base = "https://bikeodon.org/users/alice"
        url  = f"{base}/activities/12345"
        assert _activity_id_from_note_url(url, base) == 12345

    def test_url_with_suffix(self):
        from activitypub import _activity_id_from_note_url
        base = "https://bikeodon.org/users/alice"
        url  = f"{base}/activities/99/create"
        assert _activity_id_from_note_url(url, base) == 99

    def test_wrong_base(self):
        from activitypub import _activity_id_from_note_url
        assert _activity_id_from_note_url(
            "https://other.example/users/bob/activities/1",
            "https://bikeodon.org/users/alice"
        ) is None

    def test_non_numeric(self):
        from activitypub import _activity_id_from_note_url
        base = "https://bikeodon.org/users/alice"
        assert _activity_id_from_note_url(f"{base}/activities/abc", base) is None


class TestActivityRowToAp:

    def test_structure(self, app):
        from activitypub import _activity_row_to_ap
        row = {
            "id": 42, "name": "Morning Ride", "sport_type": "Ride",
            "distance": 30000, "total_elevation_gain": 300,
            "start_date": "2024-06-01T08:00:00Z",
        }
        actor_url  = "https://bikeodon.org/users/alice"
        outbox_url = "https://bikeodon.org/users/alice/outbox"
        with app.app_context():
            doc = _activity_row_to_ap(row, actor_url, outbox_url)

        assert doc["type"] == "Create"
        assert doc["actor"] == actor_url
        note = doc["object"]
        assert note["type"] == "Note"
        assert "Morning Ride" in note["content"]
        assert "30.0 km" in note["content"]
        assert note["attributedTo"] == actor_url

    def test_hashtags_in_tags(self, app):
        from activitypub import _activity_row_to_ap
        row = {"id": 1, "name": "Ride", "sport_type": "Ride",
               "distance": 0, "total_elevation_gain": 0, "start_date": "2024-01-01T00:00:00Z"}
        with app.app_context():
            doc = _activity_row_to_ap(row, "https://bikeodon.org/users/u",
                                      "https://bikeodon.org/users/u/outbox")
        tag_names = {t["name"] for t in doc["object"]["tag"]}
        assert "#cycling" in tag_names
        assert "#strava" in tag_names

    def test_image_attachments(self, app):
        from activitypub import _activity_row_to_ap
        row = {"id": 2, "name": "X", "sport_type": "Ride",
               "distance": 0, "total_elevation_gain": 0, "start_date": "2024-01-01T00:00:00Z"}
        images = ["https://bikeodon.org/output/2.png"]
        with app.app_context():
            doc = _activity_row_to_ap(row, "https://bikeodon.org/users/u",
                                      "https://bikeodon.org/users/u/outbox", images)
        assert len(doc["object"]["attachment"]) == 1
        assert doc["object"]["attachment"][0]["url"] == images[0]

    def test_no_distance_omits_stats_line(self, app):
        from activitypub import _activity_row_to_ap
        row = {"id": 3, "name": "Walk", "sport_type": "Walk",
               "distance": 0, "total_elevation_gain": 0, "start_date": "2024-01-01T00:00:00Z"}
        with app.app_context():
            doc = _activity_row_to_ap(row, "https://bikeodon.org/users/u",
                                      "https://bikeodon.org/users/u/outbox")
        assert "km" not in doc["object"]["content"]


class TestIsLocalActor:

    def test_local_is_true(self, app):
        from activitypub import _is_local_actor
        with app.app_context():
            assert _is_local_actor("https://bikeodon.org/users/alice") is True

    def test_remote_is_false(self, app):
        from activitypub import _is_local_actor
        with app.app_context():
            assert _is_local_actor("https://mastodon.social/users/bob") is False


class TestBuildActorDoc:

    def test_required_fields(self, app, user, keypair):
        from activitypub import _build_actor_doc
        pub_pem, _ = keypair
        with app.app_context():
            doc = _build_actor_doc(user["username"], user, pub_pem)
        assert doc["type"] == "Person"
        assert doc["preferredUsername"] == user["username"]
        assert "inbox" in doc
        assert "outbox" in doc
        assert doc["publicKey"]["publicKeyPem"] == pub_pem

    def test_display_name_included(self, app, db_path, user, keypair):
        from activitypub import _build_actor_doc
        from database import _conn
        pub_pem, _ = keypair
        conn = _conn(db_path)
        conn.execute("UPDATE users SET display_name=? WHERE id=?",
                     ("Real Name", user["id"]))
        conn.commit()
        conn.close()
        from database import get_user_by_username
        updated = dict(get_user_by_username(db_path, user["username"]))
        with app.app_context():
            doc = _build_actor_doc(user["username"], updated, pub_pem)
        assert doc.get("name") == "Real Name"


# ---------------------------------------------------------------------------
# 2. Keypair + HTTP signature round-trip
# ---------------------------------------------------------------------------

class TestKeypair:

    def test_generates_pem(self, keypair):
        pub, priv = keypair
        assert pub.startswith("-----BEGIN PUBLIC KEY-----")
        assert priv.startswith("-----BEGIN PRIVATE KEY-----")

    def test_idempotent(self, app, db_path, user):
        from activitypub import get_or_create_keypair
        with app.app_context():
            pub1, priv1 = get_or_create_keypair(db_path, user["id"])
            pub2, priv2 = get_or_create_keypair(db_path, user["id"])
        assert pub1 == pub2
        assert priv1 == priv2


class TestSignVerifyRoundTrip:

    def _make_req(self, headers: dict, path: str = "/inbox"):
        """Build a minimal mock request for _verify_http_signature."""
        from werkzeug.datastructures import Headers
        req = MagicMock()
        req.headers = Headers(list(headers.items()))
        req.path = path
        return req

    def test_valid_signature_accepted(self, app, keypair):
        from activitypub import _sign_headers, _verify_http_signature
        pub_pem, priv_pem = keypair
        body = b'{"type":"Follow"}'
        inbox = "https://remote.example/inbox"
        key_id = "https://bikeodon.org/users/alice#main-key"

        with app.app_context():
            hdrs = _sign_headers("POST", inbox, body, priv_pem, key_id)

        req = self._make_req(hdrs, path="/inbox")
        with patch("activitypub._fetch_public_key_pem", return_value=pub_pem):
            ok, reason = _verify_http_signature(req, body)
        assert ok is True, reason

    def test_tampered_body_rejected(self, app, keypair):
        from activitypub import _sign_headers, _verify_http_signature
        pub_pem, priv_pem = keypair
        body = b'{"type":"Follow"}'
        with app.app_context():
            hdrs = _sign_headers("POST", "https://remote.example/inbox", body, priv_pem,
                                 "https://bikeodon.org/users/alice#main-key")
        req = self._make_req(hdrs, path="/inbox")
        with patch("activitypub._fetch_public_key_pem", return_value=pub_pem):
            ok, reason = _verify_http_signature(req, b'{"type":"Evil"}')
        assert ok is False
        assert "digest" in reason

    def test_missing_signature_header_rejected(self):
        from activitypub import _verify_http_signature
        req = self._make_req({})
        ok, reason = _verify_http_signature(req, b"body")
        assert ok is False
        assert "Signature" in reason

    def test_unfetchable_key_rejected(self, app, keypair):
        from activitypub import _sign_headers, _verify_http_signature
        pub_pem, priv_pem = keypair
        body = b'{"type":"Follow"}'
        with app.app_context():
            hdrs = _sign_headers("POST", "https://remote.example/inbox", body, priv_pem,
                                 "https://bikeodon.org/users/alice#main-key")
        req = self._make_req(hdrs, path="/inbox")
        with patch("activitypub._fetch_public_key_pem", return_value=None):
            ok, reason = _verify_http_signature(req, body)
        assert ok is False
        assert "public key" in reason


class TestSignHeaders:

    def test_required_headers_present(self, app, keypair):
        from activitypub import _sign_headers
        _, priv_pem = keypair
        with app.app_context():
            hdrs = _sign_headers("POST", "https://remote.example/users/bob/inbox",
                                 b"body", priv_pem, "https://bikeodon.org/users/alice#main-key")
        assert "Signature" in hdrs
        assert "Digest" in hdrs
        assert "Date" in hdrs
        assert "Host" in hdrs

    def test_digest_matches_body(self, app, keypair):
        from activitypub import _sign_headers
        _, priv_pem = keypair
        body = b"hello world"
        with app.app_context():
            hdrs = _sign_headers("POST", "https://remote.example/inbox",
                                 body, priv_pem, "https://bikeodon.org/users/alice#main-key")
        expected = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()
        assert hdrs["Digest"] == expected

    def test_host_extracted_from_url(self, app, keypair):
        from activitypub import _sign_headers
        _, priv_pem = keypair
        with app.app_context():
            hdrs = _sign_headers("POST", "https://mastodon.social/inbox",
                                 b"x", priv_pem, "https://bikeodon.org/users/alice#main-key")
        assert hdrs["Host"] == "mastodon.social"


# ---------------------------------------------------------------------------
# 3. send_follow / send_unfollow / send_profile_update
# ---------------------------------------------------------------------------

class TestSendFollow:

    def test_enqueues_follow_activity(self, app, db_path, user, keypair):
        from activitypub import send_follow
        from database import get_following
        remote = "https://remote.example/users/bob"
        with patch("activitypub.requests.get", return_value=_fake_actor()) as mock_get:
            with app.app_context():
                send_follow(user["username"], user, remote, db_path)

        mock_get.assert_called_once_with(remote, headers={"Accept": "application/activity+json"},
                                         timeout=10)
        row = _latest_queue(db_path)
        assert row is not None
        activity = json.loads(row["activity_json"])
        assert activity["type"] == "Follow"
        assert activity["object"] == remote

    def test_adds_to_following_table(self, app, db_path, user):
        from activitypub import send_follow
        from database import get_following
        remote = "https://remote2.example/users/carol"
        with patch("activitypub.requests.get", return_value=_fake_actor(
            inbox="https://remote2.example/inbox", name="Carol"
        )):
            with app.app_context():
                send_follow(user["username"], user, remote, db_path)
                rows = get_following(db_path, user["username"])
        assert any(r["actor_url"] == remote for r in rows)

    def test_no_inbox_skips_delivery(self, app, db_path, user):
        from activitypub import send_follow
        before = len(_queue_rows(db_path))
        m = _fake_actor(inbox="")
        m.json.return_value["inbox"] = ""
        with patch("activitypub.requests.get", return_value=m):
            with app.app_context():
                send_follow(user["username"], user, "https://remote.example/users/nobody", db_path)
        assert len(_queue_rows(db_path)) == before

    def test_network_error_does_not_crash(self, app, db_path, user):
        from activitypub import send_follow
        with patch("activitypub.requests.get", side_effect=Exception("timeout")):
            with app.app_context():
                send_follow(user["username"], user,
                            "https://down.example/users/x", db_path)  # must not raise


class TestSendUnfollow:

    def test_enqueues_undo_and_removes_following(self, app, db_path, user):
        from activitypub import send_follow, send_unfollow
        from database import get_following
        remote = "https://unfollow.example/users/dave"
        with patch("activitypub.requests.get", return_value=_fake_actor(
            inbox="https://unfollow.example/inbox"
        )):
            with app.app_context():
                send_follow(user["username"], user, remote, db_path)
                before_count = len(_queue_rows(db_path))
                send_unfollow(user["username"], user, remote, db_path)

        row = _latest_queue(db_path)
        activity = json.loads(row["activity_json"])
        assert activity["type"] == "Undo"
        assert activity["object"]["type"] == "Follow"

        with app.app_context():
            remaining = get_following(db_path, user["username"])
        assert not any(r["actor_url"] == remote for r in remaining)

    def test_unfollow_unknown_actor_is_noop(self, app, db_path, user):
        from activitypub import send_unfollow
        before = len(_queue_rows(db_path))
        with app.app_context():
            send_unfollow(user["username"], user,
                          "https://never-followed.example/users/x", db_path)
        assert len(_queue_rows(db_path)) == before


class TestSendProfileUpdate:

    def test_fans_out_to_followers(self, app, db_path, user):
        from activitypub import send_profile_update
        from database import add_follower
        # Seed two followers
        with app.app_context():
            add_follower(db_path, user["username"],
                         "https://f1.example/users/f1", "https://f1.example/inbox")
            add_follower(db_path, user["username"],
                         "https://f2.example/users/f2", "https://f2.example/inbox")
            before = len(_queue_rows(db_path))
            send_profile_update(user["username"], user, db_path)

        new_rows = _queue_rows(db_path)[before:]
        assert len(new_rows) == 2
        for r in new_rows:
            a = json.loads(r["activity_json"])
            assert a["type"] == "Update"
            assert a["object"]["type"] == "Person"

    def test_no_followers_enqueues_nothing(self, app, db_path):
        from activitypub import send_profile_update
        from database import create_user, get_user_by_username
        # A fresh user with no followers
        create_user(db_path, "lonely_user", generate_password_hash("pw"))
        lonely = dict(get_user_by_username(db_path, "lonely_user"))
        before = len(_queue_rows(db_path))
        with app.app_context():
            send_profile_update("lonely_user", lonely, db_path)
        assert len(_queue_rows(db_path)) == before


# ---------------------------------------------------------------------------
# 4. send_like / send_unlike / send_boost / send_unboost / send_reply
# ---------------------------------------------------------------------------

REMOTE_ACTOR  = "https://remote.example/users/bob"
REMOTE_INBOX  = "https://remote.example/inbox"
REMOTE_OBJ    = "https://remote.example/users/bob/statuses/1"


class TestSendLikeUnlike:

    def test_like_enqueues_like_activity(self, app, db_path, user):
        from activitypub import send_like
        with patch("activitypub._resolve_inbox", return_value=REMOTE_INBOX):
            with app.app_context():
                send_like(user["username"], user, REMOTE_OBJ, REMOTE_ACTOR, db_path)
        row = _latest_queue(db_path)
        a = json.loads(row["activity_json"])
        assert a["type"] == "Like"
        assert a["object"] == REMOTE_OBJ

    def test_like_local_actor_is_skipped(self, app, db_path, user):
        from activitypub import send_like
        before = len(_queue_rows(db_path))
        local_actor = f"https://bikeodon.org/users/{user['username']}"
        with app.app_context():
            send_like(user["username"], user, REMOTE_OBJ, local_actor, db_path)
        assert len(_queue_rows(db_path)) == before

    def test_unlike_enqueues_undo_like(self, app, db_path, user):
        from activitypub import send_unlike
        with patch("activitypub._resolve_inbox", return_value=REMOTE_INBOX):
            with app.app_context():
                send_unlike(user["username"], user, REMOTE_OBJ, REMOTE_ACTOR, db_path)
        row = _latest_queue(db_path)
        a = json.loads(row["activity_json"])
        assert a["type"] == "Undo"
        assert a["object"]["type"] == "Like"

    def test_like_no_inbox_skips(self, app, db_path, user):
        from activitypub import send_like
        before = len(_queue_rows(db_path))
        with patch("activitypub._resolve_inbox", return_value=None):
            with app.app_context():
                send_like(user["username"], user, REMOTE_OBJ, REMOTE_ACTOR, db_path)
        assert len(_queue_rows(db_path)) == before


class TestSendBoostUnboost:

    def test_boost_enqueues_announce(self, app, db_path, user):
        from activitypub import send_boost
        with patch("activitypub._resolve_inbox", return_value=REMOTE_INBOX):
            with app.app_context():
                send_boost(user["username"], user, REMOTE_OBJ, REMOTE_ACTOR, db_path)
        row = _latest_queue(db_path)
        a = json.loads(row["activity_json"])
        assert a["type"] == "Announce"
        assert a["object"] == REMOTE_OBJ

    def test_unboost_enqueues_undo_announce(self, app, db_path, user):
        from activitypub import send_unboost
        with patch("activitypub._resolve_inbox", return_value=REMOTE_INBOX):
            with app.app_context():
                send_unboost(user["username"], user, REMOTE_OBJ, REMOTE_ACTOR, db_path)
        row = _latest_queue(db_path)
        a = json.loads(row["activity_json"])
        assert a["type"] == "Undo"
        assert a["object"]["type"] == "Announce"

    def test_boost_local_actor_skipped(self, app, db_path, user):
        from activitypub import send_boost
        before = len(_queue_rows(db_path))
        local_actor = f"https://bikeodon.org/users/{user['username']}"
        with app.app_context():
            send_boost(user["username"], user, REMOTE_OBJ, local_actor, db_path)
        assert len(_queue_rows(db_path)) == before


class TestSendReply:

    def test_enqueues_create_note(self, app, db_path, user):
        from activitypub import send_reply
        with patch("activitypub._resolve_inbox", return_value=REMOTE_INBOX):
            with app.app_context():
                send_reply(user["username"], user,
                           REMOTE_OBJ, REMOTE_ACTOR, "Great post!", db_path)
        row = _latest_queue(db_path)
        a = json.loads(row["activity_json"])
        assert a["type"] == "Create"
        assert a["object"]["type"] == "Note"
        assert a["object"]["inReplyTo"] == REMOTE_OBJ
        assert "Great post!" in a["object"]["content"]

    def test_no_inbox_skips(self, app, db_path, user):
        from activitypub import send_reply
        before = len(_queue_rows(db_path))
        with patch("activitypub._resolve_inbox", return_value=None):
            with app.app_context():
                send_reply(user["username"], user,
                           REMOTE_OBJ, REMOTE_ACTOR, "hi", db_path)
        assert len(_queue_rows(db_path)) == before


class TestResolveInbox:

    def test_cache_hit_from_following_table(self, app, db_path, user):
        from activitypub import _resolve_inbox
        from database import add_following
        cached_inbox = "https://cached.example/inbox"
        with app.app_context():
            add_following(db_path, user["username"],
                          "https://cached.example/users/bob", cached_inbox)
            result = _resolve_inbox("https://cached.example/users/bob", db_path, user["username"])
        assert result == cached_inbox

    def test_fallback_to_fetch(self, app, db_path, user):
        from activitypub import _resolve_inbox
        m = MagicMock()
        m.ok = True
        m.json.return_value = {"inbox": "https://fetched.example/inbox"}
        with patch("activitypub.requests.get", return_value=m):
            with app.app_context():
                result = _resolve_inbox("https://fetched.example/users/x",
                                        db_path, user["username"])
        assert result == "https://fetched.example/inbox"

    def test_fetch_failure_returns_none(self, app, db_path, user):
        from activitypub import _resolve_inbox
        with patch("activitypub.requests.get", side_effect=Exception("down")):
            with app.app_context():
                result = _resolve_inbox("https://down.example/users/x",
                                        db_path, user["username"])
        assert result is None


# ---------------------------------------------------------------------------
# 5. Delivery worker
# ---------------------------------------------------------------------------

def _seed_delivery(db_path, inbox_url, activity, key_id, created_ago_secs=0):
    """Insert a delivery_queue row directly, optionally backdating created_at."""
    conn = sqlite3.connect(db_path)
    if created_ago_secs:
        created = (datetime.now(timezone.utc) -
                   timedelta(seconds=created_ago_secs)).isoformat()
        conn.execute(
            "INSERT INTO delivery_queue (inbox_url, activity_json, key_id, created_at)"
            " VALUES (?,?,?,?)",
            (inbox_url, json.dumps(activity), key_id, created),
        )
    else:
        conn.execute(
            "INSERT INTO delivery_queue (inbox_url, activity_json, key_id) VALUES (?,?,?)",
            (inbox_url, json.dumps(activity), key_id),
        )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return row_id


def _queue_status(db_path, row_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM delivery_queue WHERE id=?", (row_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


class TestDeliveryWorker:

    def _key_id(self, username):
        return f"https://bikeodon.org/users/{username}#main-key"

    def test_success_marks_sent(self, app, db_path, user, keypair):
        from activitypub import _process_due_deliveries
        activity = {"type": "Follow", "actor": "https://bikeodon.org/users/x"}
        row_id = _seed_delivery(db_path, REMOTE_INBOX, activity, self._key_id(user["username"]))
        with patch("activitypub._do_http_post"):
            with app.app_context():
                _process_due_deliveries(db_path)
        assert _queue_status(db_path, row_id)["status"] == "sent"

    def test_failure_increments_attempts(self, app, db_path, user, keypair):
        from activitypub import _process_due_deliveries
        activity = {"type": "Like"}
        row_id = _seed_delivery(db_path, REMOTE_INBOX, activity, self._key_id(user["username"]))
        with patch("activitypub._do_http_post", side_effect=Exception("HTTP 503")):
            with app.app_context():
                _process_due_deliveries(db_path)
        row = _queue_status(db_path, row_id)
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        assert "503" in row["last_error"]

    def test_failure_applies_exponential_backoff(self, app, db_path, user, keypair):
        from activitypub import _process_due_deliveries, _BACKOFF_INITIAL
        activity = {"type": "Announce"}
        row_id = _seed_delivery(db_path, REMOTE_INBOX, activity, self._key_id(user["username"]))
        with patch("activitypub._do_http_post", side_effect=Exception("fail")):
            with app.app_context():
                _process_due_deliveries(db_path)
        row = _queue_status(db_path, row_id)
        # next_attempt_at must be in the future (at least BACKOFF_INITIAL - epsilon seconds away)
        next_at = datetime.fromisoformat(row["next_attempt_at"])
        if next_at.tzinfo is None:
            next_at = next_at.replace(tzinfo=timezone.utc)
        assert next_at > datetime.now(timezone.utc)

    def test_aged_out_failure_marks_failed(self, app, db_path, user, keypair):
        from activitypub import _process_due_deliveries, _DELIVERY_TTL
        activity = {"type": "Create"}
        # Backdate by more than TTL (3 days)
        row_id = _seed_delivery(db_path, REMOTE_INBOX, activity,
                                self._key_id(user["username"]),
                                created_ago_secs=_DELIVERY_TTL + 3600)
        with patch("activitypub._do_http_post", side_effect=Exception("still failing")):
            with app.app_context():
                _process_due_deliveries(db_path)
        assert _queue_status(db_path, row_id)["status"] == "failed"

    def test_unknown_user_in_key_id_increments_attempts(self, app, db_path):
        from activitypub import _process_due_deliveries
        activity = {"type": "Follow"}
        key_id = "https://bikeodon.org/users/ghost_user_xyz#main-key"
        row_id = _seed_delivery(db_path, REMOTE_INBOX, activity, key_id)
        with app.app_context():
            _process_due_deliveries(db_path)  # must not crash
        row = _queue_status(db_path, row_id)
        assert row["status"] == "pending"
        assert row["attempts"] == 1


class TestDoHttpPost:

    def test_410_treated_as_success(self, app, keypair):
        from activitypub import _do_http_post
        _, priv_pem = keypair
        m = MagicMock()
        m.status_code = 410
        m.ok = False
        with patch("activitypub.requests.post", return_value=m):
            with app.app_context():
                # Should return None (not raise)
                result = _do_http_post(REMOTE_INBOX, b"body", priv_pem,
                                       "https://bikeodon.org/users/u#main-key")
        assert result is None

    def test_non_2xx_raises(self, app, keypair):
        from activitypub import _do_http_post
        _, priv_pem = keypair
        m = MagicMock()
        m.status_code = 500
        m.ok = False
        m.text = "Internal Server Error"
        with patch("activitypub.requests.post", return_value=m):
            with app.app_context():
                with pytest.raises(Exception, match="HTTP 500"):
                    _do_http_post(REMOTE_INBOX, b"body", priv_pem,
                                  "https://bikeodon.org/users/u#main-key")
