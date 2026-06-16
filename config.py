import os
import yaml
from dotenv import load_dotenv

load_dotenv()

with open(os.environ.get("BIKEODON_CONFIG", "config.yaml")) as f:
    _base_cfg = yaml.safe_load(f)

DB_PATH              = _base_cfg["database"]["path"]
OUTPUT_DIR           = _base_cfg["map"].get("output_dir", "output")
STRAVA_CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")
SYNC_COOLDOWN_SECS   = 15 * 60
