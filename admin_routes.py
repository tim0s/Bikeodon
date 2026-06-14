import threading
from functools import wraps

from flask import flash, redirect, render_template, url_for
from flask_login import current_user, login_required

from config import DB_PATH, _base_cfg, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET
from fit_encoder import generate_fit
from database import (
    get_activity, get_admin_stats, get_error_activities, get_recent_jobs,
    get_setting, load_user_config, reset_metrics_computed, save_activity_file,
    set_setting, upsert_activity,
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
        recent_jobs      = get_recent_jobs(DB_PATH)
        backfill_running = _backfill_lock.locked()
        return render_template("admin.html", stats=stats, errors=errors,
                               recent_jobs=recent_jobs, backfill_running=backfill_running)

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
        set_setting(DB_PATH, uid, "inference", "ftp", "")
        set_setting(DB_PATH, uid, "inference", "max_hr", "")
        flash("Metrics reset. Visit the You page to trigger recomputation.", "success")
        return redirect(url_for("admin"))
