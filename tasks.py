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

from config import DB_PATH, OUTPUT_DIR, _base_cfg

from database import (
    _conn, get_activities_without_metrics, get_activity, get_athlete_param,
    get_mmp_as_of, get_power_best, get_setting,
    load_user_config, mark_rendered, mark_posted,
    set_activity_error, set_athlete_param, set_power_best, set_scheduled,
    set_wbal_json, update_activity_metrics,
)
from activity_parser import points_from_file, stream_from_file
from charts import generate_charts
from map_renderer import render_activity_map
from mastodon_client import MastodonClient
from training_load import (
    _label_to_secs,
    compute_hr_tss, compute_np, compute_peak_powers,
    compute_trimp, compute_tss, compute_wbal, compute_zone_times, fit_critical_power,
)

_backfill_lock  = threading.Lock()
_backfill_event = threading.Event()
_backfill_uids: set[int] = set()
_backfill_uids_lock = threading.Lock()


def request_backfill(uid: int) -> None:
    """Signal the backfill worker to process pending metrics for uid."""
    with _backfill_uids_lock:
        _backfill_uids.add(uid)
    _backfill_event.set()


def start_backfill_worker() -> None:
    """Start the persistent backfill worker thread. Safe to call once at startup."""
    def _loop():
        while True:
            _backfill_event.wait()
            _backfill_event.clear()
            with _backfill_uids_lock:
                uids = list(_backfill_uids)
                _backfill_uids.clear()
            for uid in uids:
                try:
                    run_metrics_backfill(uid)
                except Exception as e:
                    print(f"[backfill-worker] Unexpected error for user {uid}: {e}")

    t = threading.Thread(target=_loop, daemon=True, name="backfill-worker")
    t.start()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _find_activity_image(out_dir: str, stem: str) -> str | None:
    """Return path to an activity image, preferring .jpg over legacy .png."""
    for ext in (".jpg", ".png"):
        p = os.path.join(out_dir, f"{stem}{ext}")
        if os.path.exists(p):
            return p
    return None


def _render_and_track(activity_id: int, uid: int, cfg: dict, out_dir: str, row=None):
    """Render map + charts for an activity, storing any errors in the DB."""
    if row is None:
        row = get_activity(DB_PATH, activity_id, user_id=uid)
    if not row:
        return
    os.makedirs(out_dir, exist_ok=True)
    errors = []

    source_file = row["source_file"]
    pts = points_from_file(source_file) if source_file else []
    if pts:
        try:
            img = render_activity_map(pts, dict(row), cfg)
            if img:
                from PIL import Image as _Image
                if img.mode == "RGBA":
                    bg = _Image.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[3])
                    img = bg
                img.save(os.path.join(out_dir, f"{activity_id}.jpg"), "JPEG", quality=85)
            mark_rendered(DB_PATH, activity_id, uid, map=True)
        except Exception as e:
            errors.append(f"map: {e}")
            print(f"[render] map failed for {activity_id}: {e}")
    else:
        mark_rendered(DB_PATH, activity_id, uid, map=True)

    if not cfg["charts"]["power"]["ftp"]:
        v = get_athlete_param(DB_PATH, uid, "ftp")
        if v:
            cfg["charts"]["power"]["ftp"] = float(v)
    if not cfg["charts"]["heart_rate"]["max_hr"]:
        v = get_athlete_param(DB_PATH, uid, "max_hr")
        if v:
            cfg["charts"]["heart_rate"]["max_hr"] = float(v)

    stream = stream_from_file(source_file) if source_file else []
    try:
        generate_charts(activity_id, stream, cfg, out_dir, db_path=DB_PATH, user_id=uid)
        mark_rendered(DB_PATH, activity_id, uid, charts=True)
    except Exception as e:
        errors.append(f"charts: {e}")
        print(f"[render] charts failed for {activity_id}: {e}")

    set_activity_error(DB_PATH, activity_id, uid, "render",
                       "; ".join(errors) if errors else None)

    process_activity(activity_id, uid, cfg, stream, row)


# ---------------------------------------------------------------------------
# Metrics pipeline
# ---------------------------------------------------------------------------

