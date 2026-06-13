<p align="center">
  <img src="static/logo.png" alt="Bikeodon" width="400">
</p>

**Bikeodon** is a self-hosted cycling training dashboard and Fediverse client. It pulls your rides from Strava (or from uploaded files), generates route maps and training charts, and lets you share activities across the Fediverse — either via Mastodon or as a native ActivityPub actor that other people can follow directly.

**→ [bikeodon.org](https://bikeodon.org)**

---

## Screenshots

<p align="center">
  <img src="docs/screenshots/activity-list.png" width="800" alt="Activity list">
  <br><em>All your rides in one place, with quick stats and Strava/Mastodon links inline</em>
</p>

<p align="center">
  <img src="docs/screenshots/activity-detail.png" width="800" alt="Activity detail">
  <br><em>Activity detail — route map and HR chart; post to Mastodon or to your followers in one click</em>
</p>

<p align="center">
  <img src="docs/screenshots/dashboard-overview.png" width="800" alt="Training dashboard">
  <br><em>Training dashboard — fitness (CTL), fatigue (ATL), form (TSB), Critical Power, W', and weekly load</em>
</p>

<p align="center">
  <img src="docs/screenshots/dashboard-power.png" width="800" alt="Power curve">
  <br><em>Mean maximal power curve with Critical Power model overlay</em>
</p>

<p align="center">
  <img src="docs/screenshots/settings.png" width="800" alt="Settings">
  <br><em>Auto-detects FTP and max HR from your data — or set them manually</em>
</p>

---

## What you get

**For every activity**, Bikeodon generates:

- **A route map** — GPS track rendered on OpenStreetMap tiles, with a stats overlay
- **Heart rate zones chart** — time in each zone, coloured by your thresholds
- **Power zones chart** — same for power, if you rode with a power meter

**A private training dashboard** with:

- **Performance Management Chart** — Fitness (CTL), Fatigue (ATL), Form (TSB) over 6 months
- **Mean maximal power curve** — all-time and last 90 days, with Critical Power model overlay
- **W' balance** — per-activity chart of anaerobic reserve depletion and recovery (Skiba 2012)
- **Zone distribution** — total time in each HR and power zone across all activities
- **Critical Power and W'** — fitted from your own MMP curve, no lab test needed

**Fediverse federation** — Bikeodon is a full ActivityPub server:

- Every user gets an actor (`@you@bikeodon.org`) that anyone on Mastodon, Akkoma, Misskey, etc. can follow
- Post an activity to your followers with one click — the map and charts are attached as images
- Posts include structured hashtags (sport-specific + `#strava #bikeodon`) that federate correctly
- Follow other people from the Fediverse and see their posts in your home feed
- Profile updates propagate automatically to your followers
- NodeInfo 2.0 for instance discovery

**Sharing options:**

- Post to **Mastodon** via the Mastodon API (your existing account on any instance)
- Post as **ActivityPub** (your Bikeodon actor — followers receive it natively on the Fediverse)
- **Schedule** a post for later, or re-post after changing settings

Everything is customisable: map tiles, colours, stats bar fields, post text template, zone definitions.

## Getting your activities in

Upload **GPX, TCX, or FIT files** directly — works with Zwift, TrainerRoad, Wahoo ELEMNT, Garmin Connect, Polar, and any platform that exports standard activity files.

Connect **Strava** and new activities appear automatically via webhook — no manual steps needed. A manual sync button is also available in Settings.

## How to post

### To Mastodon
1. Connect your Mastodon account in Settings
2. Click **Post to Mastodon** on any activity page

### To your Fediverse followers
1. Your ActivityPub actor is created automatically — share `@you@bikeodon.org` so people can follow you
2. Click **Post in my Feed** on any activity page to fan out to all your followers

### Following others
Go to **You → Following** and enter a Fediverse handle (`@alice@mastodon.social`) to follow them. Their posts appear in your **Home Feed**.

## Self-hosting

Bikeodon is open source and designed to be self-hosted. See [DEPLOY.md](DEPLOY.md) for full instructions (Oracle Cloud free tier, nginx, gunicorn — all covered).

## License

BSD 3-Clause. See [LICENSE](LICENSE).
