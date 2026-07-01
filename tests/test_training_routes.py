"""
Endpoint-contract tests for /training/generate and /training/export.*.

Route smoke coverage ("doesn't 500") lives in test_routes_smoke.py; this file
asserts the actual JSON/file contract, since these are the training feature's
only Flask-level surface (the real logic is unit-tested directly in
test_workout_generator.py / test_fit_writer.py / test_zwo_writer.py).
"""
import json
import os

import fitparse
import io
import pytest
import xml.etree.ElementTree as ET
import yaml
from werkzeug.security import generate_password_hash


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("training_routes")
    cfg = {
        "database": {"path": str(tmp / "test.db")},
        "map":      {"output_dir": str(tmp / "output"), "tiles": {}},
    }
    cfg_path = str(tmp / "config.yaml")
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)
    os.environ["BIKEODON_CONFIG"] = cfg_path
    os.environ["FLASK_SECRET_KEY"] = "test-secret-key-long-enough"

    import importlib
    import config as config_mod
    importlib.reload(config_mod)
    import app as app_mod
    importlib.reload(app_mod)

    app_mod.app.config["TESTING"] = True
    app_mod.app.config["SERVER_NAME"] = "bikeodon.test"
    app_mod.app.config["_TEST_DB_PATH"] = str(tmp / "test.db")

    yield app_mod.app
    os.environ.pop("BIKEODON_CONFIG", None)


@pytest.fixture(scope="module")
def db_path(app):
    return app.config["_TEST_DB_PATH"]


@pytest.fixture(scope="module")
def client(app, db_path):
    """Logged-in user with an FTP set — the happy path for workout generation."""
    from database import create_user, set_athlete_param

    uid = create_user(db_path, "trainuser", generate_password_hash("Password1!"))
    set_athlete_param(db_path, uid, "ftp", 250, source="manual")
    c = app.test_client()
    c.post("/login", data={"username": "trainuser", "password": "Password1!"})
    return c


@pytest.fixture(scope="module")
def client_no_ftp(app, db_path):
    """Logged-in user with no FTP recorded — exercises the no_ftp error path."""
    from database import create_user

    create_user(db_path, "noftpuser", generate_password_hash("Password1!"))
    c = app.test_client()
    c.post("/login", data={"username": "noftpuser", "password": "Password1!"})
    return c


def _generate(client, **overrides):
    body = {"goal": "sweet_spot", "duration_min": 60, "hardness": 0.5}
    body.update(overrides)
    return client.post("/training/generate", data=json.dumps(body),
                        content_type="application/json")


class TestGenerateEndpoint:

    def test_unauthenticated_redirects(self, app):
        r = app.test_client().post("/training/generate")
        assert r.status_code in (302, 401)

    def test_feasible_request_returns_steps(self, client):
        r = _generate(client)
        data = r.get_json()
        assert r.status_code == 200
        assert data["ok"] is True
        assert len(data["steps"]) > 0
        assert data["ftp"] == 250

    def test_infeasible_request_returns_explanatory_error(self, client):
        r = _generate(client, goal="endurance", duration_min=30)
        data = r.get_json()
        assert data["ok"] is False
        assert data["error"] == "infeasible"

    def test_no_ftp_user_gets_no_ftp_error(self, client_no_ftp):
        r = _generate(client_no_ftp)
        data = r.get_json()
        assert data["ok"] is False
        assert data["error"] == "no_ftp"

    def test_bad_duration_input_does_not_crash(self, client):
        r = client.post(
            "/training/generate",
            data=json.dumps({"goal": "sweet_spot", "duration_min": "not-a-number"}),
            content_type="application/json",
        )
        assert r.status_code == 200
        assert r.get_json()["ok"] is False


class TestExportEndpoints:

    @pytest.fixture()
    def generated_workout(self, client):
        return _generate(client).get_json()

    def test_export_fit_unauthenticated_redirects(self, app):
        r = app.test_client().post("/training/export.fit")
        assert r.status_code in (302, 401)

    def test_export_fit_returns_valid_fit_file(self, client, generated_workout):
        r = client.post("/training/export.fit", data=json.dumps(generated_workout),
                         content_type="application/json")
        assert r.status_code == 200
        assert r.mimetype == "application/octet-stream"
        assert "attachment" in r.headers.get("Content-Disposition", "")
        assert r.data[8:12] == b".FIT"
        # fully round-trips through the FIT parser
        ff = fitparse.FitFile(io.BytesIO(r.data))
        assert list(ff.get_messages("workout_step"))

    def test_export_zwo_returns_valid_xml(self, client, generated_workout):
        r = client.post("/training/export.zwo", data=json.dumps(generated_workout),
                         content_type="application/json")
        assert r.status_code == 200
        assert "attachment" in r.headers.get("Content-Disposition", "")
        root = ET.fromstring(r.data)
        assert root.tag == "workout_file"

    def test_export_fit_filename_reflects_goal_and_duration(self, client, generated_workout):
        r = client.post("/training/export.fit", data=json.dumps(generated_workout),
                         content_type="application/json")
        disposition = r.headers.get("Content-Disposition", "")
        assert "sweet-spot" in disposition
        assert "60min" in disposition

    def test_export_fit_with_malformed_body_returns_400(self, client):
        r = client.post("/training/export.fit", data=json.dumps({"not": "a workout"}),
                         content_type="application/json")
        assert r.status_code == 400
        assert r.get_json()["ok"] is False

    def test_export_zwo_with_malformed_body_returns_400(self, client):
        r = client.post("/training/export.zwo", data=json.dumps({"not": "a workout"}),
                         content_type="application/json")
        assert r.status_code == 400
        assert r.get_json()["ok"] is False
