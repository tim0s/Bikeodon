#!/usr/bin/env python3
"""
Bikeodon – CLI
Usage:
  python main.py sync              # fetch 10 most recent Strava activities
  python main.py sync --count 20   # fetch more
  python main.py list              # list activities stored in the database
  python main.py render            # render map for the most recent activity
  python main.py render 12345678   # render map for a specific activity ID
  python main.py charts 12345678   # generate HR/power charts
  python main.py post 12345678     # render and post to Mastodon
  python main.py config list       # show all per-user settings
  python main.py config get <area> <key>
  python main.py config set <area> <key> <value>
"""

import argparse
import os
import sys
import time

# Force line-buffered output so systemd journald sees prints immediately.
sys.stdout.reconfigure(line_buffering=True)

import yaml
from dotenv import load_dotenv

load_dotenv()

from activity_parser import points_from_file, stream_from_file
from charts import generate_charts
from database import (
    clear_rendered, get_activity, get_all_users, get_latest_activity_date,
    get_setting, get_site_setting, get_unposted,
    get_unrendered, get_user_by_username, init_db, list_activities, list_settings,
    load_user_config, mark_posted, mark_rendered, save_activity_file,
    set_admin, set_setting, set_site_setting, upsert_activity,
)
from fit_encoder import generate_fit
from strava import StravaClient, delete_webhook, list_webhooks, register_webhook
from map_renderer import render_activity_map
from mastodon_client import MastodonClient
from tasks import _render_and_track
from database import (
    get_activities_without_metrics, reset_metrics_computed,
    get_all_peak_powers,
)
from training_load import (
    aggregate_power_curve, compute_pmc, fit_critical_power,
)

STRAVA_CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")


def _resolve_user_id(db_path: str, username: str | None) -> int:
    """Return the user_id to use for CLI operations."""
    if username:
        user = get_user_by_username(db_path, username)
        if not user:
            print(f"User '{username}' not found.")
            sys.exit(1)
        return user["id"]
    users = get_all_users(db_path)
    if not users:
        print("No connected users found. Connect Strava via the web UI first.")
        sys.exit(1)
    return users[0]["id"]


def _load_cfg(config_path: str, user_id: int) -> dict:
    with open(config_path) as f:
        base = yaml.safe_load(f)
    db_path = base["database"]["path"]
    init_db(db_path)
    return load_user_config(db_path, user_id, base)


def _strava_client_for(db_path: str, user_id: int) -> "StravaClient":
    access_token = get_setting(db_path, user_id, "strava", "access_token") or ""
    refresh_tok  = get_setting(db_path, user_id, "strava", "refresh_token") or ""
    expires_at   = float(get_setting(db_path, user_id, "strava", "token_expires_at") or 0)

    if not access_token:
        raise ValueError(
            f"User {user_id} has no Strava token. Connect via the web UI first."
        )

    def _on_refresh(new_access, new_refresh, new_expires):
        set_setting(db_path, user_id, "strava", "access_token",     new_access)
        set_setting(db_path, user_id, "strava", "refresh_token",    new_refresh)
        set_setting(db_path, user_id, "strava", "token_expires_at", str(new_expires))

    return StravaClient(
        access_token=access_token,
        client_id=STRAVA_CLIENT_ID,
        client_secret=STRAVA_CLIENT_SECRET,
        refresh_tok=refresh_tok,
        expires_at=expires_at,
        on_refresh=_on_refresh,
    )


