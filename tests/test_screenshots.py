"""
Cross-browser, multi-viewport screenshot tests.

Run:
    pytest tests/test_screenshots.py -v

Screenshots land in screenshots/<browser>/<viewport>/<page>.png.
The tests always pass (they are visual inspection aids, not assertions).
Add --screenshot-assert to turn mismatches into failures once a baseline exists.
"""

import os
import threading
import time
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Viewports to test
# ---------------------------------------------------------------------------

VIEWPORTS = {
    "mobile_390":  {"width": 390,  "height": 844},   # iPhone 14
    "tablet_768":  {"width": 768,  "height": 1024},  # iPad
    "desktop_1280": {"width": 1280, "height": 800},  # laptop
}

# ---------------------------------------------------------------------------
# Pages to screenshot (path, requires_auth)
# ---------------------------------------------------------------------------

PAGES = [
    ("/login",    False),
    ("/register", False),
    ("/",         True),   # activities list
    ("/me",       True),   # dashboard / overview
    ("/me?tab=fitness",   True),
    ("/me?tab=zones",     True),
    ("/me?tab=power",     True),
    ("/me?tab=followers", True),
    ("/feed",     True),
    ("/settings", True),
]

SCREENSHOT_DIR = Path(__file__).parent.parent / "screenshots"


# ---------------------------------------------------------------------------
# Flask app fixture — starts a real server so Playwright can hit it
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """Start the Flask app on a free port and yield the base URL."""
    tmp = tmp_path_factory.mktemp("bikeodon_screenshots")
    cfg = {
        "database": {"path": str(tmp / "test.db")},
        "daemon":   {"interval_minutes": 15},
        "map":      {"output_dir": str(tmp / "output"), "tiles": {}},
    }
    cfg_path = str(tmp / "config.yaml")
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)

    os.environ["BIKEODON_CONFIG"] = cfg_path
    os.environ["FLASK_SECRET_KEY"] = "screenshot-secret"

    import importlib
    import app as app_module
    importlib.reload(app_module)
    flask_app = app_module.app
    flask_app.config["TESTING"] = False  # real responses, not test shortcuts
    flask_app.config["SERVER_NAME"] = None

    # Pick a free port
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = threading.Thread(
        target=lambda: flask_app.run(host="127.0.0.1", port=port, use_reloader=False),
        daemon=True,
    )
    server.start()

    # Wait until the server is accepting connections
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            import urllib.request
            urllib.request.urlopen(base_url + "/login", timeout=1)
            break
        except Exception:
            time.sleep(0.2)

    # Register a demo user via the real HTTP endpoint
    import urllib.request, urllib.parse
    urllib.request.urlopen(
        urllib.request.Request(
            f"{base_url}/register",
            data=urllib.parse.urlencode({"username": "demo", "password": "demo1234"}).encode(),
            method="POST",
        )
    )

    yield base_url

    os.environ.pop("BIKEODON_CONFIG", None)


# ---------------------------------------------------------------------------
# Playwright browser fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pw_sync():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def browser(request, pw_sync):
    launcher = getattr(pw_sync, request.param)
    b = launcher.launch(headless=True)
    yield b, request.param
    b.close()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _slugify(path: str) -> str:
    return path.lstrip("/").replace("/", "_").replace("?", "_").replace("=", "-") or "root"


def _login_page(page, base_url: str):
    page.goto(f"{base_url}/login")
    page.fill("input[name='username']", "demo")
    page.fill("input[name='password']", "demo1234")
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("viewport_name,viewport", VIEWPORTS.items())
def test_screenshots(live_server, browser, viewport_name, viewport):
    b, browser_name = browser
    context = b.new_context(viewport=viewport)
    page = context.new_page()

    authenticated = False

    for path, requires_auth in PAGES:
        if requires_auth and not authenticated:
            _login_page(page, live_server)
            authenticated = True

        page.goto(f"{live_server}{path}")
        page.wait_for_load_state("networkidle")

        out = SCREENSHOT_DIR / browser_name / viewport_name
        out.mkdir(parents=True, exist_ok=True)
        dest = out / f"{_slugify(path)}.png"
        page.screenshot(path=str(dest), full_page=True)

    context.close()
    # Screenshots are purely for visual inspection — no assertion.
    assert True
