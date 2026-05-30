<p align="center">
  <img src="static/logo.png" alt="Bikeodon" width="400">
</p>

# Bikeodon

Automatically post your Strava activities to Mastodon — with a rendered route map, stats overlay, and heart rate / power zone charts.

## What it does

For each activity you choose to share, Bikeodon:

1. Downloads the GPS track and sensor data from Strava
2. Renders a map image by stitching OpenStreetMap tiles with the route drawn on top
3. Overlays a stats bar (distance, elevation gain, or any fields you configure)
4. Generates heart rate and power zone charts when the data is available
5. Posts the map and charts as a single Mastodon status with a configurable text template

Training parameters (max HR, FTP) are inferred automatically from your recorded history if you haven't set them explicitly.

## Example output

The post includes up to four images: the route map plus HR and power charts.

```
A nice loop through the hills 🚴
📍 87.3 km  🏔 1240 m  ⏱ 2h 58m

#cycling #strava

Connect Strava to the fediverse using Bikeodon [tim0s.github.io/Bikeodon]
```

## Requirements

- Python 3.11+
- A Strava account (authentication via browser session cookie — no API key needed)
- A Mastodon account and API token

## Installation

```bash
git clone https://github.com/tim0s/Bikeodon.git
cd Bikeodon
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

### Credentials

Create a `.env` file in the project root (never commit this):

```
STRAVA_SESSION=<your _strava4_session cookie value>
MASTODON_TOKEN=<your Mastodon API token>
```

**Getting your Strava session cookie:**
1. Log in to strava.com in your browser
2. Open DevTools → Application → Cookies → `https://www.strava.com`
3. Copy the value of `_strava4_session`

**Getting a Mastodon API token:**
1. Go to your instance's settings → Development → New Application
2. Grant `write:media` and `write:statuses` scopes
3. Copy the access token

### config.yaml

All visual and behavioural options live in `config.yaml`. Key sections:

```yaml
database:
  path: "bikeodon.db"       # local SQLite database

map:
  output_dir: "output"
  width: 1200               # pixels (1200×675 = 16:9, fills Mastodon feed)
  height: 675
  zoom_offset: -1           # negative = zoom out for a wider view
  tiles:
    url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    # Stadia, CartoDB, and Stamen alternatives are listed in the file

user:
  stats:
    fields:                 # choose any subset, order matters
      - distance
      - elevation_gain
      - moving_time
      - average_speed
      - average_heartrate
      - average_watts

charts:
  heart_rate:
    enabled: true
    max_hr: null            # set explicitly, or leave null to infer from data
  power:
    enabled: true
    ftp: null               # set explicitly, or leave null to infer from data

mastodon:
  instance: "https://mastodon.social"
  visibility: "public"
  post_template: |
    {name} 🚴
    📍 {distance_km:.1f} km  🏔 {elevation_m:.0f} m  ⏱ {moving_time}

    #cycling #strava
```

Available template variables: `{name}`, `{distance_km}`, `{elevation_m}`, `{moving_time}`, `{average_speed}`, `{date}`, `{sport_type}`.

## Usage

```bash
# Sync the 10 most recent activities from Strava
python main.py sync

# Sync more
python main.py sync --count 50

# List what's in the local database
python main.py list

# Render the map for the most recent activity
python main.py render

# Render a specific activity
python main.py render 12345678901

# Generate HR/power charts only
python main.py charts 12345678901

# Preview a post (no upload)
python main.py post 12345678901 --dry-run

# Render and post to Mastodon
python main.py post 12345678901
```

## How training parameters are inferred

When `max_hr` or `ftp` is `null` in the config, Bikeodon estimates them from your synced history:

- **Max HR** — 99th percentile of all recorded heart rate samples (avoids inflating the estimate from sensor spikes)
- **FTP** — 95% of the best 20-minute average power found across all activities with power data (standard 20-min FTP test protocol)

The inferred values are printed when charts are generated so you can review them.

## Tile providers

The default is OpenStreetMap, which requires no API key. Alternatives are listed as comments in `config.yaml`:

| Provider | Style |
|---|---|
| OpenStreetMap | Standard |
| Stadia Alidade Smooth | Clean light / dark |
| Stadia Outdoors | Good for cycling routes |
| Stamen Terrain | Topographic |
| CartoDB Positron | Minimal light |
| CartoDB Dark Matter | Minimal dark |

Please follow each provider's usage policy and rate limits.

## Project structure

```
main.py            CLI entry point (sync, list, render, charts, post)
strava.py          Strava session cookie auth + GPX download/parse
database.py        SQLite schema and query helpers
map_renderer.py    OSM tile fetch, route render, stats overlay
charts.py          Matplotlib HR and power zone charts
inference.py       Infer max HR and FTP from recorded data
mastodon_client.py Mastodon media upload and status post
config.yaml        All user-facing settings
```

## License

BSD 3-Clause. See [LICENSE](LICENSE).
