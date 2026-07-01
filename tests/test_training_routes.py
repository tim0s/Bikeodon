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
    # training_routes (like the other *_routes modules) imports DB_PATH at load
    # time; reload it explicitly so it doesn't keep pointing at whichever test
    # module's tmp db happened to import it first in this pytest process.
    import training_routes as training_routes_mod
    importlib.reload(training_routes_mod)
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


@pytest.fixture(scope="module")
def client_5zone(app, db_path):
    """Logged-in user with FTP set and the 5-zone power preset applied."""
    from database import create_user, set_athlete_param, apply_zone_preset

    uid = create_user(db_path, "fivezoneuser", generate_password_hash("Password1!"))
    set_athlete_param(db_path, uid, "ftp", 250, source="manual")
    apply_zone_preset(db_path, uid, "power", "5zone")
    c = app.test_client()
    c.post("/login", data={"username": "fivezoneuser", "password": "Password1!"})
    return c


@pytest.fixture(scope="module")
def client_other(app, db_path):
    """A second, independent user with FTP set — used for ownership-scoping tests."""
    from database import create_user, set_athlete_param

    uid = create_user(db_path, "otheruser", generate_password_hash("Password1!"))
    set_athlete_param(db_path, uid, "ftp", 200, source="manual")
    c = app.test_client()
    c.post("/login", data={"username": "otheruser", "password": "Password1!"})
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

    def test_zone_labels_reflect_users_configured_preset(self, client, client_5zone):
        # A max-effort sprint step lands in a different named zone under the
        # 5-zone model (no dedicated top-end band) than under the default 7-zone one.
        seven = _generate(client, goal="sprints", duration_min=90, hardness=1.0).get_json()
        five = _generate(client_5zone, goal="sprints", duration_min=90, hardness=1.0).get_json()
        assert seven["ok"] and five["ok"]
        work_seven = next(s for s in seven["steps"] if s["label"].startswith("Sprints"))
        work_five = next(s for s in five["steps"] if s["label"].startswith("Sprints"))
        assert work_seven["zone_name"] != work_five["zone_name"]
        assert work_five["zone_name"] == "Z5 VO2 Max"


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


class TestCustomFinalizeEndpoint:

    def test_unauthenticated_redirects(self, app):
        r = app.test_client().post("/training/custom/finalize")
        assert r.status_code in (302, 401)

    def test_valid_steps_return_finalized_workout(self, client):
        r = client.post(
            "/training/custom/finalize",
            data=json.dumps({"steps": [{"label": "Push", "duration_s": 600, "pct_ftp": 100}],
                              "goal_label": "My Focus"}),
            content_type="application/json",
        )
        data = r.get_json()
        assert r.status_code == 200
        assert data["ok"] is True
        assert data["goal"] == "custom"
        assert data["goal_label"] == "My Focus"
        assert data["steps"][0]["watts"] == 250

    def test_no_ftp_user_gets_no_ftp_error(self, client_no_ftp):
        r = client_no_ftp.post(
            "/training/custom/finalize",
            data=json.dumps({"steps": [{"label": "X", "duration_s": 60, "pct_ftp": 100}]}),
            content_type="application/json",
        )
        assert r.get_json()["error"] == "no_ftp"

    def test_empty_steps_returns_bad_input(self, client):
        r = client.post("/training/custom/finalize", data=json.dumps({"steps": []}),
                         content_type="application/json")
        data = r.get_json()
        assert data["ok"] is False
        assert data["error"] == "bad_input"