def _sync_user(db_path: str, user_id: int, username: str,
               count: int = 20, full: bool = False, base_cfg: dict | None = None) -> list[int]:
    """
    Fetch new activities for one user from Strava and store them.
    Returns the list of newly stored activity IDs.
    """
    try:
        client = _strava_client_for(db_path, user_id)
    except ValueError as e:
        print(f"  [{username}] Skipping — {e}")
        return []

    latest = get_latest_activity_date(db_path, user_id)
    after_ts = None
    if latest and not full:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
        after_ts = dt.timestamp()
        print(f"  [{username}] Fetching activities after {latest[:10]}…")
    elif full:
        print(f"  [{username}] Full sync — fetching in pages of 20…")
    else:
        print(f"  [{username}] No existing activities — fetching up to {count} most recent…")

    files_dir = os.path.join((base_cfg or {}).get("map", {}).get("output_dir", "output"), "activity_files")
    new_ids = []

    if full:
        # Collect all IDs first (Strava returns newest-first), then reverse so
        # activities are stored oldest-first — required for CP history to build
        # up correctly over time.
        all_ids = []
        page = 1
        while True:
            ids = client.get_activity_ids(n=20, after=after_ts, page=page)
            if not ids:
                break
            print(f"  [{username}] Listing page {page}: {len(ids)} activities…")
            all_ids.extend(ids)
            page += 1
        all_ids.reverse()
        print(f"  [{username}] Fetching {len(all_ids)} activities oldest-first…")
        for activity_id in all_ids:
            try:
                data, streams = client.get_activity(activity_id)
                try:
                    fit_bytes = generate_fit(data, streams)
                    data["source_file"], data["source_file_sha256"] = \
                        save_activity_file(files_dir, activity_id, user_id, fit_bytes, f"{activity_id}.fit")
                    data["source_file_type"] = "generated"
                except Exception as fe:
                    print(f"    FIT generation failed for {activity_id}: {fe}")
                upsert_activity(db_path, data, user_id=user_id)
                print(f"    + {data['name']}  ({(data.get('distance') or 0) / 1000:.1f} km)")
                new_ids.append(activity_id)
            except Exception as e:
                print(f"    Failed {activity_id}: {e}")
    else:
        ids = client.get_activity_ids(n=count, after=after_ts)
        if not ids:
            print(f"  [{username}] No new activities.")
            return []
        for activity_id in ids:
            try:
                data, streams = client.get_activity(activity_id)
                try:
                    fit_bytes = generate_fit(data, streams)
                    data["source_file"], data["source_file_sha256"] = \
                        save_activity_file(files_dir, activity_id, user_id, fit_bytes, f"{activity_id}.fit")
                    data["source_file_type"] = "generated"
                except Exception as fe:
                    print(f"    FIT generation failed for {activity_id}: {fe}")
                upsert_activity(db_path, data, user_id=user_id)
                print(f"    + {data['name']}  ({(data.get('distance') or 0) / 1000:.1f} km)")
                new_ids.append(activity_id)
            except Exception as e:
                print(f"    Failed {activity_id}: {e}")

    return new_ids


def _render_missing(db_path: str, user_id: int, cfg: dict):
    """Render maps and charts for any activities not yet rendered according to the DB."""
    rows = get_unrendered(db_path, user_id=user_id)
    if rows:
        _render_activities([r["id"] for r in rows], db_path, user_id, cfg,
                           force_map=False, force_charts=False)


def _render_activities(ids: list[int], db_path: str, user_id: int, cfg: dict,
                       force_map: bool = True, force_charts: bool = True):
    """Render maps and charts for a list of activity IDs."""
    out_dir = cfg["map"].get("output_dir", "output")
    os.makedirs(out_dir, exist_ok=True)
    for activity_id in ids:
        row = get_activity(db_path, activity_id, user_id=user_id)
        if not row:
            continue

        need_map    = force_map    or not row["map_rendered_at"]
        need_charts = force_charts or not row["charts_rendered_at"]
        source_file = row["source_file"]

        if need_map:
            map_path = os.path.join(out_dir, f"{activity_id}.png")
            points = points_from_file(source_file) if source_file else []
            if points:
                try:
                    img = render_activity_map(points, dict(row), cfg)
                    if img:
                        img.save(map_path)
                        mark_rendered(db_path, activity_id, user_id, map=True)
                        print(f"    Rendered map → {map_path}")
                except Exception as e:
                    print(f"    Map render failed for {activity_id}: {e}")
            else:
                mark_rendered(db_path, activity_id, user_id, map=True)

        if need_charts:
            stream = stream_from_file(source_file) if source_file else []
            try:
                paths = generate_charts(activity_id, stream, cfg, out_dir, db_path=db_path, user_id=user_id)
                for p in paths:
                    print(f"    Rendered chart → {p}")
            except Exception as e:
                print(f"    Chart render failed for {activity_id}: {e}")
            mark_rendered(db_path, activity_id, user_id, charts=True)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_sync(args, cfg):
    db_path = cfg["database"]["path"]
    users   = get_all_users(db_path)

    if not users:
        print("No connected users. Connect Strava via the web UI first.")
        return

    total = 0
    for user in users:
        new_ids = _sync_user(db_path, user["id"], user["username"] or f"user:{user['id']}",
                             count=args.count, full=getattr(args, "full", False),
                             base_cfg=args.base_cfg)
        total += len(new_ids)
        if new_ids:
            user_cfg = load_user_config(db_path, user["id"], args.base_cfg)
            _render_activities(new_ids, db_path, user["id"], user_cfg)

    print(f"\nDone — {total} new activit{'y' if total == 1 else 'ies'} imported.")


