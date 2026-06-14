"""
Take cross-browser, multi-viewport screenshots against a seeded real database.

Usage:
    python tools/take_screenshots.py
"""

import os, sys, shutil, socket, threading, time, urllib.request, urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

SEED_DB   = Path(__file__).parent.parent / "screenshots_seed.db"
OUT_DIR   = Path(__file__).parent.parent / "screenshots"
USERNAME  = "tim0s42"
PASSWORD  = "screenshot"

VIEWPORTS = {
    "mobile_390":   {"width": 390,  "height": 844},
    "tablet_768":   {"width": 768,  "height": 1024},
    "desktop_1280": {"width": 1280, "height": 800},
}

PAGES = [
    ("/login",              False),
    ("/",                   True),
    ("/activity/18853021065", True),
    ("/me",                 True),
    ("/me?tab=fitness",     True),
    ("/me?tab=zones",       True),
    ("/me?tab=power",       True),
    ("/me?tab=followers",   True),
    ("/feed",               True),
    ("/settings",           True),
]


def _slugify(path):
    return path.lstrip("/").replace("/", "_").replace("?", "_").replace("=", "-") or "root"


def _start_server(db_path):
    import tempfile, yaml
    tmp = tempfile.mkdtemp()
    working_db = os.path.join(tmp, "bikeodon.db")
    shutil.copy(db_path, working_db)

    cfg = {
        "database": {"path": working_db},
        "daemon":   {"interval_minutes": 99999},
        "map":      {"output_dir": str(Path(db_path).parent / "output"), "tiles": {}},
    }
    cfg_path = os.path.join(tmp, "config.yaml")
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)

    os.environ["BIKEODON_CONFIG"] = cfg_path
    os.environ["FLASK_SECRET_KEY"] = "screenshot-secret"

    import importlib
    import app as app_module
    importlib.reload(app_module)
    flask_app = app_module.app
    flask_app.config["TESTING"] = False

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    threading.Thread(
        target=lambda: flask_app.run(host="127.0.0.1", port=port, use_reloader=False),
        daemon=True,
    ).start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            urllib.request.urlopen(base + "/login", timeout=1)
            return base
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("Server didn't start")


def _login(page, base, username, password):
    page.goto(f"{base}/login")
    page.fill("input[name='username']", username)
    page.fill("input[name='password']", password)
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")


def run(browsers=("chromium", "webkit")):
    base = _start_server(SEED_DB)
    print(f"Server up at {base}")

    from playwright.sync_api import sync_playwright
    total = 0
    with sync_playwright() as p:
        for browser_name in browsers:
            b = getattr(p, browser_name).launch(headless=True)
            for vp_name, vp in VIEWPORTS.items():
                print(f"  {browser_name} / {vp_name} ...", end=" ", flush=True)
                ctx  = b.new_context(viewport=vp)
                page = ctx.new_page()
                authed = False
                for path, needs_auth in PAGES:
                    if needs_auth and not authed:
                        _login(page, base, USERNAME, PASSWORD)
                        authed = True
                    page.goto(f"{base}{path}")
                    page.wait_for_load_state("networkidle")
                    dest = OUT_DIR / browser_name / vp_name
                    dest.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(dest / f"{_slugify(path)}.png"), full_page=True)
                    total += 1
                ctx.close()
                print("done")
            b.close()

    print(f"\n{total} screenshots saved to {OUT_DIR}/")


if __name__ == "__main__":
    run()
