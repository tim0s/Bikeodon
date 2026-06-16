"""
Smoke tests for every HTTP route.

Strategy: one user, one seeded activity, two passes:
  1. Non-admin  — protected routes redirect (302), admin routes blocked.
  2. Grant admin — admin routes now render/redirect without crashing.

A 500 anywhere is a failure. Redirects and 404s are acceptable.
"""

import io
import os
import pytest
import yaml


# ---------------------------------------------------------------------------
# App / client fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("smoke")
    cfg = {
        "database": {"path": str(tmp / "smoke.db")},
        "daemon":   {"interval_minutes": 15},
        "map":      {"output_dir": str(tmp / "output"), "tiles": {}},
    }
    cfg_path = str(tmp / "config.yaml")
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)

    os.environ["BIKEODON_CONFIG"] = cfg_path
    os.environ["FLASK_SECRET_KEY"] = "smoke-secret-key-long-enough"

    import importlib
    import config as config_mod
    importlib.reload(config_mod)   # must reload before app so DB_PATH picks up the new env var
    import app as app_mod
    importlib.reload(app_mod)

    app_mod.app.config["TESTING"] = True
    app_mod.app.config["SERVER_NAME"] = "bikeodon.test"
    app_mod.app.config["PREFERRED_URL_SCHEME"] = "https"
    app_mod.app.config["_TEST_DB_PATH"] = str(tmp / "smoke.db")

    yield app_mod.app

    os.environ.pop("BIKEODON_CONFIG", None)


@pytest.fixture(scope="module")
def client(app):
    """Shared client — logged in as 'smokeuser' throughout the module."""
    c = app.test_client()
    c.post("/register", data={"username": "smokeuser", "password": "Password1!"})
    c.post("/login",    data={"username": "smokeuser", "password": "Password1!"})
    return c


@pytest.fixture(scope="module")
def activity_id(app):
    """Seed one activity and return its id."""
    from database import upsert_activity
    db_path = app.config["_TEST_DB_PATH"]
    act = {
        "id": 99999901,
        "name": "Smoke Test Ride",
        "sport_type": "Ride",
        "start_date": "2024-06-01T08:00:00Z",
        "distance": 30000,
        "moving_time": 3600,
        "elapsed_time": 3700,
        "total_elevation_gain": 300,
        "average_speed": 8.3,
        "max_speed": 15.0,
        "average_heartrate": 150,
        "max_heartrate": 175,
        "average_watts": 200,
        "max_watts": 500,
    }
    with app.app_context():
        upsert_activity(db_path, act, user_id=1, source="strava")
    return act["id"]


def _ok(r):
    assert r.status_code != 500, f"Route returned 500. Body: {r.data[:400]}"


# ---------------------------------------------------------------------------
# 1. Unauthenticated — every protected route must redirect, not crash
# ---------------------------------------------------------------------------

class TestUnauthenticated:

    @pytest.fixture()
    def anon(self, app):
        return app.test_client()

    def test_login_page(self, anon):
        r = anon.get("/login")
        _ok(r)
        assert r.status_code == 200

    def test_register_page(self, anon):
        r = anon.get("/register")
        _ok(r)
        assert r.status_code == 200

    def test_index_public(self, anon):
        r = anon.get("/")
        _ok(r)

    def test_me_redirects(self, anon):
        _ok(anon.get("/me"))

    def test_feed_redirects(self, anon):
        _ok(anon.get("/feed"))

    def test_upload_redirects(self, anon):
        _ok(anon.get("/upload"))

    def test_settings_redirects(self, anon):
        _ok(anon.get("/settings"))

    def test_admin_redirects(self, anon):
        _ok(anon.get("/admin"))

    def test_activity_redirects(self, anon):
        _ok(anon.get("/activity/999"))


# ---------------------------------------------------------------------------
# 2. Authenticated non-admin — GET routes render, admin routes blocked
# ---------------------------------------------------------------------------

