"""
Tests for authentication and registration behaviour.
"""

import os
import pytest
import yaml


@pytest.fixture()
def app(tmp_path):
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
