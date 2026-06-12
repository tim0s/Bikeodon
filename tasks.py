"""
Background task functions: rendering, metrics, posting.

All functions use DB_PATH / _base_cfg from config.py.  They are called from
route handlers (which close over app.py's DB_PATH for test-patching safety)
and from the delivery worker thread.
"""

import json
import os
import threading
import time as _time

from config import DB_PATH, _base_cfg

from database import (
    _conn, get_activities_without_metrics, get_activity, get_all_peak_powers,
    get_setting, get_stream, job_finish, job_start,
    load_user_config, mark_rendered, mark_posted,
    set_activity_error, set_scheduled, set_setting,
    update_activity_metrics,
)
from database import get_points as _gp
from charts import generate_charts
from map_renderer import render_activity_map
from mastodon_client import MastodonClient
from training_load import (
    aggregate_power_curve, compute_hr_tss, compute_np, compute_peak_powers,
    compute_trimp, compute_tss, compute_zone_times,
)
from inference import infer_training_params

_backfill_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_and_track(activity_id: int, uid: int, cfg: dict, out_dir: str, row=None):
    """Render map + charts for an activity, storing any errors in the DB."""
    if row is None:
        row = get_activity(DB_PATH, activity_id, user_id=uid)
    if not row:
        return
    os.makedirs(out_dir, exist_ok=True)
    errors = []

    pts = _gp(row)
    if pts:
        try:
            img = render_activity_map(pts, dict(row), cfg)
            if img:
                img.save(os.path.join(out_dir, f"{activity_id}.png"))
            mark_rendered(DB_PATH, activity_id, uid, map=True)
        except Exception as e:
            errors.append(f"map: {e}")
            print(f"[render] map failed for {activity_id}: {e}")
    else:
        mark_rendered(DB_PATH, activity_id, uid, map=True)

    if not cfg["charts"]["power"]["ftp"]:
        v = get_setting(DB_PATH, uid, "inference", "ftp")
        if v:
            cfg["charts"]["power"]["ftp"] = float(v)
    if not cfg["charts"]["heart_rate"]["max_hr"]:
        v = get_setting(DB_PATH, uid, "inference", "max_hr")
        if v:
            cfg["charts"]["heart_rate"]["max_hr"] = float(v)

    stream = get_stream(row)
    try:
        generate_charts(activity_id, stream, cfg, out_dir, db_path=DB_PATH, user_id=uid)
        mark_rendered(DB_PATH, activity_id, uid, charts=True)
    except Exception as e:
        errors.append(f"charts: {e}")
        print(f"[render] charts failed for {activity_id}: {e}")

    set_activity_error(DB_PATH, activity_id, uid, "render",
                       "; ".join(errors) if errors else None)

    _compute_and_store_metrics(activity_id, uid, cfg, stream, row)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _compute_and_store_metrics(activity_id: int, uid: int, cfg: dict, stream: list, row):
    """Compute NP, TSS, TRIMP, peak powers, and zone times from stream data and store them."""
    try:
        row = dict(row)
        watts_list   = [p.get("power")        for p in stream]
        hr_list      = [p.get("hr")           for p in stream]
        elapsed_list = [p.get("elapsed_secs") for p in stream]

        ftp    = cfg["charts"]["power"]["ftp"]
        hr_max = cfg["charts"]["heart_rate"]["max_hr"]

        if not ftp:
            v = get_setting(DB_PATH, uid, "inference", "ftp")
            ftp = float(v) if v else None
        if not hr_max:
            v = get_setting(DB_PATH, uid, "inference", "max_hr")
            hr_max = float(v) if v else None

        hr_rest  = float(get_setting(DB_PATH, uid, "training", "hr_rest") or 0) or None
        lthr     = float(get_setting(DB_PATH, uid, "training", "lthr")     or 0) or None
        if not lthr and hr_max:
            lthr = hr_max * 0.88
        duration = row["moving_time"] or row["elapsed_time"]

        np_w      = compute_np(watts_list)
        tss       = compute_tss(np_w, duration, ftp) if np_w else None
        trimp     = compute_trimp(hr_list, elapsed_list, hr_max, hr_rest) if hr_max and hr_rest else None
        peaks     = compute_peak_powers(stream)
        peak_json = json.dumps(peaks) if peaks else None

        # Breakthrough detection — only on first-time metrics computation
        breakthroughs_json = None
        is_first_time = not row.get("peak_power_json") and not row.get("metrics_computed_at")
        if is_first_time:
            breakthrus = []
            if peaks:
                hist_peaks = get_all_peak_powers(DB_PATH, uid, exclude_id=activity_id)
                hist_mmp   = aggregate_power_curve(hist_peaks)
                for label, val in peaks.items():
                    prev = hist_mmp.get(label)
                    if prev is None or val > prev:
                        breakthrus.append({
                            "type": "mmp", "label": label,
                            "watts": round(val), "prev": round(prev) if prev else None,
                        })
            if row.get("max_heartrate"):
                conn_b = _conn(DB_PATH)
                prev_hr = conn_b.execute(
                    "SELECT MAX(max_heartrate) FROM activities"
                    " WHERE user_id=? AND id != ? AND max_heartrate IS NOT NULL",
                    (uid, activity_id),
                ).fetchone()[0]
                conn_b.close()
                if prev_hr is None or row["max_heartrate"] > prev_hr:
                    breakthrus.append({
                        "type": "hr",
                        "bpm": round(row["max_heartrate"]),
                        "prev": round(prev_hr) if prev_hr else None,
                    })
            breakthroughs_json = json.dumps(breakthrus)

        hr_tss = None
        if tss is None and hr_max and hr_rest and lthr:
            hr_tss = compute_hr_tss(hr_list, elapsed_list, hr_max, hr_rest, lthr)

        hr_zones    = cfg["charts"]["heart_rate"]["zones"]
        power_zones = cfg["charts"]["power"]["zones"]
        hr_zone_secs, power_zone_secs = compute_zone_times(
            stream, hr_zones, power_zones, hr_max, ftp
        )
        hr_zone_json    = json.dumps(hr_zone_secs)    if hr_zone_secs    else None
        power_zone_json = json.dumps(power_zone_secs) if power_zone_secs else None

        update_activity_metrics(
            DB_PATH, activity_id, uid,
            tss, np_w, trimp, peak_json,
            hr_zone_json, power_zone_json,
            hr_tss=hr_tss,
            breakthroughs_json=breakthroughs_json,
        )

        if not cfg["charts"]["power"]["ftp"] and peaks and peaks.get("20min"):
            new_ftp = peaks["20min"] * 0.95
            cached  = get_setting(DB_PATH, uid, "inference", "ftp")
            if not cached or new_ftp > float(cached):
                set_setting(DB_PATH, uid, "inference", "ftp", str(round(new_ftp, 1)))
                print(f"[metrics] Updated inferred FTP: {new_ftp:.0f} W")

        if not cfg["charts"]["heart_rate"]["max_hr"] and row.get("max_heartrate"):
            new_hr   = row["max_heartrate"]
            cached   = get_setting(DB_PATH, uid, "inference", "max_hr")
            cached_f = float(cached) if cached else 0
            # Guard against sensor spikes: only accept if plausible (≤220) and
            # not more than 10 bpm above the current cached value
            if new_hr <= 220 and new_hr > cached_f and (not cached_f or new_hr <= cached_f + 10):
                set_setting(DB_PATH, uid, "inference", "max_hr", str(round(new_hr, 1)))
                print(f"[metrics] Updated inferred max HR: {new_hr:.0f} bpm")

    except Exception as e:
        print(f"[metrics] Failed for {activity_id}: {e}")