class TestNonAdminGets:

    def test_index(self, client):
        _ok(client.get("/"))

    def test_me(self, client):
        r = client.get("/me")
        _ok(r)
        assert r.status_code == 200

    def test_me_profile_tab(self, client):
        _ok(client.get("/me?tab=profile"))

    def test_me_power_tab(self, client):
        _ok(client.get("/me?tab=power"))

    def test_me_hr_tab(self, client):
        _ok(client.get("/me?tab=hr"))

    def test_feed(self, client):
        _ok(client.get("/feed"))

    def test_upload_page(self, client):
        _ok(client.get("/upload"))

    def test_settings(self, client):
        _ok(client.get("/settings"))

    def test_settings_charts(self, client):
        _ok(client.get("/settings?section=charts"))

    def test_settings_training(self, client):
        _ok(client.get("/settings?section=training"))

    def test_settings_mastodon(self, client):
        _ok(client.get("/settings?section=mastodon"))

    def test_settings_zones(self, client):
        _ok(client.get("/settings/zones"))

    def test_activity_not_found(self, client):
        r = client.get("/activity/999999999")
        _ok(r)
        assert r.status_code in (302, 404)

    def test_activity_exists(self, client, activity_id):
        r = client.get(f"/activity/{activity_id}")
        _ok(r)
        assert r.status_code == 200

    # Admin routes must be blocked for non-admin
    def test_admin_blocked(self, client):
        r = client.get("/admin")
        _ok(r)
        assert r.status_code == 302

    def test_admin_full_sync_blocked(self, client):
        r = client.post("/admin/full-sync")
        _ok(r)
        assert r.status_code == 302

    def test_admin_recompute_blocked(self, client):
        r = client.post("/admin/recompute-metrics")
        _ok(r)
        assert r.status_code == 302

    def test_admin_set_admin_blocked(self, client):
        r = client.post("/admin/set-admin", data={"username": "smokeuser", "is_admin": "1"})
        _ok(r)
        assert r.status_code == 302

    def test_admin_invite_code_blocked(self, client):
        r = client.post("/admin/invite-code", data={"code": "abc"})
        _ok(r)
        assert r.status_code == 302

    def test_admin_webhook_subscribe_blocked(self, client):
        r = client.post("/admin/webhook/subscribe", data={"callback_url": ""})
        _ok(r)
        assert r.status_code == 302

    def test_admin_webhook_unsubscribe_blocked(self, client):
        r = client.post("/admin/webhook/unsubscribe", data={"subscription_id": "1"})
        _ok(r)
        assert r.status_code == 302


# ---------------------------------------------------------------------------
# 3. Authenticated non-admin — POST routes
# ---------------------------------------------------------------------------

