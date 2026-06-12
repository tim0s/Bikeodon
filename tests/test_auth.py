"""
Tests for authentication and registration behaviour.
"""

import os
import pytest
import yaml


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("bikeodon_auth")
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


def _register(client, username, password="secret123"):
    return client.post("/register", data={
        "username": username, "password": password,
    }, follow_redirects=True)


def _login(client, username, password="secret123"):
    return client.post("/login", data={
        "username": username, "password": password,
    }, follow_redirects=False)


class TestOpenRedirect:

    def test_safe_local_next_is_honoured(self, client):
        _register(client, "redirect_user1")
        r = _login(client, "redirect_user1")
        # default redirect after login goes somewhere local
        assert r.status_code in (301, 302)
        location = r.headers["Location"]
        assert "evil.com" not in location

    def test_external_next_is_ignored(self, client):
        _register(client, "redirect_user2")
        r = client.post("/login?next=https://evil.com/steal",
                        data={"username": "redirect_user2", "password": "secret123"},
                        follow_redirects=False)
        assert r.status_code in (301, 302)
        assert "evil.com" not in r.headers["Location"]

    def test_protocol_relative_next_is_ignored(self, client):
        _register(client, "redirect_user3")
        r = client.post("/login?next=//evil.com/steal",
                        data={"username": "redirect_user3", "password": "secret123"},
                        follow_redirects=False)
        assert r.status_code in (301, 302)
        assert "evil.com" not in r.headers["Location"]


class TestUsernameValidation:

    def test_valid_username_accepted(self, client):
        r = _register(client, "alice_42")
        assert b"Username may only contain" not in r.data

    def test_slash_rejected(self, client):
        r = _register(client, "ali/ce")
        assert b"Username may only contain" in r.data

    def test_at_sign_rejected(self, client):
        r = _register(client, "ali@ce")
        assert b"Username may only contain" in r.data

    def test_hash_rejected(self, client):
        r = _register(client, "ali#ce")
        assert b"Username may only contain" in r.data

    def test_space_rejected(self, client):
        r = _register(client, "ali ce")
        assert b"Username may only contain" in r.data

    def test_too_long_rejected(self, client):
        r = _register(client, "a" * 31)
        assert b"Username may only contain" in r.data

    def test_exactly_30_chars_accepted(self, client):
        r = _register(client, "a" * 30)
        assert b"Username may only contain" not in r.data

    def test_dots_and_hyphens_accepted(self, client):
        r = _register(client, "alice.smith-jr")
        assert b"Username may only contain" not in r.data