class TestSavedWorkoutsCrud:

    def test_save_unauthenticated_redirects(self, app):
        r = app.test_client().post("/training/save")
        assert r.status_code in (302, 401)

    def test_saved_list_unauthenticated_redirects(self, app):
        r = app.test_client().get("/training/saved")
        assert r.status_code in (302, 401)

    def test_save_requires_a_name(self, client):
        workout = _generate(client).get_json()
        r = client.post("/training/save", data=json.dumps({"name": "", "workout": workout}),
                         content_type="application/json")
        data = r.get_json()
        assert data["ok"] is False
        assert data["error"] == "bad_input"

    def test_save_requires_a_workout(self, client):
        r = client.post("/training/save", data=json.dumps({"name": "No workout"}),
                         content_type="application/json")
        data = r.get_json()
        assert data["ok"] is False
        assert data["error"] == "bad_input"

    def test_save_then_list_then_delete_round_trip(self, client):
        workout = _generate(client, goal="threshold", duration_min=60).get_json()
        assert workout["ok"] is True

        save_resp = client.post(
            "/training/save",
            data=json.dumps({"name": "Round Trip Workout", "workout": workout}),
            content_type="application/json",
        ).get_json()
        assert save_resp["ok"] is True
        workout_id = save_resp["id"]

        listed = client.get("/training/saved").get_json()
        assert listed["ok"] is True
        saved = next(w for w in listed["workouts"] if w["id"] == workout_id)
        assert saved["name"] == "Round Trip Workout"
        assert saved["goal_label"] == "Threshold"
        assert saved["duration_min"] == 60
        assert saved["planned_if"] == workout["planned_if"]
        assert saved["planned_tss"] == workout["planned_tss"]
        assert saved["steps"] == workout["steps"]

        delete_resp = client.post(f"/training/saved/{workout_id}/delete").get_json()
        assert delete_resp["ok"] is True

        listed_after = client.get("/training/saved").get_json()
        assert all(w["id"] != workout_id for w in listed_after["workouts"])

    def test_delete_nonexistent_returns_ok_false(self, client):
        r = client.post("/training/saved/999999999/delete")
        assert r.get_json()["ok"] is False

    def test_user_cannot_see_another_users_saved_workout(self, client, client_other):
        workout = _generate(client).get_json()
        save_resp = client.post(
            "/training/save",
            data=json.dumps({"name": "Private Workout", "workout": workout}),
            content_type="application/json",
        ).get_json()
        workout_id = save_resp["id"]

        other_list = client_other.get("/training/saved").get_json()
        assert all(w["id"] != workout_id for w in other_list["workouts"])

    def test_user_cannot_delete_another_users_saved_workout(self, client, client_other):
        workout = _generate(client).get_json()
        save_resp = client.post(
            "/training/save",
            data=json.dumps({"name": "Not Yours", "workout": workout}),
            content_type="application/json",
        ).get_json()
        workout_id = save_resp["id"]

        delete_resp = client_other.post(f"/training/saved/{workout_id}/delete").get_json()
        assert delete_resp["ok"] is False

        still_there = client.get("/training/saved").get_json()
        assert any(w["id"] == workout_id for w in still_there["workouts"])


def _samples(n=60, power=200, hr=140, speed=30.0):
    return [{"t": i, "power": power, "cadence": 85, "hr": hr, "speed": speed} for i in range(n)]


class TestSaveActivityEndpoint:

    def test_unauthenticated_redirects(self, app):
        r = app.test_client().post("/training/save_activity")
        assert r.status_code in (302, 401)

    def test_too_few_samples_rejected(self, client):
        r = client.post(
            "/training/save_activity",
            data=json.dumps({"name": "Short", "samples": _samples(n=3), "started_at": "2026-01-01T00:00:00.000Z"}),
            content_type="application/json",
        )
        data = r.get_json()
        assert data["ok"] is False
        assert data["error"] == "bad_input"

    def test_missing_started_at_rejected(self, client):
        r = client.post(
            "/training/save_activity",
            data=json.dumps({"name": "No date", "samples": _samples()}),
            content_type="application/json",
        )
        assert r.get_json()["ok"] is False

    def test_valid_session_creates_a_fetchable_activity(self, client):
        r = client.post(
            "/training/save_activity",
            data=json.dumps({
                "name": "Route Test Ride",
                "samples": _samples(n=60),
                "started_at": "2026-01-15T08:00:00.000Z",
            }),
            content_type="application/json",
        )
        data = r.get_json()
        assert r.status_code == 200
        assert data["ok"] is True
        activity_id = data["activity_id"]

        page = client.get(f"/activity/{activity_id}")
        assert page.status_code == 200
        assert b"Route Test Ride" in page.data

    def test_created_activity_has_source_training_and_no_gps(self, client, db_path):
        from database import get_activity

        r = client.post(
            "/training/save_activity",
            data=json.dumps({
                "name": "Indoor Ride",
                "samples": _samples(n=30),
                "started_at": "2026-01-15T09:00:00.000Z",
            }),
            content_type="application/json",
        )
        activity_id = r.get_json()["activity_id"]

        from database import _conn
        conn = _conn(db_path)
        row = conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        conn.close()
        assert row["source"] == "training"
        assert row["sport_type"] == "VirtualRide"
        assert row["source_file"] is not None
        assert row["average_watts"] == 200