def _update_physiology(uid: int, activity_id: int, date: str,
                       peaks: dict | None, activity_row: dict) -> list[dict]:
    """Update power_bests and max_hr in athlete_params from this activity.
    Comparisons are as_of date so processing order does not matter.
    Returns a list of breakthrough dicts."""
    breakthroughs = []

    if peaks:
        for label, watts in peaks.items():
            if watts is None:
                continue
            prev_best = get_power_best(DB_PATH, uid, label, as_of=date)
            prev_watts = prev_best["power_watts"] if prev_best else None
            if set_power_best(DB_PATH, uid, label, watts, activity_id, date):
                breakthroughs.append({
                    "type": "mmp", "label": label,
                    "watts": round(watts),
                    "prev": round(prev_watts) if prev_watts else None,
                })

    max_hr = activity_row.get("max_heartrate")
    if max_hr and max_hr <= 220:
        prev_max_hr = get_athlete_param(DB_PATH, uid, "max_hr", as_of=date)
        if not prev_max_hr or max_hr > prev_max_hr:
            if set_athlete_param(DB_PATH, uid, "max_hr", max_hr,
                                 source="derived", activity_id=activity_id, date=date,
                                 cleanup_superseded=True):
                breakthroughs.append({
                    "type": "hr", "bpm": round(max_hr),
                    "prev": round(prev_max_hr) if prev_max_hr else None,
                })

    return breakthroughs


def _estimate_derived_params(uid: int, activity_id: int, date: str) -> dict:
    """Fit CP/W' and FTP from power_bests as_of date. Writes to athlete_params.
    Returns {cp, w_prime, ftp, cp_changed, prev_cp}."""
    mmp = get_mmp_as_of(DB_PATH, uid, as_of=date)
    cp = w_prime = ftp = None
    cp_changed = False
    prev_cp = get_athlete_param(DB_PATH, uid, "cp_watts", as_of=date)

    if mmp:
        cp, w_prime = fit_critical_power(mmp)
        # Only record CP if the estimate improved — a new MMP point can change the OLS
        # slope without the athlete actually getting weaker, so we treat CP like a power
        # best: only record it when it goes up.
        if cp and (prev_cp is None or cp > prev_cp):
            cp_changed = set_athlete_param(DB_PATH, uid, "cp_watts", cp,
                                           source="derived", activity_id=activity_id, date=date)
            set_athlete_param(DB_PATH, uid, "w_prime_joules", w_prime,
                              source="derived", activity_id=activity_id, date=date)

        # Find the power at the label closest to 20 min (1200s) within ±5 min.
        # Labels use Xs / Nmin / Nh format so we need _label_to_secs to resolve them.
        _TARGET = 1200
        _WINDOW = 300
        best_20min = None
        best_dist  = _WINDOW + 1
        for lbl, w in mmp.items():
            t = _label_to_secs(lbl)
            if t is None:
                continue
            dist = abs(t - _TARGET)
            if dist <= _WINDOW and dist < best_dist:
                best_20min = w
                best_dist  = dist
        if best_20min:
            ftp = round(best_20min * 0.95, 1)
        elif cp:
            ftp = cp
        if ftp:
            set_athlete_param(DB_PATH, uid, "ftp", ftp,
                              source="derived", activity_id=activity_id, date=date)

    return {"cp": cp, "w_prime": w_prime, "ftp": ftp,
            "cp_changed": cp_changed, "prev_cp": prev_cp}