def run_metrics_backfill(uid: int):
    """Compute metrics for activities that have never been processed. Thread-safe via _backfill_lock."""
    if not _backfill_lock.acquire(blocking=False):
        return
    try:
        pending = get_activities_without_metrics(DB_PATH, uid)
        if not pending:
            return
        job_id = job_start(DB_PATH, "metrics_backfill",
                           f"Starting — {len(pending)} activities pending")
        print(f"[backfill] Starting metrics for {len(pending)} activities (user {uid})")

        bcfg_pre = load_user_config(DB_PATH, uid, _base_cfg)
        if not bcfg_pre["charts"]["power"]["ftp"] or not bcfg_pre["charts"]["heart_rate"]["max_hr"]:
            inferred = infer_training_params(DB_PATH, uid)
            if inferred["ftp"] and not bcfg_pre["charts"]["power"]["ftp"]:
                set_setting(DB_PATH, uid, "inference", "ftp", str(round(inferred["ftp"], 1)))
                print(f"[backfill] Inferred FTP: {inferred['ftp']:.0f} W")
            if inferred["max_hr"] and not bcfg_pre["charts"]["heart_rate"]["max_hr"]:
                set_setting(DB_PATH, uid, "inference", "max_hr", str(round(inferred["max_hr"], 1)))
                print(f"[backfill] Inferred max HR: {inferred['max_hr']:.0f} bpm")

        done = 0
        for brow in pending:
            try:
                bcfg    = load_user_config(DB_PATH, uid, _base_cfg)
                bstream = get_stream(brow)
                _compute_and_store_metrics(brow["id"], uid, bcfg, bstream, brow)
                done += 1
            except Exception as e:
                print(f"[backfill] Error on activity {brow['id']}: {e}")
            _time.sleep(0.05)
        job_finish(DB_PATH, job_id, "done",
                   f"Computed metrics for {done}/{len(pending)} activities")
        print(f"[backfill] Done — {done}/{len(pending)} activities processed")
    except Exception as e:
        print(f"[backfill] Fatal error: {e}")
    finally:
        _backfill_lock.release()