class TestNonAdminPosts:

    def test_physio_birthday(self, client):
        r = client.post("/profile/physio", data={"param": "birthday", "value": "1990-01-01"})
        _ok(r)

    def test_physio_weight(self, client):
        r = client.post("/profile/physio", data={"param": "weight_kg", "value": "70.5"})
        _ok(r)

    def test_physio_max_hr(self, client):
        r = client.post("/profile/physio", data={"param": "max_hr", "value": "185"})
        _ok(r)

    def test_physio_rest_hr(self, client):
        r = client.post("/profile/physio", data={"param": "rest_hr", "value": "52"})
        _ok(r)

    def test_physio_height(self, client):
        r = client.post("/profile/physio", data={"param": "height_cm", "value": "178"})
        _ok(r)

    def test_physio_invalid_param_400(self, client):
        r = client.post("/profile/physio", data={"param": "evil", "value": "1"})
        assert r.status_code == 400

    def test_physio_non_numeric_400(self, client):
        r = client.post("/profile/physio", data={"param": "weight_kg", "value": "abc"})
        assert r.status_code == 400

    def test_me_profile_post(self, client):
        r = client.post("/me/profile", data={"display_name": "Smoke", "bio": "hi"})
        _ok(r)

    def test_settings_mastodon(self, client):
        r = client.post("/settings/mastodon", data={
            "instance": "", "handle": "", "visibility": "public", "post_template": "{name}",
        })
        _ok(r)

    def test_settings_map(self, client):
        r = client.post("/settings/map", data={})
        _ok(r)

    def test_settings_charts(self, client):
        r = client.post("/settings/charts", data={"ftp": "250", "max_hr": "185"})
        _ok(r)

    def test_settings_training(self, client):
        r = client.post("/settings/training", data={
            "body_weight_kg": "70", "hr_rest": "50", "lthr": "",
        })
        _ok(r)

    def test_settings_stats(self, client):
        r = client.post("/settings/stats", data={"fields": ["distance", "moving_time"]})
        _ok(r)

    def test_settings_zones(self, client):
        r = client.post("/settings/zones", data={"zone_type": "hr"})
        _ok(r)

    def test_settings_zones_preset(self, client):
        r = client.post("/settings/zones/preset", data={"zone_type": "hr", "preset": "coggan"})
        _ok(r)

    def test_upload_gpx(self, client):
        gpx = (
            b'<?xml version="1.0"?>'
            b'<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
            b"<trk><name>Upload Test</name><type>Ride</type><trkseg>"
            b'<trkpt lat="48.0" lon="11.0"><ele>500</ele><time>2024-07-01T08:00:00Z</time></trkpt>'
            b'<trkpt lat="48.01" lon="11.01"><ele>510</ele><time>2024-07-01T09:00:00Z</time></trkpt>'
            b"</trkseg></trk></gpx>"
        )
        r = client.post("/upload", data={"file": (io.BytesIO(gpx), "test.gpx")},
                        content_type="multipart/form-data")
        _ok(r)

    def test_activity_rerender(self, client, activity_id):
        r = client.post(f"/activity/{activity_id}/rerender")
        _ok(r)

    def test_activity_schedule(self, client, activity_id):
        r = client.post(f"/activity/{activity_id}/schedule")
        _ok(r)

    def test_activity_delete(self, client, app, activity_id):
        # Seed a disposable activity to delete
        from database import upsert_activity
        db_path = app.config["_TEST_DB_PATH"]
        disposable = {
            "id": 99999902, "name": "Delete Me", "sport_type": "Ride",
            "start_date": "2024-06-02T08:00:00Z", "distance": 1000,
            "moving_time": 600, "elapsed_time": 600,
        }
        upsert_activity(db_path, disposable, user_id=1, source="strava")
        r = client.post("/activity/99999902/delete")
        _ok(r)

    def test_feed_react_no_params(self, client):
        r = client.post("/feed/react", data={})
        _ok(r)

    def test_ap_follow_no_actor(self, client):
        r = client.post("/ap/follow", data={})
        assert r.status_code in (302, 400)

    def test_sync_no_strava(self, client):
        r = client.post("/sync")
        _ok(r)


# ---------------------------------------------------------------------------
# 4. Admin user — grant admin, verify admin routes now work
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def admin_client(app):
    """Separate client logged in as 'adminuser', promoted to admin."""
    from database import set_admin
    db_path = app.config["_TEST_DB_PATH"]

    c = app.test_client()
    c.post("/register", data={"username": "adminuser", "password": "Password1!"})
    c.post("/login",    data={"username": "adminuser", "password": "Password1!"})

    # Grant admin — user_loader reads is_admin from DB on every request,
    # so this takes effect immediately without re-login.
    with app.app_context():
        set_admin(db_path, "adminuser", True)

    return c


class TestAdminRoutes:

    def test_admin_page_accessible(self, admin_client):
        r = admin_client.get("/admin")
        _ok(r)
        assert r.status_code == 200

    def test_admin_recompute_metrics(self, admin_client):
        r = admin_client.post("/admin/recompute-metrics")
        _ok(r)
        assert r.status_code == 302

    def test_admin_full_sync_no_strava(self, admin_client):
        # Fires a background thread that exits early (no Strava token) — no crash
        r = admin_client.post("/admin/full-sync")
        _ok(r)
        assert r.status_code == 302

    def test_admin_set_admin(self, admin_client):
        r = admin_client.post("/admin/set-admin",
                              data={"username": "smokeuser", "is_admin": "0"})
        _ok(r)
        assert r.status_code == 302

    def test_admin_invite_code(self, admin_client):
        r = admin_client.post("/admin/invite-code", data={"code": "testcode123"})
        _ok(r)
        assert r.status_code == 302

    def test_admin_webhook_subscribe_no_strava(self, admin_client):
        r = admin_client.post("/admin/webhook/subscribe", data={"callback_url": ""})
        _ok(r)
        assert r.status_code == 302

    def test_admin_webhook_unsubscribe_no_strava(self, admin_client):
        r = admin_client.post("/admin/webhook/unsubscribe", data={"subscription_id": "99"})
        _ok(r)
        assert r.status_code == 302

    def test_non_admin_still_blocked_after_grant(self, client):
        """Granting admin to adminuser must not affect smokeuser."""
        r = client.get("/admin")
        _ok(r)
        assert r.status_code == 302
