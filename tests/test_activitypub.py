"""
Tests for ActivityPub federation endpoints.

  Layer 3 — Inbox + Followers  (added below)

Covers the two layers needed before any federation can work:

  Layer 1 — WebFinger  (/.well-known/webfinger)
      Discovery: lets remote servers find us given an account handle like
      @tim0s42@bikeodon.org.

  Layer 2 — Actor  (/users/{username})
      Identity: the JSON-LD document that describes a user and declares their
      inbox, outbox, public key, etc.

Each test is written against the spec *before* the endpoints exist, so
running the suite tells you exactly what still needs to be built.

Run with:
    pytest tests/test_activitypub.py -v
"""

import json
import os
import tempfile
from unittest.mock import patch, MagicMock
import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app(tmp_path):
    """
    Spin up the Flask app with an isolated temp DB and a minimal config.
    Patches the module-level DB_PATH and _base_cfg so the real bikeodon.db
    is never touched.
    """
    import yaml

    # Minimal config that satisfies app.py bootstrap
    cfg = {
        "database": {"path": str(tmp_path / "test.db")},
        "daemon":   {"interval_minutes": 15},
        "map":      {"output_dir": str(tmp_path / "output"), "tiles": {}},
    }
    cfg_path = str(tmp_path / "config.yaml")
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)

    os.environ["BIKEODON_CONFIG"] = cfg_path
    os.environ["FLASK_SECRET_KEY"] = "test-secret"

    # Import after env is set so module-level bootstrap picks up the temp config
    import importlib
    import app as app_module
    importlib.reload(app_module)

    app_module.app.config["TESTING"] = True
    app_module.app.config["SERVER_NAME"] = "bikeodon.org"
    app_module.app.config["PREFERRED_URL_SCHEME"] = "https"

    yield app_module.app

    os.environ.pop("BIKEODON_CONFIG", None)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def user(app):
    """Create a test user and return (username, user_id)."""
    from werkzeug.security import generate_password_hash
    import app as app_module
    from database import create_user

    username = "tim0s42"
    uid = create_user(app_module.DB_PATH, username, generate_password_hash("pw"))
    return username, uid


# ---------------------------------------------------------------------------
# Layer 1 — WebFinger
# ---------------------------------------------------------------------------

class TestWebFinger:
    """
    RFC 7033 + ActivityPub §4.1
    GET /.well-known/webfinger?resource=acct:{username}@{domain}
    """

    def test_known_account_returns_200(self, client, user):
        username, _ = user
        r = client.get(f"/.well-known/webfinger?resource=acct:{username}@bikeodon.org")
        assert r.status_code == 200

    def test_route_exists(self, client):
        """Webfinger route must be registered — any non-405 response proves it."""
        r = client.get("/.well-known/webfinger?resource=acct:x@bikeodon.org")
        assert r.status_code != 405, "route not registered"

    def test_unknown_account_returns_404(self, client, user):
        r = client.get("/.well-known/webfinger?resource=acct:nobody@bikeodon.org")
        assert r.status_code == 404

    def test_missing_resource_returns_400(self, client):
        r = client.get("/.well-known/webfinger")
        assert r.status_code == 400

    def test_content_type_is_jrd_json(self, client, user):
        username, _ = user
        r = client.get(f"/.well-known/webfinger?resource=acct:{username}@bikeodon.org")
        assert "application/jrd+json" in r.content_type

    def test_subject_matches_request(self, client, user):
        username, _ = user
        r = client.get(f"/.well-known/webfinger?resource=acct:{username}@bikeodon.org")
        data = r.get_json()
        assert data["subject"] == f"acct:{username}@bikeodon.org"

    def test_has_self_link(self, client, user):
        username, _ = user
        r = client.get(f"/.well-known/webfinger?resource=acct:{username}@bikeodon.org")
        data = r.get_json()
        self_links = [l for l in data.get("links", []) if l.get("rel") == "self"]
        assert len(self_links) == 1, "exactly one 'self' link required"

    def test_self_link_type_is_activity_json(self, client, user):
        username, _ = user
        r = client.get(f"/.well-known/webfinger?resource=acct:{username}@bikeodon.org")
        data = r.get_json()
        self_link = next(l for l in data["links"] if l["rel"] == "self")
        assert self_link["type"] == "application/activity+json"

    def test_self_link_href_points_to_actor(self, client, user):
        username, _ = user
        r = client.get(f"/.well-known/webfinger?resource=acct:{username}@bikeodon.org")
        data = r.get_json()
        self_link = next(l for l in data["links"] if l["rel"] == "self")
        # href must be an https URL containing the username
        href = self_link["href"]
        assert href.startswith("https://bikeodon.org")
        assert username in href

    def test_cross_domain_resource_returns_404(self, client, user):
        # We must not serve WebFinger for accounts on other domains
        username, _ = user
        r = client.get(f"/.well-known/webfinger?resource=acct:{username}@evil.example")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Layer 2 — Actor
# ---------------------------------------------------------------------------