# ---------------------------------------------------------------------------
# Mastodon posting
# ---------------------------------------------------------------------------

def _build_post_text(activity: dict, template: str) -> str:
    def fmt_time(secs):
        if not secs:
            return "?"
        h, m = divmod(int(secs) // 60, 60)
        return f"{h}h {m:02d}m" if h else f"{m}m"
    return template.format(
        name          = activity.get("name", "Activity"),
        distance_km   = (activity.get("distance") or 0) / 1000,
        elevation_m   = activity.get("total_elevation_gain") or 0,
        moving_time   = fmt_time(activity.get("moving_time")),
        average_speed = (activity.get("average_speed") or 0) * 3.6,
        date          = (activity.get("start_date") or "")[:10],
        sport_type    = activity.get("sport_type") or "",
    )


def _collect_activity_images(activity_id: int, uid: int, cfg: dict, out_dir: str,
                              row) -> list[str]:
    """Return up to 4 image paths (map + charts) for an activity, generating them if needed."""
    img_path = os.path.join(out_dir, f"{activity_id}.png")
    if not os.path.exists(img_path):
        _render_and_track(activity_id, uid, cfg, out_dir, row=row)
        img_path_exists = os.path.exists(img_path)
    else:
        img_path_exists = True

    stream      = get_stream(row)
    chart_paths = generate_charts(activity_id, stream, cfg, out_dir, db_path=DB_PATH, user_id=uid)
    images      = ([img_path] if img_path_exists else []) + chart_paths
    return images[:4]


def _do_post_activity(activity_id: int, uid: int):
    """Post an activity to Mastodon. Meant to run in a background thread."""
    row = get_activity(DB_PATH, activity_id, user_id=uid)
    if not row:
        return

    cfg     = load_user_config(DB_PATH, uid, _base_cfg)
    out_dir = _base_cfg["map"].get("output_dir", "output")
    os.makedirs(out_dir, exist_ok=True)
    img_path = os.path.join(out_dir, f"{activity_id}.png")

    if not os.path.exists(img_path):
        _render_and_track(activity_id, uid, cfg, out_dir, row=row)
        row = get_activity(DB_PATH, activity_id, user_id=uid)
        if not row or row["render_error"]:
            msg = (row["render_error"] if row else "render failed")
            set_activity_error(DB_PATH, activity_id, uid, "post", f"render required first: {msg}")
            set_scheduled(DB_PATH, activity_id, uid, False)
            return

    try:
        text   = _build_post_text(dict(row), cfg["mastodon"].get("post_template", "{name}\n#cycling"))
        images = _collect_activity_images(activity_id, uid, cfg, out_dir, row)

        client    = MastodonClient.from_cfg(cfg)
        media_ids = [client.upload_image(p) for p in images]
        resp = client._session.post(
            f"{client._base}/api/v1/statuses",
            json={
                "status":     text,
                "media_ids":  media_ids,
                "visibility": cfg["mastodon"].get("visibility", "public"),
            },
        )
        resp.raise_for_status()
        post_url = resp.json().get("url", "")
        mark_posted(DB_PATH, activity_id, post_url, user_id=uid)
        set_activity_error(DB_PATH, activity_id, uid, "post", None)
        print(f"[post] Posted activity {activity_id} → {post_url}")
    except Exception as e:
        set_activity_error(DB_PATH, activity_id, uid, "post", str(e))
        set_scheduled(DB_PATH, activity_id, uid, False)
        print(f"[post] Failed to post activity {activity_id}: {e}")
