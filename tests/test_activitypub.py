"""
Tests for ActivityPub federation endpoints.

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