def _finalize_activity_metrics(uid: int, activity_id: int, date: str,
                                stream: list, row: dict, cfg: dict,
                                peaks: dict | None, breakthroughs: list) -> None:
    """Compute TSS/TRIMP/wbal using athlete_params as_of date and save to the activity row."""
    # Read physiological params as known on this date — from athlete_params first,
    # then fall back to cfg (user-configured static values)
    ftp     = get_athlete_param(DB_PATH, uid, "ftp",    as_of=date) or cfg["charts"]["power"]["ftp"] or None
    hr_max  = get_athlete_param(DB_PATH, uid, "max_hr", as_of=date) or cfg["charts"]["heart_rate"]["max_hr"] or None
    hr_rest = get_athlete_param(DB_PATH, uid, "rest_hr", as_of=date)
    cp      = get_athlete_param(DB_PATH, uid, "cp_watts",      as_of=date)
    w_prime = get_athlete_param(DB_PATH, uid, "w_prime_joules", as_of=date)

    lthr = float(get_setting(DB_PATH, uid, "training", "lthr") or 0) or None
    if not lthr and hr_max:
        lthr = hr_max * 0.88

    watts_list   = [p.get("power")        for p in stream]
    hr_list      = [p.get("hr")           for p in stream]
    elapsed_list = [p.get("elapsed_secs") for p in stream]
    duration     = row.get("moving_time") or row.get("elapsed_time")

    np_w  = compute_np(watts_list)
    tss   = compute_tss(np_w, duration, ftp)   if np_w and ftp  else None
    trimp = compute_trimp(hr_list, elapsed_list, hr_max, hr_rest) if hr_max and hr_rest else None
    hr_tss = None
    if tss is None and hr_max and hr_rest and lthr:
        hr_tss = compute_hr_tss(hr_list, elapsed_list, hr_max, hr_rest, lthr)

    hr_zones    = cfg["charts"]["heart_rate"]["zones"]
    power_zones = cfg["charts"]["power"]["zones"]
    hr_zone_secs, power_zone_secs = compute_zone_times(
        stream, hr_zones, power_zones, hr_max, ftp
    )

    update_activity_metrics(
        DB_PATH, activity_id, uid,
        tss, np_w, trimp,
        json.dumps(peaks) if peaks else None,
        json.dumps(hr_zone_secs)    if hr_zone_secs    else None,
        json.dumps(power_zone_secs) if power_zone_secs else None,
        hr_tss=hr_tss,
        breakthroughs_json=json.dumps(breakthroughs) if breakthroughs is not None else None,
    )

    if cp and w_prime and np_w:
        try:
            wbal = compute_wbal(stream, cp, w_prime)
            if wbal:
                set_wbal_json(DB_PATH, activity_id, uid, json.dumps(wbal))
        except Exception as e:
            print(f"[metrics] wbal failed for {activity_id}: {e}")


def process_activity(activity_id: int, uid: int, cfg: dict,
                     stream: list, row) -> None:
    """Run the full metrics pipeline for one activity."""
    try:
        row  = dict(row)
        date = (row.get("start_date") or "")[:10]
        if not date:
            return

        peaks         = compute_peak_powers(stream)
        breakthroughs = _update_physiology(uid, activity_id, date, peaks, row)
        derived       = _estimate_derived_params(uid, activity_id, date)

        if derived["cp_changed"] and derived["cp"]:
            breakthroughs.append({
                "type": "cp", "cp_watts": round(derived["cp"]),
                "prev": round(derived["prev_cp"]) if derived["prev_cp"] else None,
            })

        _finalize_activity_metrics(uid, activity_id, date, stream, row, cfg,
                                   peaks, breakthroughs)

    except Exception as e:
        print(f"[metrics] Failed for {activity_id}: {e}")


def run_metrics_backfill(uid: int):
    """Compute metrics for activities that have not yet been processed."""
    pending = get_activities_without_metrics(DB_PATH, uid)
    if not pending:
        return
    try:
        print(f"[backfill] Starting metrics for {len(pending)} activities (user {uid})")
        done = 0
        for brow in pending:
            try:
                bcfg        = load_user_config(DB_PATH, uid, _base_cfg)
                source_file = brow["source_file"]
                bstream     = stream_from_file(source_file) if source_file else []
                process_activity(brow["id"], uid, bcfg, bstream, brow)
                done += 1
            except Exception as e:
                print(f"[backfill] Error on activity {brow['id']}: {e}")
            _time.sleep(0.05)
        print(f"[backfill] Done — {done}/{len(pending)} activities processed")
    except Exception as e:
        print(f"[backfill] Fatal error: {e}")


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
    img_path = _find_activity_image(out_dir, str(activity_id))
    if not img_path:
        _render_and_track(activity_id, uid, cfg, out_dir, row=row)
        img_path = _find_activity_image(out_dir, str(activity_id))

    source_file = row["source_file"]
    stream      = stream_from_file(source_file) if source_file else []
    chart_paths = generate_charts(activity_id, stream, cfg, out_dir, db_path=DB_PATH, user_id=uid)
    images      = ([img_path] if img_path else []) + chart_paths
    return images[:4]


def _do_post_activity(activity_id: int, uid: int):
    """Post an activity to Mastodon. Meant to run in a background thread."""
    row = get_activity(DB_PATH, activity_id, user_id=uid)
    if not row:
        return

    cfg     = load_user_config(DB_PATH, uid, _base_cfg)
    out_dir = OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    if not _find_activity_image(out_dir, str(activity_id)):
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
