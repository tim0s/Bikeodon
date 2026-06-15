import threading
from functools import wraps

from flask import abort, flash, redirect, render_template, url_for
from flask_login import current_user, login_required

from config import DB_PATH, _base_cfg, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET
from fit_encoder import generate_fit
from flask import request
from strava import delete_webhook, list_webhooks, register_webhook
import glob, os
from database import (
    clear_athlete_params, clear_cp_history, get_activity, get_admin_stats,
    get_all_users_for_admin, get_error_activities, get_setting, get_site_setting,
    set_site_setting, delete_site_setting, load_user_config, reset_metrics_computed,
    save_activity_file, set_admin, set_setting, upsert_activity,
)
from tasks import _backfill_lock, _render_and_track


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Admin access required.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


def register_routes(app):

    @app.route("/admin")
    @login_required
    @admin_required
    def admin():
        stats            = get_admin_stats(DB_PATH)
        errors           = get_error_activities(DB_PATH)
        backfill_running = _backfill_lock.locked()
        invite_code      = get_site_setting(DB_PATH, "invite_code")

        out_dir   = os.path.abspath(_base_cfg["map"].get("output_dir", "output"))
        files_dir = os.path.join(out_dir, "activity_files")
        users     = get_all_users_for_admin(DB_PATH)

        # Build activity_id → user_id map for PNG attribution
        from database import _conn as _db_conn
        conn = _db_conn(DB_PATH)
        act_rows = conn.execute("SELECT id, user_id FROM activities").fetchall()
        conn.close()
        act_to_user = {r["id"]: r["user_id"] for r in act_rows}

        # Sum PNG sizes per user
        png_bytes_by_user: dict[int, int] = {}
        for path in glob.glob(os.path.join(out_dir, "*.png")):
            fname = os.path.basename(path)
            try:
                act_id = int(fname.split("_")[0].split(".")[0])
            except ValueError:
                continue
            uid_ = act_to_user.get(act_id)
            if uid_ is not None:
                png_bytes_by_user[uid_] = png_bytes_by_user.get(uid_, 0) + os.path.getsize(path)

        for u in users:
            uid_ = u["id"]
            fit_dir   = os.path.join(files_dir, str(uid_))
            fit_bytes = sum(
                os.path.getsize(f)
                for f in glob.glob(os.path.join(fit_dir, "*"))
                if os.path.isfile(f)
            ) if os.path.isdir(fit_dir) else 0
            u["disk_bytes"] = fit_bytes + png_bytes_by_user.get(uid_, 0)

        try:
            webhook_subs = list_webhooks(STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET)
        except Exception:
            webhook_subs = None  # Strava unreachable or credentials missing

        default_callback = url_for("strava_webhook_verify", _external=True)

        return render_template("admin.html", stats=stats, errors=errors,
                               backfill_running=backfill_running,
                               invite_code=invite_code, users=users,
                               webhook_subs=webhook_subs,
                               default_callback=default_callback)

    @app.route("/admin/full-sync", methods=["POST"])
    @login_required
    @admin_required
    def admin_full_sync():
        uid = int(current_user.id)

        def _run():
            from strava import StravaClient
            access_token = get_setting(DB_PATH, uid, "strava", "access_token") or ""
            refresh_tok  = get_setting(DB_PATH, uid, "strava", "refresh_token") or ""
            expires_at   = float(get_setting(DB_PATH, uid, "strava", "token_expires_at") or 0)
            if not access_token:
                return

            def _on_refresh(a, r, e):
                set_setting(DB_PATH, uid, "strava", "access_token",     a)
                set_setting(DB_PATH, uid, "strava", "refresh_token",    r)
                set_setting(DB_PATH, uid, "strava", "token_expires_at", str(e))

            client = StravaClient(
                access_token=access_token, client_id=STRAVA_CLIENT_ID,
                client_secret=STRAVA_CLIENT_SECRET, refresh_tok=refresh_tok,
                expires_at=expires_at, on_refresh=_on_refresh,
            )
            print(f"[full-sync] Fetching and processing activities in pages of 20…")
            files_dir = _base_cfg["map"].get("output_dir", "output") + "/activity_files"

            new_ids = []
            page = 1
            while True:
                ids = client.get_activity_ids(n=20, page=page)
                if not ids:
                    break
                print(f"[full-sync] Page {page}: {len(ids)} activities…")
                for activity_id in ids:
                    if get_activity(DB_PATH, activity_id, user_id=uid):
                        continue
                    try:
                        data, streams = client.get_activity(activity_id)
                        try:
                            fit_bytes = generate_fit(data, streams)
                            data["source_file"], data["source_file_sha256"] = \
                                save_activity_file(files_dir, activity_id, uid, fit_bytes, f"{activity_id}.fit")
                            data["source_file_type"] = "generated"
                        except Exception as fe:
                            print(f"[full-sync] FIT generation failed for {activity_id}: {fe}")
                        upsert_activity(DB_PATH, data, user_id=uid)
                        new_ids.append(activity_id)
                        print(f"[full-sync] + {data['name']}")
                    except Exception as e:
                        print(f"[full-sync] Failed {activity_id}: {e}")
                page += 1

            if new_ids:
                out_dir = _base_cfg["map"].get("output_dir", "output")
                cfg = load_user_config(DB_PATH, uid, _base_cfg)
                for activity_id in new_ids:
                    _render_and_track(activity_id, uid, cfg, out_dir)
            print(f"[full-sync] Done — {len(new_ids)} new activities imported.")

        threading.Thread(target=_run, daemon=True).start()
        flash("Full sync started in the background — check logs for progress.", "success")
        return redirect(url_for("admin"))

    @app.route("/admin/recompute-metrics", methods=["POST"])
    @login_required
    @admin_required
    def admin_recompute_metrics():
        uid = int(current_user.id)
        reset_metrics_computed(DB_PATH, uid)
        clear_cp_history(DB_PATH, uid)
        clear_athlete_params(DB_PATH, uid)
        set_setting(DB_PATH, uid, "inference", "ftp", "")
        set_setting(DB_PATH, uid, "inference", "max_hr", "")
        flash("Metrics reset. Visit the You page to trigger recomputation.", "success")
        return redirect(url_for("admin"))

    @app.route("/admin/set-admin", methods=["POST"])
    @login_required
    @admin_required
    def admin_set_admin():
        username = request.form.get("username", "").strip()
        grant    = request.form.get("grant") == "1"
        if not username:
            abort(400)
        set_admin(DB_PATH, username, grant)
        action = "granted admin to" if grant else "revoked admin from"
        flash(f"Successfully {action} {username}.", "success")
        return redirect(url_for("admin"))

    @app.route("/admin/invite-code", methods=["POST"])
    @login_required
    @admin_required
    def admin_set_invite_code():
        code = request.form.get("invite_code", "").strip()
        if code:
            set_site_setting(DB_PATH, "invite_code", code)
            flash(f"Invite code set to '{code}'.", "success")
        else:
            delete_site_setting(DB_PATH, "invite_code")
            flash("Invite code cleared — registration is now open to anyone.", "success")
        return redirect(url_for("admin"))

    @app.route("/admin/webhook/subscribe", methods=["POST"])
    @login_required
    @admin_required
    def admin_webhook_subscribe():
        verify_token = os.environ.get("STRAVA_WEBHOOK_VERIFY_TOKEN", "").strip()
        callback_url = request.form.get("callback_url", "").strip()
        if not verify_token:
            flash("STRAVA_WEBHOOK_VERIFY_TOKEN is not set in environment.", "error")
            return redirect(url_for("admin"))
        if not callback_url:
            flash("Callback URL is required.", "error")
            return redirect(url_for("admin"))
        try:
            result = register_webhook(STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET,
                                      callback_url, verify_token)
            flash(f"Webhook subscribed. ID: {result.get('id')}", "success")
        except Exception as e:
            flash(f"Subscription failed: {e}", "error")
        return redirect(url_for("admin"))

    @app.route("/admin/webhook/unsubscribe", methods=["POST"])
    @login_required
    @admin_required
    def admin_webhook_unsubscribe():
        sub_id = request.form.get("sub_id")
        if not sub_id:
            flash("No subscription ID provided.", "error")
            return redirect(url_for("admin"))
        try:
            delete_webhook(STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, int(sub_id))
            flash(f"Webhook subscription {sub_id} deleted.", "success")
        except Exception as e:
            flash(f"Unsubscribe failed: {e}", "error")
        return redirect(url_for("admin"))
