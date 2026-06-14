import os
import threading
from datetime import datetime, timezone

from flask import flash, redirect, request, url_for
from flask_login import current_user, login_required

from config import DB_PATH, _base_cfg, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, SYNC_COOLDOWN_SECS
from database import (
    _conn, get_activity, get_setting, get_user_by_athlete_id,
    load_user_config, save_activity_file, set_setting, upsert_activity,
)
from fit_encoder import generate_fit
from strava import StravaClient, exchange_code, strava_auth_url
from tasks import _render_and_track, request_backfill


def _make_strava_client(uid: int):
    """Return a StravaClient for uid, or None if no token is stored."""
    access_token = get_setting(DB_PATH, uid, "strava", "access_token") or ""
    if not access_token:
        return None
    refresh_tok = get_setting(DB_PATH, uid, "strava", "refresh_token") or ""
    expires_at  = float(get_setting(DB_PATH, uid, "strava", "token_expires_at") or 0)

    def _on_refresh(new_access, new_refresh, new_expires):
        set_setting(DB_PATH, uid, "strava", "access_token",     new_access)
        set_setting(DB_PATH, uid, "strava", "refresh_token",    new_refresh)
        set_setting(DB_PATH, uid, "strava", "token_expires_at", str(new_expires))

    return StravaClient(
        access_token=access_token, client_id=STRAVA_CLIENT_ID,
        client_secret=STRAVA_CLIENT_SECRET, refresh_tok=refresh_tok,
        expires_at=expires_at, on_refresh=_on_refresh,
    )


def _generate_and_save_fit(activity: dict, streams: dict, activity_id: int, uid: int) -> None:
    """Generate a FIT file from Strava streams and populate source_file fields on activity."""
    files_dir = os.path.join(_base_cfg["map"].get("output_dir", "output"), "activity_files")
    try:
        fit_bytes = generate_fit(activity, streams)
        path, sha256 = save_activity_file(files_dir, activity_id, uid, fit_bytes, f"{activity_id}.fit")
        activity["source_file"]      = path
        activity["source_file_sha256"] = sha256
        activity["source_file_type"] = "generated"
    except Exception as e:
        print(f"[strava] Could not generate FIT for {activity_id}: {e}")


def _sync_cooldown_remaining(uid: int) -> int:
    """Return seconds until the user may sync again, or 0 if available."""
    last = get_setting(DB_PATH, uid, "strava", "last_manual_sync_at")
    if not last:
        return 0
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return max(0, int(SYNC_COOLDOWN_SECS - elapsed))
    except (ValueError, AttributeError):
        return 0


def _handle_webhook_event(event: dict):
    obj_type = event.get("object_type")
    aspect   = event.get("aspect_type")
    obj_id   = event.get("object_id")
    owner_id = event.get("owner_id")

    user = get_user_by_athlete_id(DB_PATH, owner_id)
    if not user:
        return

    uid = user["id"]

    if obj_type == "athlete" and aspect == "deauthorize":
        for key in ("access_token", "refresh_token", "token_expires_at", "athlete_id"):
            set_setting(DB_PATH, uid, "strava", key, "")
        return

    if obj_type != "activity":
        return

    if aspect == "delete":
        conn = _conn(DB_PATH)
        conn.execute("DELETE FROM activities WHERE id=? AND user_id=?", (obj_id, uid))
        conn.commit()
        conn.close()
        return

    if aspect in ("create", "update"):
        client = _make_strava_client(uid)
        if not client:
            return
        try:
            data, streams = client.get_activity(obj_id)
        except Exception:
            return

        _generate_and_save_fit(data, streams, obj_id, uid)
        upsert_activity(DB_PATH, data, user_id=uid)
        cfg     = load_user_config(DB_PATH, uid, _base_cfg)
        out_dir = _base_cfg["map"].get("output_dir", "output")
        _render_and_track(obj_id, uid, cfg, out_dir)
        request_backfill(uid)


