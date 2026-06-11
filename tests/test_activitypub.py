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
        inbox_url, accept_doc, _priv, _key_id = mock_deliver.call_args[0]
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