def cmd_list(args, cfg):
    db_path = cfg["database"]["path"]
    rows = list_activities(db_path, user_id=args.user_id)

    if not rows:
        print("No activities in database. Run 'sync' first.")
        return

    header = f"{'ID':<14} {'Date':<12} {'Type':<18} {'Distance':>9} {'Elev':>8}  {'Posted':<11}  Name"
    print(header)
    print("─" * len(header))
    for r in rows:
        dist   = f"{(r['distance'] or 0) / 1000:.1f} km"
        elev   = f"{r['total_elevation_gain'] or 0:.0f} m"
        date   = (r["start_date"] or "")[:10]
        stype  = r["sport_type"] or ""
        posted = "✓" if r["posted_at"] else "–"
        print(f"{r['id']:<14} {date:<12} {stype:<18} {dist:>9} {elev:>8}  {posted:<11}  {r['name']}")


def cmd_render(args, cfg):
    db_path = cfg["database"]["path"]

    if args.activity_id:
        row = get_activity(db_path, args.activity_id, user_id=args.user_id)
        if not row:
            print(f"Activity {args.activity_id} not found. Run 'sync' first.")
            sys.exit(1)
        rows = [row]
    else:
        all_rows = list_activities(db_path, user_id=args.user_id)
        if not all_rows:
            print("No activities in database. Run 'sync' first.")
            sys.exit(1)
        rows = [all_rows[0]]

    out_dir = cfg["map"].get("output_dir", "output")
    os.makedirs(out_dir, exist_ok=True)

    for row in rows:
        source_file = row["source_file"]
        points = points_from_file(source_file) if source_file else []
        print(f"Rendering [{row['id']}] {row['name']}…")
        if not points:
            print("  No GPS data for this activity, skipping.")
            continue
        img = render_activity_map(points, dict(row), cfg)
        if img is None:
            print("  Render returned no image.")
            continue
        out_path = os.path.join(out_dir, f"{row['id']}.png")
        img.save(out_path)
        print(f"  Saved → {out_path}")


def cmd_charts(args, cfg):
    db_path = cfg["database"]["path"]
    row = get_activity(db_path, args.activity_id, user_id=args.user_id)
    if not row:
        print(f"Activity {args.activity_id} not found.")
        sys.exit(1)
    source_file = row["source_file"]
    stream  = stream_from_file(source_file) if source_file else []
    out_dir = cfg["map"].get("output_dir", "output")
    print(f"Generating charts for [{row['id']}] {row['name']}…")
    paths = generate_charts(row["id"], stream, cfg, out_dir, db_path=db_path, user_id=args.user_id)
    if not paths:
        print("  No chart data available (no HR or power recorded).")
    for p in paths:
        print(f"  → {p}")


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