class TestActor:
    """
    ActivityPub §4.1  /users/{username}
    Must be served with Accept: application/activity+json (or ld+json with
    profile). Regular browsers still get the HTML profile page.
    """

    AP_ACCEPT = "application/activity+json"

    def test_actor_returns_200_for_known_user(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        assert r.status_code == 200

    def test_actor_route_exists(self, client):
        """Actor route must be registered — any non-405 response proves it."""
        r = client.get("/users/nobody", headers={"Accept": self.AP_ACCEPT})
        assert r.status_code != 405, "route not registered"

    def test_actor_returns_404_for_unknown_user(self, client):
        r = client.get("/users/nobody", headers={"Accept": self.AP_ACCEPT})
        assert r.status_code == 404

    def test_actor_content_type_is_activity_json(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        assert "application/activity+json" in r.content_type

    def test_actor_has_required_context(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        data = r.get_json()
        ctx = data.get("@context", [])
        ctx_list = ctx if isinstance(ctx, list) else [ctx]
        assert "https://www.w3.org/ns/activitystreams" in ctx_list

    def test_actor_type_is_person(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        data = r.get_json()
        assert data["type"] == "Person"

    def test_actor_id_is_canonical_url(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        data = r.get_json()
        assert data["id"] == f"https://bikeodon.org/users/{username}"

    def test_actor_preferred_username_matches(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        data = r.get_json()
        assert data["preferredUsername"] == username

    def test_actor_has_inbox(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        data = r.get_json()
        assert "inbox" in data
        assert data["inbox"].startswith("https://bikeodon.org")

    def test_actor_has_outbox(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        data = r.get_json()
        assert "outbox" in data
        assert data["outbox"].startswith("https://bikeodon.org")

    def test_actor_has_followers_and_following(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        data = r.get_json()
        assert "followers" in data
        assert "following" in data

    def test_actor_has_public_key(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        data = r.get_json()
        pk = data.get("publicKey", {})
        assert pk.get("id") == f"https://bikeodon.org/users/{username}#main-key"
        assert pk.get("owner") == f"https://bikeodon.org/users/{username}"
        assert "publicKeyPem" in pk
        # PEM block must be a real RSA public key
        assert pk["publicKeyPem"].startswith("-----BEGIN PUBLIC KEY-----")

    def test_actor_public_key_is_unique_per_user(self, client, user, app):
        """Two users must not share a key — each needs their own RSA keypair."""
        from werkzeug.security import generate_password_hash
        import app as app_module
        from database import create_user

        username2 = "other_rider"
        create_user(app_module.DB_PATH, username2, generate_password_hash("pw2"))

        username, _ = user
        r1 = client.get(f"/users/{username}",  headers={"Accept": self.AP_ACCEPT})
        r2 = client.get(f"/users/{username2}", headers={"Accept": self.AP_ACCEPT})

        pem1 = r1.get_json()["publicKey"]["publicKeyPem"]
        pem2 = r2.get_json()["publicKey"]["publicKeyPem"]
        assert pem1 != pem2

    def test_html_accept_returns_html_not_json(self, client, user):
        """Regular browser visit must still get the HTML profile, not JSON."""
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": "text/html"})
        assert r.status_code == 200
        assert "text/html" in r.content_type


# ---------------------------------------------------------------------------
# Layer 2b — Actor profile fields
# ---------------------------------------------------------------------------

class TestActorProfile:
    """
    Actor document must expose display_name, summary, and avatar icon
    when set, and omit name/summary when not set.
    """

    AP_ACCEPT = "application/activity+json"

    def _set_profile(self, db_path, user_id, **kwargs):
        from database import _conn
        conn = _conn(db_path)
        sets = ", ".join(f"{k}=?" for k in kwargs)
        conn.execute(f"UPDATE users SET {sets} WHERE id=?", (*kwargs.values(), user_id))
        conn.commit()

    def test_actor_has_icon_always(self, client, user):
        """icon is always present — falls back to the default avatar."""
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        data = r.get_json()
        assert "icon" in data
        assert data["icon"]["type"] == "Image"
        assert "url" in data["icon"]
        assert "mediaType" in data["icon"]

    def test_actor_name_absent_when_not_set(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        assert "name" not in r.get_json()

    def test_actor_summary_absent_when_not_set(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        assert "summary" not in r.get_json()

    def test_actor_name_present_when_set(self, client, user, app):
        import app as app_module
        username, uid = user
        self._set_profile(app_module.DB_PATH, uid, display_name="Tim Schneider")
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        assert r.get_json()["name"] == "Tim Schneider"

    def test_actor_summary_present_when_set(self, client, user, app):
        import app as app_module
        username, uid = user
        self._set_profile(app_module.DB_PATH, uid, summary="Cycling enthusiast")
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        assert r.get_json()["summary"] == "Cycling enthusiast"

    def test_actor_icon_url_points_to_avatar_route(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}", headers={"Accept": self.AP_ACCEPT})
        icon_url = r.get_json()["icon"]["url"]
        assert f"/users/{username}/avatar" in icon_url

    def test_avatar_route_returns_image(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}/avatar")
        assert r.status_code == 200
        assert r.content_type.startswith("image/")


# ---------------------------------------------------------------------------
# Layer 3 — Inbox (Follow / Undo) + Followers collection
# ---------------------------------------------------------------------------

class TestInboxFollow:
    """
    POST /users/{username}/inbox   — remote servers push Follow/Undo activities
    GET  /users/{username}/followers — public OrderedCollection of follower URLs

    Outgoing network calls (actor fetch + Accept delivery) are mocked so these
    tests run entirely in-process with no real HTTP traffic.
    Signature verification is bypassed via an autouse fixture; it is tested
    separately in TestHttpSignatureVerification.
    """

    AP           = "application/activity+json"

    @pytest.fixture(autouse=True)
    def bypass_sig_verification(self):
        with patch("activitypub._verify_http_signature", return_value=(True, "ok")):
            yield
    REMOTE_ACTOR = "https://mastodon.social/users/alice"
    REMOTE_INBOX = "https://mastodon.social/users/alice/inbox"

    # ---- activity builders ----

    def _follow(self, username):
        return {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{self.REMOTE_ACTOR}#follows/1",
            "type": "Follow",
            "actor": self.REMOTE_ACTOR,
            "object": f"https://bikeodon.org/users/{username}",
        }

    def _undo(self, username):
        return {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{self.REMOTE_ACTOR}#follows/1/undo",
            "type": "Undo",
            "actor": self.REMOTE_ACTOR,
            "object": self._follow(username),
        }

    def _remote_actor_mock(self):
        """Mock for requests.get that returns a minimal remote actor document."""
        m = MagicMock()
        m.json.return_value = {
            "id": self.REMOTE_ACTOR,
            "type": "Person",
            "inbox": self.REMOTE_INBOX,
        }
        m.ok = True
        return m

    # ---- inbox basic behaviour ----

    def test_inbox_route_exists(self, client, user):
        username, _ = user
        r = client.post(f"/users/{username}/inbox", data="{}", content_type=self.AP)
        assert r.status_code != 405

    def test_inbox_unknown_user_returns_404(self, client):
        r = client.post("/users/nobody/inbox", data="{}", content_type=self.AP)
        assert r.status_code == 404

    def test_inbox_non_json_returns_400(self, client, user):
        username, _ = user
        r = client.post(
            f"/users/{username}/inbox",
            data="not json at all",
            content_type="text/plain",
        )
        assert r.status_code == 400

    # ---- Follow handling ----

    def test_follow_returns_202(self, client, user):
        username, _ = user
        with patch("activitypub.requests") as mock_req, \
             patch("activitypub._deliver_activity"):
            mock_req.get.return_value = self._remote_actor_mock()
            r = client.post(
                f"/users/{username}/inbox",
                json=self._follow(username),
                content_type=self.AP,
            )
        assert r.status_code == 202

    def test_follow_stores_actor_in_followers_table(self, client, user, app):
        import app as app_module
        from database import get_followers
        username, _ = user
        with patch("activitypub.requests") as mock_req, \
             patch("activitypub._deliver_activity"):
            mock_req.get.return_value = self._remote_actor_mock()
            client.post(
                f"/users/{username}/inbox",
                json=self._follow(username),
                content_type=self.AP,
            )
        followers = get_followers(app_module.DB_PATH, username)
        assert any(f["actor_url"] == self.REMOTE_ACTOR for f in followers)

    def test_follow_triggers_accept_delivery(self, client, user):
        username, _ = user
        with patch("activitypub.requests") as mock_req, \
             patch("activitypub._deliver_activity") as mock_deliver:
            mock_req.get.return_value = self._remote_actor_mock()
            client.post(
                f"/users/{username}/inbox",
                json=self._follow(username),
                content_type=self.AP,
            )
        assert mock_deliver.called
        inbox_url, accept_doc, _key_id, _db = mock_deliver.call_args[0]
        assert inbox_url == self.REMOTE_INBOX
        assert accept_doc["type"] == "Accept"
        assert accept_doc["object"]["type"] == "Follow"

    def test_accept_actor_is_local_user(self, client, user):
        username, _ = user
        with patch("activitypub.requests") as mock_req, \
             patch("activitypub._deliver_activity") as mock_deliver:
            mock_req.get.return_value = self._remote_actor_mock()
            client.post(
                f"/users/{username}/inbox",
                json=self._follow(username),
                content_type=self.AP,
            )
        _, accept_doc, _, _ = mock_deliver.call_args[0]
        assert accept_doc["actor"] == f"https://bikeodon.org/users/{username}"

    def test_duplicate_follow_is_idempotent(self, client, user, app):
        import app as app_module
        from database import get_followers
        username, _ = user
        with patch("activitypub.requests") as mock_req, \
             patch("activitypub._deliver_activity"):
            mock_req.get.return_value = self._remote_actor_mock()
            for _ in range(2):
                client.post(
                    f"/users/{username}/inbox",
                    json=self._follow(username),
                    content_type=self.AP,
                )
        followers = get_followers(app_module.DB_PATH, username)
        assert len([f for f in followers if f["actor_url"] == self.REMOTE_ACTOR]) == 1

    # ---- Undo{Follow} handling ----

    def test_undo_follow_returns_202(self, client, user):
        username, _ = user
        with patch("activitypub.requests") as mock_req, \
             patch("activitypub._deliver_activity"):
            mock_req.get.return_value = self._remote_actor_mock()
            client.post(f"/users/{username}/inbox", json=self._follow(username), content_type=self.AP)
        r = client.post(f"/users/{username}/inbox", json=self._undo(username), content_type=self.AP)
        assert r.status_code == 202

    def test_undo_follow_removes_follower(self, client, user, app):
        import app as app_module
        from database import get_followers
        username, _ = user
        with patch("activitypub.requests") as mock_req, \
             patch("activitypub._deliver_activity"):
            mock_req.get.return_value = self._remote_actor_mock()
            client.post(f"/users/{username}/inbox", json=self._follow(username), content_type=self.AP)
        client.post(f"/users/{username}/inbox", json=self._undo(username), content_type=self.AP)
        followers = get_followers(app_module.DB_PATH, username)
        assert not any(f["actor_url"] == self.REMOTE_ACTOR for f in followers)

    # ---- Followers collection ----

    def test_followers_returns_200(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}/followers", headers={"Accept": self.AP})
        assert r.status_code == 200

    def test_followers_is_ordered_collection(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}/followers", headers={"Accept": self.AP})
        assert r.get_json()["type"] == "OrderedCollection"

    def test_followers_empty_by_default(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}/followers", headers={"Accept": self.AP})
        data = r.get_json()
        assert data["totalItems"] == 0
        assert data["orderedItems"] == []

    def test_followers_count_after_follow(self, client, user):
        username, _ = user
        with patch("activitypub.requests") as mock_req, \
             patch("activitypub._deliver_activity"):
            mock_req.get.return_value = self._remote_actor_mock()
            client.post(f"/users/{username}/inbox", json=self._follow(username), content_type=self.AP)
        r = client.get(f"/users/{username}/followers", headers={"Accept": self.AP})
        data = r.get_json()
        assert data["totalItems"] == 1
        assert self.REMOTE_ACTOR in data["orderedItems"]

    def test_followers_unknown_user_returns_404(self, client):
        r = client.get("/users/nobody/followers", headers={"Accept": "application/activity+json"})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Layer 3b — HTTP Signature verification
# ---------------------------------------------------------------------------

class TestHttpSignatureVerification:
    """
    End-to-end: sign a request with a real RSA keypair using _sign_headers,
    then POST it to the inbox.  Each test exercises one rejection path.
    The valid-signature test is the only one that reaches _handle_follow.
    """

    AP           = "application/activity+json"
    REMOTE_ACTOR = "https://mastodon.social/users/alice"
    REMOTE_INBOX = "https://mastodon.social/users/alice/inbox"
    KEY_ID       = "https://mastodon.social/users/alice#main-key"

    # ---- fixtures ----

    @pytest.fixture(autouse=True)
    def clear_key_cache(self):
        import activitypub
        activitypub._pubkey_cache.clear()
        yield
        activitypub._pubkey_cache.clear()

    @pytest.fixture
    def keypair(self):
        from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
        from cryptography.hazmat.primitives import serialization as _ser
        priv = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = priv.private_bytes(
            _ser.Encoding.PEM, _ser.PrivateFormat.PKCS8, _ser.NoEncryption()
        ).decode()
        pub_pem = priv.public_key().public_bytes(
            _ser.Encoding.PEM, _ser.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        return priv_pem, pub_pem

    # ---- helpers ----

    def _actor_mock(self, pub_pem):
        m = MagicMock()
        m.ok = True
        m.json.return_value = {
            "id":    self.REMOTE_ACTOR,
            "type":  "Person",
            "inbox": self.REMOTE_INBOX,
            "publicKey": {
                "id":           self.KEY_ID,
                "owner":        self.REMOTE_ACTOR,
                "publicKeyPem": pub_pem,
            },
        }
        return m

    def _signed_post(self, username, priv_pem, body=None):
        """Return (body_bytes, headers) for a signed Follow POST."""
        from activitypub import _sign_headers
        if body is None:
            body = json.dumps({
                "@context": "https://www.w3.org/ns/activitystreams",
                "id": f"{self.REMOTE_ACTOR}#follows/1",
                "type": "Follow",
                "actor": self.REMOTE_ACTOR,
                "object": f"https://bikeodon.org/users/{username}",
            }).encode()
        headers = _sign_headers(
            "POST",
            f"https://bikeodon.org/users/{username}/inbox",
            body, priv_pem, self.KEY_ID,
        )
        return body, headers

    def _post(self, client, username, body, headers):
        # Separate Content-Type so Flask test client handles it correctly
        extra = {k: v for k, v in headers.items() if k != "Content-Type"}
        return client.post(
            f"/users/{username}/inbox",
            data=body,
            headers=extra,
            content_type=self.AP,
        )

    # ---- tests ----

    def test_valid_signature_accepted(self, client, user, keypair):
        priv_pem, pub_pem = keypair
        username, _ = user
        body, hdrs = self._signed_post(username, priv_pem)
        with patch("activitypub.requests") as mock_req, \
             patch("activitypub._deliver_activity"):
            mock_req.get.return_value = self._actor_mock(pub_pem)
            r = self._post(client, username, body, hdrs)
        assert r.status_code == 202

    def test_missing_signature_returns_401(self, client, user):
        username, _ = user
        r = client.post(
            f"/users/{username}/inbox",
            data=json.dumps({"type": "Follow", "actor": self.REMOTE_ACTOR}).encode(),
            content_type=self.AP,
        )
        assert r.status_code == 401

    def test_wrong_key_returns_401(self, client, user, keypair):
        """Signs with one key but presents a different public key — should fail."""
        priv_pem, pub_pem = keypair
        from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
        from cryptography.hazmat.primitives import serialization as _ser
        other_priv = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_priv_pem = other_priv.private_bytes(
            _ser.Encoding.PEM, _ser.PrivateFormat.PKCS8, _ser.NoEncryption()
        ).decode()

        username, _ = user
        body, hdrs = self._signed_post(username, other_priv_pem)  # signed with wrong key
        with patch("activitypub.requests") as mock_req:
            mock_req.get.return_value = self._actor_mock(pub_pem)  # but we present original pub key
            r = self._post(client, username, body, hdrs)
        assert r.status_code == 401

    def test_tampered_body_returns_401(self, client, user, keypair):
        """Body changed after signing — digest mismatch."""
        priv_pem, pub_pem = keypair
        username, _ = user
        body, hdrs = self._signed_post(username, priv_pem)
        tampered = body + b" tampered"
        with patch("activitypub.requests") as mock_req:
            mock_req.get.return_value = self._actor_mock(pub_pem)
            r = self._post(client, username, tampered, hdrs)
        assert r.status_code == 401

    def test_stale_date_returns_401(self, client, user, keypair):
        """Request signed with a date one hour in the past."""
        import time
        from email.utils import formatdate
        priv_pem, pub_pem = keypair
        username, _ = user
        stale = formatdate(timeval=time.time() - 3600, usegmt=True)
        with patch("activitypub.formatdate", return_value=stale):
            body, hdrs = self._signed_post(username, priv_pem)
        with patch("activitypub.requests") as mock_req:
            mock_req.get.return_value = self._actor_mock(pub_pem)
            r = self._post(client, username, body, hdrs)
        assert r.status_code == 401

    def test_unreachable_key_returns_401(self, client, user, keypair):
        """Remote server unavailable — can't fetch public key."""
        priv_pem, _ = keypair
        username, _ = user
        body, hdrs = self._signed_post(username, priv_pem)
        with patch("activitypub.requests") as mock_req:
            mock_req.get.side_effect = Exception("connection refused")
            r = self._post(client, username, body, hdrs)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Delivery queue
# ---------------------------------------------------------------------------

class TestDeliveryQueue:
    """
    _deliver_activity must enqueue immediately and return; the worker
    thread does the actual HTTP POST with exponential backoff and a
    3-day give-up window.
    """

    KEY_ID    = "https://bikeodon.org/users/tim0s42#main-key"
    INBOX_URL = "https://mastodon.social/users/alice/inbox"

    def _activity(self):
        return {"@context": "https://www.w3.org/ns/activitystreams",
                "type": "Create", "actor": "https://bikeodon.org/users/tim0s42"}

    # ---- enqueue ----

    def test_deliver_enqueues_not_sends(self, app, user):
        """_deliver_activity must not make any HTTP call — it only queues."""
        import app as app_module
        from activitypub import _deliver_activity
        username, _ = user
        with patch("activitypub.requests") as mock_req:
            _deliver_activity(self.INBOX_URL, self._activity(), self.KEY_ID, app_module.DB_PATH)
            mock_req.post.assert_not_called()

    def test_deliver_stores_row_in_db(self, app, user):
        import app as app_module
        from activitypub import _deliver_activity
        from database import get_due_deliveries
        username, _ = user
        _deliver_activity(self.INBOX_URL, self._activity(), self.KEY_ID, app_module.DB_PATH)
        rows = get_due_deliveries(app_module.DB_PATH)
        assert any(r["inbox_url"] == self.INBOX_URL for r in rows)

    # ---- worker: successful delivery ----

    def test_worker_delivers_and_marks_sent(self, app, user):
        import app as app_module
        from activitypub import _deliver_activity, _process_due_deliveries
        from database import get_due_deliveries
        username, _ = user
        _deliver_activity(self.INBOX_URL, self._activity(), self.KEY_ID, app_module.DB_PATH)

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 202
        with patch("activitypub.requests") as mock_req:
            mock_req.post.return_value = mock_resp
            _process_due_deliveries(app_module.DB_PATH)

        assert get_due_deliveries(app_module.DB_PATH) == []

    def test_worker_treats_410_gone_as_success(self, app, user):
        """410 Gone means the remote account is deleted — don't retry."""
        import app as app_module
        from activitypub import _deliver_activity, _process_due_deliveries
        from database import get_due_deliveries
        username, _ = user
        _deliver_activity(self.INBOX_URL, self._activity(), self.KEY_ID, app_module.DB_PATH)

        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 410
        with patch("activitypub.requests") as mock_req:
            mock_req.post.return_value = mock_resp
            _process_due_deliveries(app_module.DB_PATH)

        assert get_due_deliveries(app_module.DB_PATH) == []

    # ---- worker: failure / retry ----

    def test_worker_reschedules_on_failure(self, app, user):
        """On HTTP 500, the delivery must be rescheduled (not deleted)."""
        import app as app_module
        from activitypub import _deliver_activity, _process_due_deliveries
        from database import _conn
        username, _ = user
        _deliver_activity(self.INBOX_URL, self._activity(), self.KEY_ID, app_module.DB_PATH)

        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 500
        mock_resp.text = "server error"
        with patch("activitypub.requests") as mock_req:
            mock_req.post.return_value = mock_resp
            _process_due_deliveries(app_module.DB_PATH)

        conn = _conn(app_module.DB_PATH)
        row = conn.execute("SELECT attempts, status, last_error FROM delivery_queue").fetchone()
        assert row["attempts"] == 1
        assert row["status"] == "pending"
        assert "500" in row["last_error"]

    def test_worker_gives_up_after_3_days(self, app, user):
        """A delivery that is > 3 days old must be marked failed, not retried."""
        import app as app_module
        from activitypub import _deliver_activity, _process_due_deliveries
        from database import _conn
        from datetime import datetime, timezone, timedelta
        username, _ = user
        _deliver_activity(self.INBOX_URL, self._activity(), self.KEY_ID, app_module.DB_PATH)

        # Back-date created_at to 4 days ago
        old_ts = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        conn = _conn(app_module.DB_PATH)
        conn.execute("UPDATE delivery_queue SET created_at=?", (old_ts,))
        conn.commit()

        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 503
        mock_resp.text = "unavailable"
        with patch("activitypub.requests") as mock_req:
            mock_req.post.return_value = mock_resp
            _process_due_deliveries(app_module.DB_PATH)

        row = conn.execute("SELECT status FROM delivery_queue").fetchone()
        assert row["status"] == "failed"

    def test_backoff_grows_exponentially(self, app, user):
        """Each failure must push next_attempt_at further into the future."""
        import app as app_module
        from activitypub import _deliver_activity, _process_due_deliveries
        from database import _conn
        username, _ = user

        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 503
        mock_resp.text = "unavailable"
        with patch("activitypub.requests") as mock_req:
            mock_req.post.return_value = mock_resp

            _deliver_activity(self.INBOX_URL, self._activity(), self.KEY_ID, app_module.DB_PATH)
            conn = _conn(app_module.DB_PATH)

            # Force next_attempt_at to now so the worker picks it up each time
            delays = []
            for _ in range(3):
                conn.execute("UPDATE delivery_queue SET next_attempt_at=datetime('now')")
                conn.commit()
                before = conn.execute("SELECT next_attempt_at FROM delivery_queue").fetchone()[0]
                _process_due_deliveries(app_module.DB_PATH)
                after = conn.execute("SELECT next_attempt_at FROM delivery_queue").fetchone()[0]
                delays.append(after)

        # Each successive next_attempt_at must be later than the previous
        assert delays[0] < delays[1] < delays[2]


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------

class TestOutbox:
    AP = "application/activity+json"

    def _seed_activity(self, db_path, user_id):
        from database import upsert_activity
        upsert_activity(db_path, {
            "id": 1001,
            "name": "Morning Ride",
            "sport_type": "Ride",
            "start_date": "2026-06-01T07:00:00Z",
            "distance": 45200,
            "total_elevation_gain": 320,
            "moving_time": 5400,
            "elapsed_time": 5600,
        }, user_id)

    def test_outbox_returns_200(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}/outbox", headers={"Accept": self.AP})
        assert r.status_code == 200

    def test_outbox_content_type(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}/outbox", headers={"Accept": self.AP})
        assert "application/activity+json" in r.content_type

    def test_outbox_is_ordered_collection(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}/outbox", headers={"Accept": self.AP})
        assert r.get_json()["type"] == "OrderedCollection"

    def test_outbox_has_total_items(self, client, user, app):
        import app as app_module
        username, uid = user
        self._seed_activity(app_module.DB_PATH, uid)
        r = client.get(f"/users/{username}/outbox", headers={"Accept": self.AP})
        assert r.get_json()["totalItems"] == 1

    def test_outbox_has_first_page_link(self, client, user):
        username, _ = user
        r = client.get(f"/users/{username}/outbox", headers={"Accept": self.AP})
        data = r.get_json()
        assert "first" in data
        assert "page=true" in data["first"]

    def test_outbox_page_returns_create_activities(self, client, user, app):
        import app as app_module
        username, uid = user
        self._seed_activity(app_module.DB_PATH, uid)
        r = client.get(f"/users/{username}/outbox?page=true", headers={"Accept": self.AP})
        data = r.get_json()
        assert data["type"] == "OrderedCollectionPage"
        assert len(data["orderedItems"]) == 1
        item = data["orderedItems"][0]
        assert item["type"] == "Create"
        assert item["object"]["type"] == "Note"

    def test_outbox_unknown_user_404(self, client):
        r = client.get("/users/nobody/outbox", headers={"Accept": self.AP})
        assert r.status_code == 404

    def test_note_has_tag_array(self, client, user, app):
        import app as app_module
        username, uid = user
        self._seed_activity(app_module.DB_PATH, uid)
        r = client.get(f"/users/{username}/outbox?page=true", headers={"Accept": self.AP})
        note = r.get_json()["orderedItems"][0]["object"]
        assert "tag" in note
        assert isinstance(note["tag"], list)
        assert len(note["tag"]) > 0

    def test_note_tags_are_hashtag_type(self, client, user, app):
        import app as app_module
        username, uid = user
        self._seed_activity(app_module.DB_PATH, uid)
        r = client.get(f"/users/{username}/outbox?page=true", headers={"Accept": self.AP})
        note = r.get_json()["orderedItems"][0]["object"]
        assert all(t["type"] == "Hashtag" for t in note["tag"])

    def test_note_tags_have_name_and_href(self, client, user, app):
        import app as app_module
        username, uid = user
        self._seed_activity(app_module.DB_PATH, uid)
        r = client.get(f"/users/{username}/outbox?page=true", headers={"Accept": self.AP})
        note = r.get_json()["orderedItems"][0]["object"]
        for tag in note["tag"]:
            assert tag["name"].startswith("#")
            assert tag["href"].startswith("https://")

    def test_note_sport_tag_included(self, client, user, app):
        """A Ride activity must include #cycling in tags."""
        import app as app_module
        username, uid = user
        self._seed_activity(app_module.DB_PATH, uid)  # sport_type="Ride"
        r = client.get(f"/users/{username}/outbox?page=true", headers={"Accept": self.AP})
        note = r.get_json()["orderedItems"][0]["object"]
        names = [t["name"] for t in note["tag"]]
        assert "#cycling" in names

    def test_note_base_tags_always_present(self, client, user, app):
        """#strava and #bikeodon appear on every activity regardless of sport."""
        import app as app_module
        username, uid = user
        self._seed_activity(app_module.DB_PATH, uid)
        r = client.get(f"/users/{username}/outbox?page=true", headers={"Accept": self.AP})
        note = r.get_json()["orderedItems"][0]["object"]
        names = [t["name"] for t in note["tag"]]
        assert "#strava" in names
        assert "#bikeodon" in names

    def test_note_content_has_linked_hashtags(self, client, user, app):
        """Hashtags in content must be <a> links, not plain text."""
        import app as app_module
        username, uid = user
        self._seed_activity(app_module.DB_PATH, uid)
        r = client.get(f"/users/{username}/outbox?page=true", headers={"Accept": self.AP})
        content = r.get_json()["orderedItems"][0]["object"]["content"]
        assert '<a href=' in content
        assert 'rel="tag"' in content


# ---------------------------------------------------------------------------
# NodeInfo
# ---------------------------------------------------------------------------

class TestNodeInfo:

    def test_discovery_returns_200(self, client):
        r = client.get("/.well-known/nodeinfo")
        assert r.status_code == 200

    def test_discovery_has_nodeinfo_link(self, client):
        data = client.get("/.well-known/nodeinfo").get_json()
        rels = [l["rel"] for l in data.get("links", [])]
        assert "http://nodeinfo.diaspora.software/ns/schema/2.0" in rels

    def test_discovery_href_points_to_nodeinfo_endpoint(self, client):
        data = client.get("/.well-known/nodeinfo").get_json()
        href = next(
            l["href"] for l in data["links"]
            if l["rel"] == "http://nodeinfo.diaspora.software/ns/schema/2.0"
        )
        assert "/nodeinfo/2.0" in href

    def test_nodeinfo_returns_200(self, client):
        r = client.get("/nodeinfo/2.0")
        assert r.status_code == 200

    def test_nodeinfo_version(self, client):
        assert client.get("/nodeinfo/2.0").get_json()["version"] == "2.0"

    def test_nodeinfo_software_name(self, client):
        assert client.get("/nodeinfo/2.0").get_json()["software"]["name"] == "bikeodon"

    def test_nodeinfo_protocols_includes_activitypub(self, client):
        assert "activitypub" in client.get("/nodeinfo/2.0").get_json()["protocols"]

    def test_nodeinfo_open_registrations_false(self, client):
        assert client.get("/nodeinfo/2.0").get_json()["openRegistrations"] is False

    def test_nodeinfo_usage_has_required_fields(self, client, user):
        data = client.get("/nodeinfo/2.0").get_json()
        usage = data["usage"]
        assert "localPosts" in usage
        assert "total" in usage["users"]
        assert "activeHalfyear" in usage["users"]
        assert "activeMonth" in usage["users"]

    def test_nodeinfo_user_count_reflects_db(self, client, user):
        data = client.get("/nodeinfo/2.0").get_json()
        assert data["usage"]["users"]["total"] == 1

    def test_nodeinfo_local_posts_reflects_db(self, client, user, app):
        import app as app_module
        from database import upsert_activity
        _, uid = user
        upsert_activity(app_module.DB_PATH, {
            "id": 2001, "name": "Test Ride", "sport_type": "Ride",
            "start_date": "2026-06-01T08:00:00Z",
        }, uid)
        data = client.get("/nodeinfo/2.0").get_json()
        assert data["usage"]["localPosts"] == 1


# ---------------------------------------------------------------------------
# Profile update propagation
# ---------------------------------------------------------------------------

class TestProfileUpdate:
    """
    send_profile_update must fan out an Update{Person} activity to all
    followers when called after a profile change.
    """

    REMOTE_ACTOR = "https://mastodon.social/users/alice"
    REMOTE_INBOX = "https://mastodon.social/users/alice/inbox"

    def _add_follower(self, db_path, username):
        from database import add_follower
        add_follower(db_path, username, self.REMOTE_ACTOR, self.REMOTE_INBOX,
                     display_name="Alice", avatar_url=None)

    @pytest.fixture(autouse=True)
    def bypass_sig_verification(self):
        with patch("activitypub._verify_http_signature", return_value=(True, "ok")):
            yield

    def test_no_followers_sends_nothing(self, app, user):
        import app as app_module
        from activitypub import send_profile_update
        username, uid = user
        user_row = __import__("database").get_user_by_id(app_module.DB_PATH, uid)
        with patch("activitypub._deliver_activity") as mock_deliver:
            send_profile_update(username, user_row, app_module.DB_PATH)
        mock_deliver.assert_not_called()

    def test_fans_out_to_each_follower_inbox(self, app, user):
        import app as app_module
        from activitypub import send_profile_update
        username, uid = user
        self._add_follower(app_module.DB_PATH, username)
        user_row = __import__("database").get_user_by_id(app_module.DB_PATH, uid)
        with app.app_context(), patch("activitypub._deliver_activity") as mock_deliver:
            send_profile_update(username, user_row, app_module.DB_PATH)
        assert mock_deliver.call_count == 1
        inbox_url, activity, key_id, db = mock_deliver.call_args[0]
        assert inbox_url == self.REMOTE_INBOX

    def test_update_activity_type(self, app, user):
        import app as app_module
        from activitypub import send_profile_update
        username, uid = user
        self._add_follower(app_module.DB_PATH, username)
        user_row = __import__("database").get_user_by_id(app_module.DB_PATH, uid)
        with app.app_context(), patch("activitypub._deliver_activity") as mock_deliver:
            send_profile_update(username, user_row, app_module.DB_PATH)
        _, activity, _, _ = mock_deliver.call_args[0]
        assert activity["type"] == "Update"

    def test_update_object_is_person(self, app, user):
        import app as app_module
        from activitypub import send_profile_update
        username, uid = user
        self._add_follower(app_module.DB_PATH, username)
        user_row = __import__("database").get_user_by_id(app_module.DB_PATH, uid)
        with app.app_context(), patch("activitypub._deliver_activity") as mock_deliver:
            send_profile_update(username, user_row, app_module.DB_PATH)
        _, activity, _, _ = mock_deliver.call_args[0]
        assert activity["object"]["type"] == "Person"
        assert activity["object"]["id"] == f"https://bikeodon.org/users/{username}"

    def test_save_profile_triggers_update(self, client, user, app):  # noqa: E301
        """Saving a profile via the HTTP route must fan out an Update."""
        import app as app_module
        from werkzeug.security import generate_password_hash
        username, uid = user
        self._add_follower(app_module.DB_PATH, username)

        with client.session_transaction() as sess:
            sess["_user_id"] = str(uid)

        with patch("activitypub._deliver_activity") as mock_deliver:
            r = client.post("/me/profile", data={
                "display_name": "Tim Schneider",
                "summary": "Cycling enthusiast",
            })

        assert r.status_code in (200, 302)
        assert mock_deliver.called
        _, activity, _, _ = mock_deliver.call_args[0]
        assert activity["type"] == "Update"


# ---------------------------------------------------------------------------
# Unfollow
# ---------------------------------------------------------------------------

class TestUnfollow:

    REMOTE_ACTOR = "https://mastodon.social/users/alice"
    REMOTE_INBOX = "https://mastodon.social/users/alice/inbox"

    def _add_following(self, db_path, username):
        from database import add_following
        add_following(db_path, username, self.REMOTE_ACTOR, self.REMOTE_INBOX,
                      display_name="Alice", avatar_url=None)

    @pytest.fixture(autouse=True)
    def bypass_sig_verification(self):
        with patch("activitypub._verify_http_signature", return_value=(True, "ok")):
            yield

    def test_unfollow_removes_from_db(self, app, user):
        import app as app_module
        from activitypub import send_unfollow
        from database import get_following
        username, uid = user
        self._add_following(app_module.DB_PATH, username)
        user_row = __import__("database").get_user_by_id(app_module.DB_PATH, uid)
        with app.app_context(), patch("activitypub._deliver_activity"):
            send_unfollow(username, user_row, self.REMOTE_ACTOR, app_module.DB_PATH)
        rows = get_following(app_module.DB_PATH, username)
        assert not any(r["actor_url"] == self.REMOTE_ACTOR for r in rows)

    def test_unfollow_sends_undo_follow(self, app, user):
        import app as app_module
        from activitypub import send_unfollow
        username, uid = user
        self._add_following(app_module.DB_PATH, username)
        user_row = __import__("database").get_user_by_id(app_module.DB_PATH, uid)
        with app.app_context(), patch("activitypub._deliver_activity") as mock_deliver:
            send_unfollow(username, user_row, self.REMOTE_ACTOR, app_module.DB_PATH)
        assert mock_deliver.called
        inbox_url, activity, _, _ = mock_deliver.call_args[0]
        assert inbox_url == self.REMOTE_INBOX
        assert activity["type"] == "Undo"
        assert activity["object"]["type"] == "Follow"
        assert activity["object"]["object"] == self.REMOTE_ACTOR

    def test_unfollow_noop_if_not_following(self, app, user):
        import app as app_module
        from activitypub import send_unfollow
        username, uid = user
        user_row = __import__("database").get_user_by_id(app_module.DB_PATH, uid)
        with app.app_context(), patch("activitypub._deliver_activity") as mock_deliver:
            send_unfollow(username, user_row, self.REMOTE_ACTOR, app_module.DB_PATH)
        mock_deliver.assert_not_called()

    def test_unfollow_route_removes_and_redirects(self, client, user, app):
        import app as app_module
        from database import get_following
        username, uid = user
        self._add_following(app_module.DB_PATH, username)

        with client.session_transaction() as sess:
            sess["_user_id"] = str(uid)

        with patch("activitypub._deliver_activity"):
            r = client.post("/ap/unfollow",
                            data={"actor_url": self.REMOTE_ACTOR})

        assert r.status_code in (200, 302)
        rows = get_following(app_module.DB_PATH, username)
        assert not any(r["actor_url"] == self.REMOTE_ACTOR for r in rows)


# ---------------------------------------------------------------------------
# Home Feed
# ---------------------------------------------------------------------------

class TestHomeFeed:

    REMOTE_ACTOR = "https://mastodon.social/users/bob"
    REMOTE_INBOX = "https://mastodon.social/users/bob/inbox"
    NOTE_ID      = "https://mastodon.social/users/bob/statuses/1"

    def _add_following(self, db_path, username):
        from database import add_following, accept_following
        add_following(db_path, username, self.REMOTE_ACTOR, self.REMOTE_INBOX,
                      display_name="Bob", avatar_url=None)
        accept_following(db_path, username, self.REMOTE_ACTOR)

    def _create_note_activity(self, extra_note=None):
        note = {
            "id": self.NOTE_ID,
            "type": "Note",
            "attributedTo": self.REMOTE_ACTOR,
            "content": "<p>Hello from Bob!</p>",
            "published": "2026-06-11T12:00:00Z",
            "url": self.NOTE_ID,
        }
        if extra_note:
            note.update(extra_note)
        return {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{self.NOTE_ID}/activity",
            "type": "Create",
            "actor": self.REMOTE_ACTOR,
            "object": note,
        }

    @pytest.fixture(autouse=True)
    def bypass_sig(self):
        with patch("activitypub._verify_http_signature", return_value=(True, "ok")):
            yield

    def test_create_note_from_followed_stored(self, app, user):
        import app as app_module
        from activitypub import _handle_create_note
        from database import get_feed_items
        username, _ = user
        self._add_following(app_module.DB_PATH, username)

        activity = self._create_note_activity()
        _handle_create_note(username, activity, activity["object"], app_module.DB_PATH)

        items = get_feed_items(app_module.DB_PATH, username)
        assert len(items) == 1
        assert items[0]["object_id"] == self.NOTE_ID
        assert items[0]["content"] == "<p>Hello from Bob!</p>"

    def test_create_note_from_stranger_ignored(self, app, user):
        import app as app_module
        from activitypub import _handle_create_note
        from database import get_feed_items
        username, _ = user

        activity = self._create_note_activity()
        activity["actor"] = "https://evil.example/users/spam"
        activity["object"]["attributedTo"] = "https://evil.example/users/spam"
        _handle_create_note(username, activity, activity["object"], app_module.DB_PATH)

        items = get_feed_items(app_module.DB_PATH, username)
        assert len(items) == 0

    def test_create_note_stores_actor_name(self, app, user):
        import app as app_module
        from activitypub import _handle_create_note
        from database import get_feed_items
        username, _ = user
        self._add_following(app_module.DB_PATH, username)

        activity = self._create_note_activity()
        _handle_create_note(username, activity, activity["object"], app_module.DB_PATH)

        items = get_feed_items(app_module.DB_PATH, username)
        assert items[0]["actor_name"] == "Bob"

    def test_create_note_with_attachment_stored(self, app, user):
        import app as app_module
        from activitypub import _handle_create_note
        from database import get_feed_items
        username, _ = user
        self._add_following(app_module.DB_PATH, username)

        note_extra = {"attachment": [{"type": "Document", "mediaType": "image/png",
                                       "url": "https://example.com/map.png"}]}
        activity = self._create_note_activity(extra_note=note_extra)
        _handle_create_note(username, activity, activity["object"], app_module.DB_PATH)

        items = get_feed_items(app_module.DB_PATH, username)
        atts = json.loads(items[0]["attachments_json"])
        assert len(atts) == 1
        assert atts[0]["url"] == "https://example.com/map.png"

    def test_duplicate_note_ignored(self, app, user):
        import app as app_module
        from activitypub import _handle_create_note
        from database import get_feed_items
        username, _ = user
        self._add_following(app_module.DB_PATH, username)

        activity = self._create_note_activity()
        _handle_create_note(username, activity, activity["object"], app_module.DB_PATH)
        _handle_create_note(username, activity, activity["object"], app_module.DB_PATH)

        items = get_feed_items(app_module.DB_PATH, username)
        assert len(items) == 1

    def test_inbox_create_note_endpoint(self, client, user, app):
        import app as app_module
        from database import get_feed_items
        username, uid = user
        self._add_following(app_module.DB_PATH, username)

        activity = self._create_note_activity()
        r = client.post(
            f"/users/{username}/inbox",
            data=json.dumps(activity),
            content_type="application/activity+json",
        )
        assert r.status_code == 202
        items = get_feed_items(app_module.DB_PATH, username)
        assert len(items) == 1

    def test_feed_route_returns_200(self, client, user):
        username, uid = user
        with client.session_transaction() as sess:
            sess["_user_id"] = str(uid)
        r = client.get("/feed")
        assert r.status_code == 200

    def test_feed_shows_items(self, client, user, app):
        import app as app_module
        from activitypub import _handle_create_note
        username, uid = user
        self._add_following(app_module.DB_PATH, username)

        activity = self._create_note_activity()
        _handle_create_note(username, activity, activity["object"], app_module.DB_PATH)

        with client.session_transaction() as sess:
            sess["_user_id"] = str(uid)
        r = client.get("/feed")
        assert b"Hello from Bob!" in r.data

    def test_xss_content_is_sanitized(self, app, user):
        import app as app_module
        from activitypub import _handle_create_note
        from database import get_feed_items
        username, _ = user
        self._add_following(app_module.DB_PATH, username)

        xss_note = {"content": '<p>Hello</p><script>alert(1)</script><img src=x onerror=alert(2)>'}
        activity = self._create_note_activity(extra_note=xss_note)
        _handle_create_note(username, activity, activity["object"], app_module.DB_PATH)

        items = get_feed_items(app_module.DB_PATH, username)
        assert "<script>" not in items[0]["content"]
        assert "onerror" not in items[0]["content"]
        assert "Hello" in items[0]["content"]
