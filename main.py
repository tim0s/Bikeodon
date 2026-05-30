#!/usr/bin/env python3
"""
Bikeodon – CLI prototype
Usage:
  python main.py sync              # fetch 10 most recent Strava activities
  python main.py sync --count 20   # fetch more
  python main.py list              # list activities stored in the database
  python main.py render            # render map for the most recent activity
  python main.py render 12345678   # render map for a specific activity ID
"""

import argparse
import os
import sys

import yaml
from dotenv import load_dotenv

load_dotenv()

from charts import generate_charts
from database import get_activity, get_points, get_stream, init_db, list_activities, upsert_activity
from map_renderer import render_activity_map
from mastodon_client import MastodonClient
from strava import StravaClient


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_sync(args, cfg):
    try:
        client = StravaClient()
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    db_path = cfg["database"]["path"]
    init_db(db_path)

    print(f"Fetching {args.count} most recent activity IDs…")
    try:
        ids = client.get_activity_ids(n=args.count)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    if not ids:
        print("No activities found.")
        return

    for activity_id in ids:
        print(f"  Downloading GPX for {activity_id}…")
        try:
            data = client.get_activity(activity_id)
            upsert_activity(db_path, data)
            pts = len(data.get("points") or [])
            print(f"    {data['name']}  ({pts} GPS points)")
        except Exception as e:
            print(f"    Failed: {e}")

    print(f"\nStored {len(ids)} activities in {db_path}")


def cmd_list(args, cfg):
    db_path = cfg["database"]["path"]
    init_db(db_path)
    rows = list_activities(db_path)

    if not rows:
        print("No activities in database. Run 'sync' first.")
        return

    header = f"{'ID':<14} {'Date':<12} {'Type':<18} {'Distance':>9} {'Elev':>8}  Name"
    print(header)
    print("─" * len(header))
    for r in rows:
        dist  = f"{(r['distance'] or 0) / 1000:.1f} km"
        elev  = f"{r['total_elevation_gain'] or 0:.0f} m"
        date  = (r["start_date"] or "")[:10]
        stype = r["sport_type"] or ""
        print(f"{r['id']:<14} {date:<12} {stype:<18} {dist:>9} {elev:>8}  {r['name']}")


def cmd_render(args, cfg):
    db_path = cfg["database"]["path"]
    init_db(db_path)

    if args.activity_id:
        row = get_activity(db_path, args.activity_id)
        if not row:
            print(f"Activity {args.activity_id} not found. Run 'sync' first.")
            sys.exit(1)
        rows = [row]
    else:
        all_rows = list_activities(db_path)
        if not all_rows:
            print("No activities in database. Run 'sync' first.")
            sys.exit(1)
        rows = [all_rows[0]]

    out_dir = cfg["map"].get("output_dir", "output")
    os.makedirs(out_dir, exist_ok=True)

    for row in rows:
        points = get_points(row)
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
    init_db(db_path)
    row = get_activity(db_path, args.activity_id)
    if not row:
        print(f"Activity {args.activity_id} not found.")
        sys.exit(1)
    stream  = get_stream(row)
    out_dir = cfg["map"].get("output_dir", "output")
    print(f"Generating charts for [{row['id']}] {row['name']}…")
    paths = generate_charts(row["id"], stream, cfg, out_dir, db_path=db_path)
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
        name         = activity.get("name", "Activity"),
        distance_km  = (activity.get("distance") or 0) / 1000,
        elevation_m  = activity.get("total_elevation_gain") or 0,
        moving_time  = fmt_time(activity.get("moving_time")),
        average_speed= (activity.get("average_speed") or 0) * 3.6,
        date         = (activity.get("start_date") or "")[:10],
        sport_type   = activity.get("sport_type") or "",
    )


def cmd_post(args, cfg):
    db_path = cfg["database"]["path"]
    init_db(db_path)

    row = get_activity(db_path, args.activity_id)
    if not row:
        print(f"Activity {args.activity_id} not found. Run 'sync' first.")
        sys.exit(1)

    # Render image if it doesn't exist yet
    out_dir  = cfg["map"].get("output_dir", "output")
    img_path = os.path.join(out_dir, f"{args.activity_id}.png")
    if not os.path.exists(img_path) or args.rerender:
        points = get_points(row)
        if not points:
            print("No GPS data for this activity.")
            sys.exit(1)
        os.makedirs(out_dir, exist_ok=True)
        print(f"Rendering map…")
        img = render_activity_map(points, dict(row), cfg)
        if img is None:
            print("Render failed.")
            sys.exit(1)
        img.save(img_path)
        print(f"  Saved → {img_path}")

    # Build post text
    masto_cfg = cfg.get("mastodon", {})
    template  = masto_cfg.get("post_template", "{name}\n#cycling")
    text      = _build_post_text(dict(row), template)

    print(f"\nPost preview:\n{'─' * 40}")
    print(text)
    print('─' * 40)
    print(f"Image: {img_path}")

    # Generate charts
    print("Generating charts…")
    stream      = get_stream(row)
    chart_paths = generate_charts(args.activity_id, stream, cfg, out_dir, db_path=db_path)
    all_images  = [img_path] + chart_paths  # map first, then charts
    # Mastodon allows max 4 attachments
    all_images  = all_images[:4]

    print(f"\nAttachments ({len(all_images)}):")
    for p in all_images:
        print(f"  {p}")

    if not args.dry_run:
        confirm = input("\nPost to Mastodon? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

        try:
            client = MastodonClient.from_env(masto_cfg.get("instance", "https://mastodon.social"))
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)

        # Upload all images, then post once with all media IDs
        media_ids = []
        for path in all_images:
            print(f"  Uploading {os.path.basename(path)}…")
            media_ids.append(client.upload_image(path))

        from mastodon_client import MastodonClient as MC
        resp = client._session.post(
            f"{client._base}/api/v1/statuses",
            json={
                "status":     text,
                "media_ids":  media_ids,
                "visibility": masto_cfg.get("visibility", "public"),
            },
        )
        resp.raise_for_status()
        print(f"\nPosted! {resp.json().get('url', '')}")
    else:
        print("\n(dry run — nothing posted)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Bikeodon – Strava activity map renderer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config.yaml", metavar="FILE",
                        help="Path to config file (default: config.yaml)")
    sub = parser.add_subparsers(dest="command")

    p_sync = sub.add_parser("sync", help="Fetch recent activities from Strava")
    p_sync.add_argument("--count", type=int, default=10,
                        help="Number of activities to fetch (default: 10)")

    sub.add_parser("list", help="List activities stored in the database")

    p_render = sub.add_parser("render", help="Render a map image for an activity")
    p_render.add_argument("activity_id", type=int, nargs="?",
                          help="Strava activity ID (default: most recent)")

    p_charts = sub.add_parser("charts", help="Generate HR/power charts for an activity")
    p_charts.add_argument("activity_id", type=int)

    p_post = sub.add_parser("post", help="Render and post an activity to Mastodon")
    p_post.add_argument("activity_id", type=int,
                        help="Strava activity ID to post")
    p_post.add_argument("--dry-run", action="store_true",
                        help="Preview the post without publishing")
    p_post.add_argument("--rerender", action="store_true",
                        help="Re-render the image even if it already exists")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cfg = load_config(args.config)
    {"sync": cmd_sync, "list": cmd_list, "render": cmd_render,
     "charts": cmd_charts, "post": cmd_post}[args.command](args, cfg)


if __name__ == "__main__":
    main()
