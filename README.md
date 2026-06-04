<p align="center">
  <img src="static/logo.png" alt="Bikeodon" width="400">
</p>

**Bikeodon** gets your bike rides onto Mastodon — with a route map, training stats, and HR/power charts.

**→ [bikeodon.org](https://bikeodon.org)**

---

## What you get

For every activity you post, Bikeodon generates:

- **A route map** — your GPS track rendered on OpenStreetMap, with a stats bar showing distance, elevation, time, and more
- **Heart rate zones chart** — time spent in each zone, coloured by your personal zone thresholds
- **Power zones chart** — same for power, if you rode with a power meter

Everything is customisable: map style, colours, which stats to show, what the post says.

## Getting your activities in

Upload **GPX, TCX, or FIT files** directly — works with Zwift, TrainerRoad, Wahoo ELEMNT, Garmin Connect, Polar, and any other platform that exports standard activity files.

If you use **Strava**, you can connect your account and new activities will appear automatically via webhook — no manual steps needed.

## How to post

1. Connect your Mastodon account in Settings
2. Bring in activities via file upload or Strava sync
3. Click **Post to Mastodon** on any activity

## Self-hosting

Bikeodon is open source and designed to be self-hosted. See [DEPLOY.md](DEPLOY.md) for full instructions (Oracle Cloud free tier, nginx, gunicorn — all covered).

## License

BSD 3-Clause. See [LICENSE](LICENSE).
