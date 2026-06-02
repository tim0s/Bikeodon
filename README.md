<p align="center">
  <img src="static/logo.png" alt="Bikeodon" width="400">
</p>

**Bikeodon** connects your Strava account to Mastodon — automatically sharing your rides with a beautiful route map, stats, and training charts.

**→ [bikeodon.org](https://bikeodon.org)**

---

## What you get

Every time you complete an activity, Bikeodon posts it to your Mastodon account with:

- **A route map** — your GPS track rendered on OpenStreetMap, with a configurable stats bar showing distance, elevation, time, and more
- **Heart rate zones chart** — time spent in each zone, coloured by your personal zone thresholds
- **Power zones chart** — same for power, if your bike has a power meter

Everything is customisable: map style, colours, which stats to show, what the post says.

## How it works

1. Connect your Strava account in Settings
2. Connect your Mastodon account in Settings
3. After each activity, open Bikeodon and click **Post to Mastodon**

That's it. The app runs as a web service and syncs your activities automatically in the background.

## Self-hosting

Bikeodon is open source and designed to be self-hosted. See [DEPLOY.md](DEPLOY.md) for instructions to run it on a server (Oracle Cloud free tier, nginx, systemd — all covered).

## License

BSD 3-Clause. See [LICENSE](LICENSE).
