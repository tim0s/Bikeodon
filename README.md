<p align="center">
  <img src="static/logo.png" alt="Bikeodon" width="400">
</p>

# Bikeodon

Automatically post your Strava activities to Mastodon — with a rendered route map, stats overlay, and heart rate / power zone charts.

## What it does

For each new activity, Bikeodon:

1. Fetches the GPS track and sensor data from Strava via OAuth
2. Renders a map image by stitching OpenStreetMap tiles with the route drawn on top
3. Overlays a stats bar (distance, elevation gain, or any fields you choose)
4. Generates heart rate and power zone charts when the data is available
5. Posts the map and charts as a single Mastodon status with a configurable text template

Training parameters (max HR, FTP) are inferred automatically from recorded history when not set explicitly.

## Example output

Up to four images are attached: the route map plus HR and power charts.

```
A nice loop through the hills 🚴
📍 87.3 km  🏔 1240 m  ⏱ 2h 58m

#cycling #strava

Connect Strava to the fediverse using Bikeodon [tim0s.github.io/Bikeodon]
```

## Requirements

- Python 3.11+
- A Strava API app (free, registered at strava.com/settings/api)
- A Mastodon account and API token

## Installation

```bash
git clone https://github.com/tim0s/Bikeodon.git
cd Bikeodon
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Server configuration

`config.yaml` holds only server-level paths. Everything else is configured per-user via the web UI.

```yaml
database:
  path: "bikeodon.db"

map:
  output_dir: "output"
  tiles:
    cache_dir: ".tile_cache"

daemon:
  interval_minutes: 15
```

Create a `.env` file for secrets (never commit this):

```
STRAVA_CLIENT_ID=<your Strava app client ID>
STRAVA_CLIENT_SECRET=<your Strava app client secret>
FLASK_SECRET_KEY=<any long random string>
```

**Registering a Strava API app:**
1. Go to [strava.com/settings/api](https://www.strava.com/settings/api)
2. Create an application — set the Authorization Callback Domain to your server's hostname (`localhost` for local dev)
3. Copy the Client ID and Client Secret into `.env`

By default Strava limits new apps to the app owner's account only. To allow other athletes to connect, apply for extended API access on the same page.

## Running the web UI

```bash
flask --app app run
```

Open [http://localhost:5000](http://localhost:5000). Register an account, then go to **Settings → Connect Strava** to authorise. New activities are picked up automatically by the daemon.

## Running the daemon

The daemon polls Strava for every connected user on a configurable interval and posts any new activities to Mastodon automatically:

```bash
python main.py daemon
```

Run this as a background service (systemd, tmux, screen) on your server.

## Per-user settings

All visual and behavioural options are configured in the web UI under **Settings**:

| Section | What you can configure |
|---|---|
| **Strava** | OAuth connect / disconnect |
| **Mastodon** | Instance URL, token, handle, visibility, post template |
| **Stats bar** | Which fields to show (distance, elevation, HR, power, …) and overlay appearance |
| **Map** | Image size, zoom, tile provider URL, route colour/width, start/end markers, padding |
| **Charts** | HR and power chart enable/disable, explicit max HR and FTP (or leave blank to infer) |
| **Zones** | HR and power zone names, thresholds, and colours |

**Post template variables:** `{name}`, `{distance_km}`, `{elevation_m}`, `{moving_time}`, `{average_speed}`, `{date}`, `{sport_type}`

## Training parameter inference

When max HR or FTP is left blank, Bikeodon estimates them from the user's synced history:

- **Max HR** — 99th percentile of all recorded heart rate samples (avoids inflating from sensor spikes)
- **FTP** — 95% of the best 20-minute average power across all activities (standard 20-min test protocol)

## Tile providers

The default is OpenStreetMap (no API key needed). The tile URL is a per-user setting, so you can point different users at different providers:

| Provider | Example URL |
|---|---|
| OpenStreetMap | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` |
| Stadia Smooth Light | `https://tiles.stadiamaps.com/tiles/alidade_smooth/{z}/{x}/{y}.png?api_key=KEY` |
| Stadia Smooth Dark | `https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}.png?api_key=KEY` |
| Stadia Outdoors | `https://tiles.stadiamaps.com/tiles/outdoors/{z}/{x}/{y}.png?api_key=KEY` |
| CartoDB Positron | `https://cartodb-basemaps-a.global.ssl.fastly.net/light_all/{z}/{x}/{y}.png` |
| CartoDB Dark Matter | `https://cartodb-basemaps-a.global.ssl.fastly.net/dark_all/{z}/{x}/{y}.png` |

## CLI reference

The CLI is primarily for operators. End users interact through the web UI.

```bash
python main.py sync              # fetch new activities for all connected users
python main.py list              # list activities in the database
python main.py render 12345678   # render a map image for a specific activity
python main.py charts 12345678   # generate HR/power charts
python main.py post 12345678     # render and post an activity manually
python main.py post 12345678 --dry-run   # preview without posting
python main.py daemon            # start the polling daemon
python main.py config list       # show all settings for user 1
python main.py config set <area> <key> <value>
```

## Project structure

```
app.py             Flask web frontend (auth, settings UI, Strava OAuth)
main.py            CLI (sync, daemon, render, post, config)
strava.py          Strava OAuth2 client + activity/stream fetching
database.py        SQLite schema, per-user settings and zones, auth helpers
map_renderer.py    OSM tile fetch, route render, stats overlay
charts.py          Matplotlib HR and power zone charts
inference.py       Infer max HR and FTP from recorded data
mastodon_client.py Mastodon media upload and status post
config.yaml        Server-level paths only
templates/         Jinja2 HTML templates
static/            CSS and logo assets
```

## License

BSD 3-Clause. See [LICENSE](LICENSE).