def cmd_post(args, cfg):
    db_path = cfg["database"]["path"]
    out_dir = cfg["map"].get("output_dir", "output")

    row = get_activity(db_path, args.activity_id, user_id=args.user_id)
    if not row:
        print(f"Activity {args.activity_id} not found. Run 'sync' first.")
        sys.exit(1)

    text = _build_post_text(dict(row), cfg["mastodon"].get("post_template", "{name}\n#cycling"))
    print(f"Post preview:\n{'─' * 40}")
    print(text)
    print("─" * 40)

    if args.dry_run:
        print("(dry run — nothing posted)")
        return

    confirm = input("\nPost to Mastodon? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    try:
        url = _do_post(row, cfg, db_path, out_dir, rerender=args.rerender, user_id=args.user_id)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    if url:
        print(f"\nPosted! {url}")


# ---------------------------------------------------------------------------
# Shared posting logic (used by cmd_post and cmd_daemon)
# ---------------------------------------------------------------------------

def _do_post(row, cfg, db_path: str, out_dir: str, rerender: bool = False, user_id: int = 1) -> str | None:
    """
    Render, upload, and post a single activity. Returns the Mastodon post URL
    on success, or None if posting was skipped (no GPS data, render failure).
    Raises on network/API errors.
    """
    activity_id = row["id"]
    img_path    = os.path.join(out_dir, f"{activity_id}.png")

    if not os.path.exists(img_path) or rerender:
        _render_and_track(activity_id, user_id, cfg, out_dir, row=row)
        row = get_activity(db_path, activity_id, user_id=user_id)
        if not row or not os.path.exists(img_path):
            print(f"  [{activity_id}] No GPS data or render failed — skipping.")
            return None

    text        = _build_post_text(dict(row), cfg["mastodon"].get("post_template", "{name}\n#cycling"))
    source_file = row["source_file"]
    stream      = stream_from_file(source_file) if source_file else []
    chart_paths = generate_charts(activity_id, stream, cfg, out_dir, db_path=db_path, user_id=user_id)
    all_images  = ([img_path] + chart_paths)[:4]

    client    = MastodonClient.from_cfg(cfg)
    media_ids = [client.upload_image(p) for p in all_images]

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
    mark_posted(db_path, activity_id, post_url, user_id=user_id)
    return post_url


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Metrics backfill
# ---------------------------------------------------------------------------

def cmd_metrics(args, cfg):
    import json as _json
    db_path = cfg["database"]["path"]
    uid     = args.user_id

    if args.all:
        print("Clearing metrics_computed_at for all activities…")
        reset_metrics_computed(db_path, uid)
        # Also clear cached inference so it re-runs from scratch
        set_setting(db_path, uid, "inference", "ftp",      "")
        set_setting(db_path, uid, "inference", "max_hr",   "")
        set_setting(db_path, uid, "inference", "cp",       "")
        set_setting(db_path, uid, "inference", "w_prime",  "")

    from tasks import run_metrics_backfill
    run_metrics_backfill(uid)

    # Fit CP/W' from the now-populated MMP curve
    all_peaks   = get_all_peak_powers(db_path, uid)
    curve       = aggregate_power_curve(all_peaks)
    cp, w_prime = fit_critical_power(curve)
    if cp:
        set_setting(db_path, uid, "inference", "cp",      str(cp))
        set_setting(db_path, uid, "inference", "w_prime", str(w_prime))
        print(f"\nCritical Power fit:  CP={cp:.0f} W   W'={w_prime/1000:.1f} kJ")
    else:
        print("\nCould not fit CP model (need MMP data at ≥2 durations ≥1 min).")


# ---------------------------------------------------------------------------
# Config management
# ---------------------------------------------------------------------------

def cmd_webhook(args, cfg):
    verify_token = os.environ.get("STRAVA_WEBHOOK_VERIFY_TOKEN", "")

    if args.webhook_cmd == "status":
        subs = list_webhooks(STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET)
        if not subs:
            print("No webhook subscription registered.")
        for s in subs:
            print(f"  ID {s['id']}  callback: {s['callback_url']}")

    elif args.webhook_cmd == "subscribe":
        if not verify_token:
            print("STRAVA_WEBHOOK_VERIFY_TOKEN not set in .env")
            sys.exit(1)
        result = register_webhook(
            STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET,
            args.callback_url, verify_token,
        )
        print(f"Subscribed. ID: {result.get('id')}")

    elif args.webhook_cmd == "unsubscribe":
        subs = list_webhooks(STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET)
        if not subs:
            print("No subscription to remove.")
            return
        for s in subs:
            delete_webhook(STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, s["id"])
            print(f"Deleted subscription {s['id']}.")


def cmd_admin(args, cfg):
    db_path = cfg["database"]["path"]
    user = get_user_by_username(db_path, args.username)
    if not user:
        print(f"User '{args.username}' not found.")
        sys.exit(1)
    set_admin(db_path, args.username, True)
    print(f"'{args.username}' is now an admin.")


def cmd_invite_code(args, cfg):
    db_path = cfg["database"]["path"]
    if args.code:
        set_site_setting(db_path, "invite_code", args.code)
        print(f"Invite code set.")
    else:
        code = get_site_setting(db_path, "invite_code")
        if code:
            print(f"Current invite code: {code}")
        else:
            print("No invite code set — registration is open to anyone.")


def cmd_config(args, cfg):
    db_path = cfg["database"]["path"]

    if args.config_cmd == "list":
        rows = list_settings(db_path, args.user_id)
        if not rows:
            print("No settings found.")
            return
        area_width = max(len(r["area"]) for r in rows)
        key_width  = max(len(r["key"])  for r in rows)
        for r in rows:
            value = r["value"] or ""
            if r["key"] in ("token",) and value:
                value = value[:6] + "…"
            print(f"  {r['area']:<{area_width}}  {r['key']:<{key_width}}  {value}")

    elif args.config_cmd == "get":
        value = get_setting(db_path, args.user_id, args.area, args.key)
        if value is None:
            print(f"No setting found for {args.area}/{args.key}")
            sys.exit(1)
        if args.key in ("token",) and value:
            value = value[:6] + "…"
        print(value)

    elif args.config_cmd == "set":
        set_setting(db_path, args.user_id, args.area, args.key, args.value)
        print(f"Set {args.area}/{args.key}")

    else:
        print("Usage: config list | config get <area> <key> | config set <area> <key> <value>")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Bikeodon – Strava → Mastodon bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config.yaml", metavar="FILE",
                        help="Path to config file (default: config.yaml)")
    sub = parser.add_subparsers(dest="command")

    p_sync = sub.add_parser("sync", help="Fetch recent activities from Strava")
    p_sync.add_argument("--count", type=int, default=20,
                        help="Number of activities to fetch (default: 20)")
    p_sync.add_argument("--full", action="store_true",
                        help="Fetch entire activity history (all pages)")

    sub.add_parser("list", help="List activities stored in the database")

    p_render = sub.add_parser("render", help="Render a map image for an activity")
    p_render.add_argument("activity_id", type=int, nargs="?",
                          help="Strava activity ID (default: most recent)")

    p_charts = sub.add_parser("charts", help="Generate HR/power charts for an activity")
    p_charts.add_argument("activity_id", type=int)

    p_post = sub.add_parser("post", help="Render and post an activity to Mastodon")
    p_post.add_argument("activity_id", type=int, help="Strava activity ID to post")
    p_post.add_argument("--dry-run",  action="store_true", help="Preview without publishing")
    p_post.add_argument("--rerender", action="store_true", help="Re-render even if image exists")

    p_webhook = sub.add_parser("webhook", help="Manage Strava webhook subscription")
    wh_sub = p_webhook.add_subparsers(dest="webhook_cmd")
    wh_sub.add_parser("status", help="Show current subscription")
    p_wh_sub = wh_sub.add_parser("subscribe", help="Register webhook with Strava")
    p_wh_sub.add_argument("callback_url", help="Public URL e.g. https://bikeodon.org/strava/webhook")
    wh_sub.add_parser("unsubscribe", help="Remove webhook subscription")

    p_admin = sub.add_parser("admin", help="Grant admin privileges to a user")
    p_admin.add_argument("username", help="Username to promote")

    p_metrics = sub.add_parser("metrics", help="Compute training metrics for all activities")
    p_metrics.add_argument("--all", action="store_true",
                           help="Recompute even already-processed activities")

    p_invite = sub.add_parser("invite-code", help="Set or show the registration invite code")
    p_invite.add_argument("code", nargs="?", help="New invite code (omit to show current)")

    p_cfg = sub.add_parser("config", help="View or update per-user settings")
    cfg_sub = p_cfg.add_subparsers(dest="config_cmd")
    cfg_sub.add_parser("list")
    p_cfg_get = cfg_sub.add_parser("get")
    p_cfg_get.add_argument("area")
    p_cfg_get.add_argument("key")
    p_cfg_set = cfg_sub.add_parser("set")
    p_cfg_set.add_argument("area")
    p_cfg_set.add_argument("key")
    p_cfg_set.add_argument("value")

    parser.add_argument("--user", metavar="USERNAME",
                        help="Username for CLI operations (default: first connected user)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    with open(args.config) as _f:
        _base = yaml.safe_load(_f)
    _db_path = _base["database"]["path"]
    init_db(_db_path)

    args.base_cfg = _base

    if args.command in ("sync", "invite-code", "admin", "webhook"):
        args.user_id = None
        cfg = load_user_config(_db_path, 1, _base)
    else:
        args.user_id = _resolve_user_id(_db_path, getattr(args, "user", None))
        cfg = load_user_config(_db_path, args.user_id, _base)

    {
        "sync":        cmd_sync,
        "list":        cmd_list,
        "render":      cmd_render,
        "charts":      cmd_charts,
        "post":        cmd_post,
        "webhook":     cmd_webhook,
        "admin":       cmd_admin,
        "invite-code": cmd_invite_code,
        "config":      cmd_config,
        "metrics":     cmd_metrics,
    }[args.command](args, cfg)


if __name__ == "__main__":
    main()
