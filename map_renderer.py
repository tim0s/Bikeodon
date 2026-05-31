"""
Map renderer: fetches OSM tiles, stitches them, and draws the route overlay.
All visual knobs are driven by the 'map' section of config.yaml.
"""

import hashlib
import io
import math
import os

import requests
from PIL import Image, ImageDraw

TILE_SIZE = 256


# ---------------------------------------------------------------------------
# Coordinate math
# ---------------------------------------------------------------------------

def _latlon_to_global_px(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Convert a lat/lon to floating-point global pixel coordinates at zoom."""
    n = 2.0 ** zoom
    x = (lon + 180.0) / 360.0 * n * TILE_SIZE
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n * TILE_SIZE
    return x, y


def _deg2tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """Return the tile (tx, ty) that contains the given lat/lon at zoom."""
    gx, gy = _latlon_to_global_px(lat, lon, zoom)
    return int(gx // TILE_SIZE), int(gy // TILE_SIZE)


def _select_zoom(min_lat, min_lon, max_lat, max_lon,
                 route_area_w: float, route_area_h: float,
                 max_tiles: int, zoom_offset: int = 0) -> int:
    """
    Pick the highest zoom where the route bbox fits inside the route area
    (output minus padding) and tile count stays under max_tiles.
    zoom_offset nudges the result up or down.
    """
    best = 1
    for zoom in range(17, 1, -1):
        gx0, gy0 = _latlon_to_global_px(max_lat, min_lon, zoom)
        gx1, gy1 = _latlon_to_global_px(min_lat, max_lon, zoom)
        route_px_w = abs(gx1 - gx0)
        route_px_h = abs(gy1 - gy0)

        tx0, ty0 = _deg2tile(max_lat, min_lon, zoom)
        tx1, ty1 = _deg2tile(min_lat, max_lon, zoom)
        nx = abs(tx1 - tx0) + 5
        ny = abs(ty1 - ty0) + 5

        if route_px_w < route_area_w and route_px_h < route_area_h and nx * ny <= max_tiles:
            best = zoom
            break

    return max(1, best + zoom_offset)


# ---------------------------------------------------------------------------
# Tile fetching
# ---------------------------------------------------------------------------

def _fetch_tile(url_template: str, z: int, x: int, y: int,
                user_agent: str, cache_dir: str) -> Image.Image:
    os.makedirs(cache_dir, exist_ok=True)
    key = hashlib.md5(f"{url_template}/{z}/{x}/{y}".encode()).hexdigest()
    cache_path = os.path.join(cache_dir, f"{key}.png")

    if os.path.exists(cache_path):
        return Image.open(cache_path).convert("RGBA")

    url = url_template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
    resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=10)
    resp.raise_for_status()

    img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
    img.save(cache_path)
    return img


def _stitch_tiles(min_lat, min_lon, max_lat, max_lon, zoom, cfg) -> tuple[Image.Image, tuple[int, int]]:
    """
    Fetch all tiles covering the bounding box (plus 1-tile buffer) and stitch
    them into a single RGBA image. Returns (image, (origin_gx, origin_gy)) where
    origin is the top-left corner of the canvas in global pixel space.
    """
    tile_cfg = cfg["map"]["tiles"]
    url = tile_cfg["url"]
    user_agent = tile_cfg.get("user_agent", "Bikeodon/0.1")
    cache_dir = tile_cfg.get("cache_dir", ".tile_cache")

    tx0, ty0 = _deg2tile(max_lat, min_lon, zoom)
    tx1, ty1 = _deg2tile(min_lat, max_lon, zoom)

    # normalise + buffer (generous so padding crop never exceeds canvas)
    tx0, tx1 = min(tx0, tx1) - 3, max(tx0, tx1) + 3
    ty0, ty1 = min(ty0, ty1) - 3, max(ty0, ty1) + 3

    nx = tx1 - tx0 + 1
    ny = ty1 - ty0 + 1
    canvas = Image.new("RGBA", (nx * TILE_SIZE, ny * TILE_SIZE), (200, 200, 200, 255))

    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            try:
                tile = _fetch_tile(url, zoom, tx, ty, user_agent, cache_dir)
                px = (tx - tx0) * TILE_SIZE
                py = (ty - ty0) * TILE_SIZE
                canvas.paste(tile, (px, py))
            except Exception as e:
                print(f"    Warning: tile {zoom}/{tx}/{ty} failed – {e}")

    origin = (tx0 * TILE_SIZE, ty0 * TILE_SIZE)
    return canvas, origin


# ---------------------------------------------------------------------------
# Route + marker drawing
# ---------------------------------------------------------------------------

def _parse_color(color, opacity: float = 1.0) -> tuple[int, int, int, int]:
    if isinstance(color, str):
        c = color.lstrip("#")
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    elif isinstance(color, (list, tuple)):
        r, g, b = int(color[0]), int(color[1]), int(color[2])
    else:
        r, g, b = 255, 255, 255
    return r, g, b, int(opacity * 255)


def _draw_route(img: Image.Image, origin: tuple[int, int],
                points: list, zoom: int, cfg: dict) -> Image.Image:
    """
    Draw the route polyline onto a transparent overlay, then composite.
    Renders at antialias_scale× resolution for smooth edges.
    """
    route_cfg = cfg["map"]["route"]
    scale = int(route_cfg.get("antialias_scale", 2))
    w, h = img.size

    overlay_big = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay_big)

    def to_px(lat, lon):
        gx, gy = _latlon_to_global_px(lat, lon, zoom)
        return ((gx - origin[0]) * scale, (gy - origin[1]) * scale)

    px_points = [to_px(lat, lon) for lat, lon in points]

    route_color = _parse_color(route_cfg.get("color", "#FC4C02"),
                                route_cfg.get("opacity", 0.9))
    route_w = route_cfg.get("width", 4) * scale
    outline_color_hex = route_cfg.get("outline_color")
    outline_w = route_cfg.get("outline_width", 0) * scale

    def draw_polyline(color, width):
        half_r = width / 2
        # Round joins: circle at each vertex
        for pt in px_points:
            draw.ellipse([pt[0] - half_r, pt[1] - half_r,
                          pt[0] + half_r, pt[1] + half_r], fill=color)
        # Segments
        for i in range(len(px_points) - 1):
            draw.line([px_points[i], px_points[i + 1]], fill=color, width=int(width))

    if outline_color_hex and outline_w > 0:
        outline_color = _parse_color(outline_color_hex, route_cfg.get("opacity", 0.9))
        draw_polyline(outline_color, route_w + outline_w * 2)

    draw_polyline(route_color, route_w)

    overlay = overlay_big.resize((w, h), Image.LANCZOS)
    return Image.alpha_composite(img, overlay)


def _draw_markers(img: Image.Image, origin: tuple[int, int],
                  points: list, zoom: int, cfg: dict) -> Image.Image:
    if not points:
        return img

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    def to_px(lat, lon):
        gx, gy = _latlon_to_global_px(lat, lon, zoom)
        return (gx - origin[0], gy - origin[1])

    def draw_dot(latlon, marker_cfg):
        if not marker_cfg.get("enabled", True):
            return
        px = to_px(*latlon)
        r = marker_cfg.get("radius", 8)
        color = _parse_color(marker_cfg.get("color", "#ffffff"))
        outline_hex = marker_cfg.get("outline_color")
        outline_w = marker_cfg.get("outline_width", 2)
        if outline_hex and outline_w > 0:
            outline = _parse_color(outline_hex)
            rr = r + outline_w
            draw.ellipse([px[0] - rr, px[1] - rr, px[0] + rr, px[1] + rr], fill=outline)
        draw.ellipse([px[0] - r, px[1] - r, px[0] + r, px[1] + r], fill=color)

    draw_dot(points[0], cfg["map"].get("start_marker", {}))
    draw_dot(points[-1], cfg["map"].get("end_marker", {}))

    return Image.alpha_composite(img, overlay)


# ---------------------------------------------------------------------------
# Crop / scale
# ---------------------------------------------------------------------------

def _crop_to_output(img: Image.Image, origin: tuple[int, int],
                    min_lat, min_lon, max_lat, max_lon, zoom: int,
                    out_w: int, out_h: int,
                    pad_left_px: float, pad_right_px: float,
                    pad_top_px: float, pad_bottom_px: float) -> Image.Image:
    """
    Crop the tile canvas to exactly (out_w × out_h).
    The route is centered within the area left after subtracting fixed padding.
    No rescaling — tile pixels are never stretched.
    """
    gx0, gy0 = _latlon_to_global_px(max_lat, min_lon, zoom)  # top-left of route bbox
    gx1, gy1 = _latlon_to_global_px(min_lat, max_lon, zoom)  # bottom-right

    cx0 = gx0 - origin[0]
    cy0 = gy0 - origin[1]
    route_px_w = gx1 - gx0
    route_px_h = gy1 - gy0

    route_area_w = out_w - pad_left_px - pad_right_px
    route_area_h = out_h - pad_top_px  - pad_bottom_px

    # Center the route within the route area, then add the left/top padding
    center_offset_x = (route_area_w - route_px_w) / 2
    center_offset_y = (route_area_h - route_px_h) / 2

    left   = int(round(cx0 - pad_left_px - center_offset_x))
    top    = int(round(cy0 - pad_top_px  - center_offset_y))
    right  = left + out_w
    bottom = top  + out_h

    if left < 0 or top < 0 or right > img.width or bottom > img.height:
        bg = Image.new("RGBA", (out_w, out_h), (200, 200, 200, 255))
        src_l = max(0, left);  src_t = max(0, top)
        src_r = min(img.width, right); src_b = min(img.height, bottom)
        if src_r > src_l and src_b > src_t:
            bg.paste(img.crop((src_l, src_t, src_r, src_b)),
                     (src_l - left, src_t - top))
        return bg

    return img.crop((left, top, right, bottom))


# ---------------------------------------------------------------------------
# Stats overlay
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Avenir Next.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]
_EMOJI_FONT_CANDIDATES = [
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
]
_ACTIVITY_ICONS = {
    # Strava sport_type values
    "Ride":           "🚴",
    "VirtualRide":    "🚴",
    "Run":            "🏃",
    "VirtualRun":     "🏃",
    "Walk":           "🚶",
    "Hike":           "🥾",
    "Swim":           "🏊",
    "WeightTraining": "🏋️",
    "Yoga":           "🧘",
    # GPX track type values (lowercase)
    "cycling":        "🚴",
    "running":        "🏃",
    "swimming":       "🏊",
    "walking":        "🚶",
    "hiking":         "🥾",
    "default":        "🏅",
}


def _load_font(size: int) -> "ImageFont.FreeTypeFont":
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _load_emoji_font(size: int) -> "ImageFont.FreeTypeFont | None":
    from PIL import ImageFont
    for path in _EMOJI_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return None


def _format_stat(key: str, activity: dict) -> tuple[str, str] | None:
    """
    Return (emoji_prefix, text) for a stat field, or None if data is absent.
    Splitting allows the emoji and text parts to be rendered with different fonts.
    """
    v = activity.get(key) or activity.get(
        {"elevation_gain": "total_elevation_gain"}.get(key, key)
    )
    if v is None:
        return None
    if key == "distance":
        return ("", f"{v / 1000:.1f} km")
    if key == "elevation_gain":
        return ("🏔", f" {v:.0f} m")
    if key == "moving_time":
        h, m = divmod(int(v) // 60, 60)
        return ("", f"{h}h {m:02d}m" if h else f"{m}m")
    if key == "average_speed":
        return ("", f"{v * 3.6:.1f} km/h")
    if key == "max_speed":
        return ("", f"max {v * 3.6:.1f} km/h")
    if key == "average_heartrate":
        return ("", f"HR {v:.0f} bpm")
    if key == "max_heartrate":
        return ("", f"HR max {v:.0f} bpm")
    if key == "average_watts":
        return ("", f"{v:.0f} W")
    if key == "max_watts":
        return ("", f"max {v:.0f} W")
    return ("", str(v))


def _mixed_width(probe, emoji: str, text: str, ef, tf) -> int:
    """Measure total pixel width of an (emoji, text) pair."""
    w = 0
    if emoji and ef:
        bb = probe.textbbox((0, 0), emoji, font=ef)
        w += bb[2] - bb[0]
    if text:
        bb = probe.textbbox((0, 0), text, font=tf)
        w += bb[2] - bb[0]
    return w


def _draw_mixed(draw, x: int, y: int, emoji: str, text: str,
                ef, tf, color) -> int:
    """Draw (emoji, text) pair, return new x position."""
    if emoji and ef:
        draw.text((x, y), emoji, font=ef, fill=color, embedded_color=True)
        bb = draw.textbbox((x, y), emoji, font=ef)
        x += bb[2] - bb[0]
    if text:
        draw.text((x, y), text, font=tf, fill=color)
        bb = draw.textbbox((x, y), text, font=tf)
        x += bb[2] - bb[0]
    return x


def draw_stats_overlay(img: Image.Image, activity: dict, cfg: dict) -> Image.Image:
    ov = cfg.get("stats_overlay", {})
    if not ov.get("enabled", True):
        return img

    fields    = cfg.get("user", {}).get("stats", {}).get("fields", ["distance", "elevation_gain"])
    font_cfg  = ov.get("font", {})
    icon_cfg  = ov.get("icon", {})
    font_size = font_cfg.get("size", 52)
    gap       = ov.get("gap", 40)
    padding   = ov.get("padding", 24)

    tf = _load_font(font_size)                       # text font (Helvetica)
    ef = _load_emoji_font(icon_cfg.get("size", font_size))  # emoji font

    sport     = activity.get("sport_type", "Ride")
    icons_map = {**_ACTIVITY_ICONS, **icon_cfg.get("activity_icons", {})}
    icon_char = icons_map.get(sport, icons_map["default"])

    stats = [s for s in (_format_stat(f, activity) for f in fields) if s]

    # ── measure for centering ──
    probe  = ImageDraw.Draw(img)
    sep    = "   •   "
    sep_w  = probe.textbbox((0, 0), sep, font=tf)[2]

    icon_w  = _mixed_width(probe, icon_char, "", ef, tf)
    stats_w = sum(_mixed_width(probe, e, t, ef, tf) for e, t in stats)
    total_w = icon_w + gap + stats_w + sep_w * max(0, len(stats) - 1)

    img_w, img_h = img.size
    bar_h = font_size + padding * 2

    # ── background bar ──
    bg  = _parse_color(ov.get("background_color", "#000000"),
                       ov.get("background_opacity", 0.6))
    bar = Image.new("RGBA", (img_w, bar_h), bg)
    result = img.copy()
    result.alpha_composite(bar, (0, img_h - bar_h))

    draw  = ImageDraw.Draw(result)
    color = _parse_color(font_cfg.get("color", "#ffffff"))
    x     = (img_w - total_w) // 2
    y     = img_h - bar_h + padding

    # ── activity icon ──
    x = _draw_mixed(draw, x, y, icon_char, "", ef, tf, color)
    x += gap

    # ── stat fields ──
    for i, (emoji, text) in enumerate(stats):
        if i > 0:
            draw.text((x, y), sep, font=tf, fill=_parse_color("#888888"))
            x += sep_w
        x = _draw_mixed(draw, x, y, emoji, text, ef, tf, color)

    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def render_activity_map(points: list[tuple[float, float]], activity: dict,
                        cfg: dict) -> Image.Image | None:
    """
    Render a map image for an activity.

    Args:
        points:   List of (lat, lon) tuples.
        activity: Activity dict (from database row) for stats overlay.
        cfg:      Parsed config.yaml dict.
    """
    if not points:
        return None

    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    # Tiny epsilon so single-point activities don't produce zero-size bbox
    lat_span = max(max_lat - min_lat, 0.001)
    lon_span = max(max_lon - min_lon, 0.001)
    min_lat -= lat_span * 0.005
    max_lat += lat_span * 0.005
    min_lon -= lon_span * 0.005
    max_lon += lon_span * 0.005

    out_w     = cfg["map"]["width"]
    out_h     = cfg["map"]["height"]
    max_tiles = cfg["map"].get("max_tiles", 100)
    zoom_offset = int(cfg["map"].get("zoom_offset", 0))

    # Padding as fractions of output dimensions (e.g. 0.05 = 5% of width/height).
    # pad_left=0.05, pad_right=0.05 reserves 10% total horizontally; zoom is chosen
    # so the route fills the remaining 90%.
    raw = cfg["map"].get("padding", 0.05)
    if isinstance(raw, dict):
        pad_top    = float(raw.get("top",    0.05))
        pad_bottom = float(raw.get("bottom", 0.12))
        pad_left   = float(raw.get("left",   0.05))
        pad_right  = float(raw.get("right",  0.05))
    else:
        f = float(raw)
        pad_top = pad_bottom = pad_left = pad_right = f

    pad_left_px   = pad_left   * out_w
    pad_right_px  = pad_right  * out_w
    pad_top_px    = pad_top    * out_h
    pad_bottom_px = pad_bottom * out_h

    route_area_w = out_w - pad_left_px  - pad_right_px
    route_area_h = out_h - pad_top_px   - pad_bottom_px

    zoom = _select_zoom(min_lat, min_lon, max_lat, max_lon,
                        route_area_w, route_area_h, max_tiles, zoom_offset)
    print(f"    zoom={zoom}")

    print(f"    Fetching tiles…")
    canvas, origin = _stitch_tiles(min_lat, min_lon, max_lat, max_lon, zoom, cfg)

    print(f"    Drawing route…")
    canvas = _draw_route(canvas, origin, points, zoom, cfg)
    canvas = _draw_markers(canvas, origin, points, zoom, cfg)

    print(f"    Cropping to {out_w}×{out_h}…")
    canvas = _crop_to_output(canvas, origin, min_lat, min_lon, max_lat, max_lon,
                             zoom, out_w, out_h,
                             pad_left_px, pad_right_px, pad_top_px, pad_bottom_px)

    print(f"    Drawing stats overlay…")
    canvas = draw_stats_overlay(canvas, activity, cfg)

    return canvas