def register_routes(app):

    @app.route("/sync", methods=["POST"])
    @login_required
    def manual_sync():
        uid = int(current_user.id)
        remaining = _sync_cooldown_remaining(uid)
        if remaining > 0:
            mins = (remaining + 59) // 60
            flash(f"Sync rate-limited — try again in {mins} min.", "error")
            return redirect(url_for("index"))

        set_setting(DB_PATH, uid, "strava", "last_manual_sync_at",
                    datetime.now(timezone.utc).isoformat())

        def _run():
            client = _make_strava_client(uid)
            if not client:
                return
            try:
                ids = client.get_activity_ids(n=10)
            except Exception as e:
                print(f"[manual-sync] Strava API error: {e}")
                return

            cfg     = load_user_config(DB_PATH, uid, _base_cfg)
            out_dir = _base_cfg["map"].get("output_dir", "output")

            new_ids = []
            for activity_id in ids:
                if get_activity(DB_PATH, activity_id, user_id=uid):
                    continue
                try:
                    data, streams = client.get_activity(activity_id)
                    _generate_and_save_fit(data, streams, activity_id, uid)
                    upsert_activity(DB_PATH, data, user_id=uid)
                    new_ids.append(activity_id)
                    print(f"[manual-sync] + {data['name']}")
                except Exception as e:
                    print(f"[manual-sync] Failed {activity_id}: {e}")

            for activity_id in new_ids:
                _render_and_track(activity_id, uid, cfg, out_dir)
            print(f"[manual-sync] Done — {len(new_ids)} new activities for user {uid}")
            if new_ids:
                request_backfill(uid)

        threading.Thread(target=_run, daemon=True).start()
        flash("Syncing from Strava — new activities will appear shortly.", "success")
        return redirect(url_for("index"))

    @app.route("/strava/connect")
    @login_required
    def strava_connect():
        if not STRAVA_CLIENT_ID or not STRAVA_CLIENT_SECRET:
            flash("Add STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET to .env and restart the server.", "error")
            return redirect(url_for("settings"))
        redirect_uri = url_for("strava_callback", _external=True)
        return redirect(strava_auth_url(STRAVA_CLIENT_ID, redirect_uri))

    @app.route("/strava/callback")
    @login_required
    def strava_callback():
        error = request.args.get("error")
        if error:
            flash(f"Strava authorization denied: {error}", "error")
            return redirect(url_for("settings"))
        code = request.args.get("code", "")
        if not code:
            flash("No authorization code received from Strava.", "error")
            return redirect(url_for("settings"))
        try:
            data = exchange_code(STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, code)
        except Exception as e:
            flash(f"Strava token exchange failed: {e}", "error")
            return redirect(url_for("settings"))

        uid = int(current_user.id)
        set_setting(DB_PATH, uid, "strava", "access_token",     data["access_token"])
        set_setting(DB_PATH, uid, "strava", "refresh_token",    data["refresh_token"])
        set_setting(DB_PATH, uid, "strava", "token_expires_at", str(data["expires_at"]))
        athlete = data.get("athlete", {})
        if athlete.get("id"):
            set_setting(DB_PATH, uid, "strava", "athlete_id", str(athlete["id"]))
        flash("Strava connected successfully!", "success")
        return redirect(url_for("settings"))

    @app.route("/strava/webhook", methods=["GET"])
    def strava_webhook_verify():
        verify_token = os.environ.get("STRAVA_WEBHOOK_VERIFY_TOKEN", "")
        if request.args.get("hub.verify_token") != verify_token:
            return "Forbidden", 403
        return {"hub.challenge": request.args.get("hub.challenge")}, 200

    @app.route("/strava/webhook", methods=["POST"])
    def strava_webhook_event():
        event = request.json or {}
        threading.Thread(target=_handle_webhook_event, args=(event,), daemon=True).start()
        return "", 200

    @app.route("/strava/disconnect")
    @login_required
    def strava_disconnect():
        uid = int(current_user.id)
        for key in ("access_token", "refresh_token", "token_expires_at", "athlete_id"):
            set_setting(DB_PATH, uid, "strava", key, "")
        flash("Strava disconnected.", "success")
        return redirect(url_for("settings"))
