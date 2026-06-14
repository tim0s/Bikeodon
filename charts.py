"""
Chart generator for activity data.
Produces 1200×675 PNG images using matplotlib.
Each function returns the output path, or None if data is unavailable.
"""

import os

from inference import infer_ftp, infer_max_hr
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as ticker
import numpy as np

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_style(fig, ax_list, cfg):
    """Apply dark theme from config to a figure and its axes."""
    style = cfg.get("charts", {}).get("style", {})
    bg    = style.get("background_color", "#ffffff")
    fg    = style.get("text_color",       "#222222")
    grid  = style.get("grid_color",       "#dddddd")

    fig.patch.set_facecolor(bg)
    for ax in ax_list:
        ax.set_facecolor(bg)
        ax.tick_params(colors=fg, labelsize=11)
        ax.xaxis.label.set_color(fg)
        ax.yaxis.label.set_color(fg)
        ax.title.set_color(fg)
        for spine in ax.spines.values():
            spine.set_edgecolor(grid)
        ax.grid(True, color=grid, linewidth=0.5, linestyle="--", alpha=0.7)
        ax.set_axisbelow(True)


def _zone_for_value(v, thresholds):
    """Return 0-based zone index for a value given a list of upper thresholds."""
    for i, t in enumerate(thresholds):
        if v <= t:
            return i
    return len(thresholds) - 1


def _zone_thresholds(zones, base):
    """Convert zone list (max_pct entries) to absolute threshold values."""
    return [z["max_pct"] / 100 * base for z in zones]


def _zone_distribution(values, thresholds):
    """Return count per zone (same length as thresholds)."""
    arr = np.asarray(values)
    t   = np.asarray(thresholds)
    idx = np.clip(np.searchsorted(t, arr, side='left'), 0, len(t) - 1)
    return [int((idx == i).sum()) for i in range(len(t))]


def _time_axis(stream, key):
    """
    Return (times_minutes, values) arrays, skipping None values.
    Falls back to point index if no elapsed_secs are stored.
    """
    pairs = [(p.get("elapsed_secs"), p.get(key))
             for p in stream if p.get(key) is not None]
    if not pairs:
        return None, None

    values = [v for _, v in pairs]
    raw_t  = [t for t, _ in pairs]

    if raw_t[0] is not None:
        t_min = [t / 60 for t in raw_t]
    else:
        # Estimate from array index
        t_min = [i / len(pairs) * (len(pairs) / 60) for i in range(len(pairs))]

    return np.array(t_min), np.array(values)


def _downsample(t, y, max_pts=600):
    """Thin arrays to at most max_pts points for faster matplotlib rendering."""
    if len(t) <= max_pts:
        return t, y
    step = max(1, len(t) // max_pts)
    return t[::step], y[::step]


def _smooth(y, window=30):
    """Simple moving-average smooth."""
    if len(y) < window:
        return y
    kernel = np.ones(window) / window
    return np.convolve(y, kernel, mode="same")


def _fig_px(cfg):
    """Return (width_in, height_in, dpi) from config map dimensions."""
    dpi = 100
    w   = cfg["map"]["width"]  / dpi
    h   = cfg["map"]["height"] / dpi
    return w, h, dpi


# ---------------------------------------------------------------------------
# Zone background bands
# ---------------------------------------------------------------------------

def _draw_zone_bands(ax, zones, thresholds, alpha=0.08):
    """Shade horizontal bands for each zone."""
    if not zones:
        return
    low = 0
    for i, (z, hi) in enumerate(zip(zones, thresholds)):
        ax.axhspan(low, hi, facecolor=z["color"], alpha=alpha, zorder=0)
        low = hi
    # Top zone has no upper bound — extend generously
    ax.axhspan(low, low * 1.5 or 300, facecolor=zones[-1]["color"], alpha=alpha, zorder=0)


# ---------------------------------------------------------------------------
# HR chart
# ---------------------------------------------------------------------------

def render_hr_chart(stream, cfg, out_path: str, db_path: str | None = None, user_id: int | None = None) -> str | None:
    chart_cfg = cfg.get("charts", {})
    hr_cfg    = chart_cfg.get("heart_rate", {})
    if not hr_cfg.get("enabled", True):
        return None

    t, hr = _time_axis(stream, "hr")
    if t is None or len(hr) == 0:
        return None

    max_hr = hr_cfg.get("max_hr")
    if not max_hr and db_path and user_id is not None:
        max_hr = infer_max_hr(db_path, user_id)
    if not max_hr:
        max_hr = int(max(hr))
    zones   = hr_cfg.get("zones", _default_hr_zones())
    thresh  = _zone_thresholds(zones, max_hr)
    counts  = _zone_distribution(hr, thresh)
    total   = sum(counts)
    pcts    = [c / total * 100 if total else 0 for c in counts]

    style     = chart_cfg.get("style", {})
    line_col  = style.get("line_color", "#FC4C02")

    w, h, dpi = _fig_px(cfg)
    fig, (ax_time, ax_dist) = plt.subplots(
        2, 1, figsize=(w, h), dpi=dpi,
        gridspec_kw={"height_ratios": [2.2, 1], "hspace": 0.65},
        constrained_layout=True,
    )
    _apply_style(fig, [ax_time, ax_dist], cfg)

    # ── time series ──
    t_ds, hr_ds = _downsample(t, hr)
    _, hr_smooth_ds = _downsample(t, _smooth(hr))
    _draw_zone_bands(ax_time, zones, thresh)
    ax_time.fill_between(t_ds, hr_ds, alpha=0.15, color=line_col)
    ax_time.plot(t_ds, hr_smooth_ds, color=line_col, linewidth=1.5, label="Heart Rate")
    ax_time.set_ylabel("bpm")
    ax_time.set_xlabel("Time (min)")
    ax_time.set_title("Heart Rate", fontsize=14, fontweight="bold", pad=10)
    ax_time.set_xlim(t[0], t[-1])
    hr_pad = (hr.max() - hr.min()) * 0.10
    ax_time.set_ylim(hr.min() - hr_pad, hr.max() + hr_pad)
    ax_time.yaxis.set_minor_locator(ticker.AutoMinorLocator())

    # Zone HR labels on right axis
    ax_r = ax_time.twinx()
    ax_r.set_facecolor("none")
    ax_r.set_ylim(ax_time.get_ylim())
    ax_r.set_yticks(thresh)
    ax_r.set_yticklabels(
        [f"{t:.0f}" for t in thresh],
        fontsize=9, color=style.get("text_color", "#222222"),
    )
    for spine in ax_r.spines.values():
        spine.set_visible(False)

    # ── zone distribution ──
    _draw_zone_bar(ax_dist, zones, pcts, cfg)

    fig.savefig(out_path, dpi=dpi, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Power chart
# ---------------------------------------------------------------------------

def render_power_chart(stream, cfg, out_path: str, db_path: str | None = None, user_id: int | None = None) -> str | None:
    chart_cfg = cfg.get("charts", {})
    pwr_cfg   = chart_cfg.get("power", {})
    if not pwr_cfg.get("enabled", True):
        return None

    t, power = _time_axis(stream, "power")
    if t is None or len(power) == 0:
        return None

    ftp = pwr_cfg.get("ftp")
    if not ftp and db_path and user_id is not None:
        ftp = infer_ftp(db_path, user_id)
    if not ftp:
        print("    Warning: charts.power.ftp not set and could not be inferred — skipping power chart")
        return None

    zones  = pwr_cfg.get("zones", _default_power_zones())
    thresh = _zone_thresholds(zones, ftp)
    counts = _zone_distribution(power, thresh)
    total  = sum(counts)
    pcts   = [c / total * 100 if total else 0 for c in counts]

    style    = cfg.get("charts", {}).get("style", {})
    line_col = style.get("power_line_color", style.get("line_color", "#4fc3f7"))

    w, h, dpi = _fig_px(cfg)
    fig, (ax_time, ax_dist) = plt.subplots(
        2, 1, figsize=(w, h), dpi=dpi,
        gridspec_kw={"height_ratios": [2.2, 1], "hspace": 0.65},
        constrained_layout=True,
    )
    _apply_style(fig, [ax_time, ax_dist], cfg)

    # ── time series ──
    t_ds, pw_ds = _downsample(t, power)
    _, pw_smooth_ds = _downsample(t, _smooth(power, window=15))
    _draw_zone_bands(ax_time, zones, thresh)
    ax_time.fill_between(t_ds, pw_ds, alpha=0.15, color=line_col)
    ax_time.plot(t_ds, pw_smooth_ds, color=line_col, linewidth=1.5)
    ax_time.axhline(ftp, color="#ffffff", linewidth=1, linestyle="--", alpha=0.5, label=f"FTP {ftp}W")
    ax_time.set_ylabel("Watts")
    ax_time.set_xlabel("Time (min)")
    ax_time.set_title("Power", fontsize=14, fontweight="bold", pad=10)
    ax_time.set_xlim(t[0], t[-1])
    pwr_pad = (power.max() - power.min()) * 0.10
    ax_time.set_ylim(power.min() - pwr_pad, power.max() + pwr_pad)
    ax_time.legend(fontsize=9, framealpha=0.2,
                   labelcolor=style.get("text_color", "#222222"))

    # ── zone distribution ──
    _draw_zone_bar(ax_dist, zones, pcts, cfg)

    fig.savefig(out_path, dpi=dpi, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Shared zone distribution bar
# ---------------------------------------------------------------------------

def _draw_zone_bar(ax, zones, pcts, cfg):
    """Horizontal stacked bar showing % time per zone."""
    style = cfg.get("charts", {}).get("style", {})
    fg    = style.get("text_color", "#eeeeee")

    left = 0
    for z, pct in zip(zones, pcts):
        if pct < 0.5:
            left += pct
            continue
        ax.barh(0, pct, left=left, color=z["color"], height=0.5, zorder=3)
        if pct > 3:
            ax.text(left + pct / 2, 0, f"{pct:.0f}%",
                    ha="center", va="center", fontsize=9,
                    color="white", fontweight="bold", zorder=4)
        left += pct

    ax.set_xlim(0, 100)
    ax.set_ylim(-0.5, 0.5)
    ax.set_xlabel("% of time")
    ax.set_yticks([])
    ax.set_title("Zone Distribution", fontsize=11, pad=8)

    # Legend — horizontal row beneath the bar
    patches = [mpatches.Patch(color=z["color"], label=z["name"]) for z in zones]
    if patches:
        ax.legend(handles=patches, loc="upper center",
                  bbox_to_anchor=(0.5, -0.35),
                  ncol=min(len(zones), 4),
                  fontsize=8, framealpha=0.15, labelcolor=fg,
                  handlelength=1.2, columnspacing=1.0)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_charts(activity_id: int, stream: list[dict], cfg: dict,
                    out_dir: str, db_path: str | None = None,
                    user_id: int | None = None) -> list[str]:
    """
    Generate all available charts for an activity.
    Returns list of image paths (only those that were successfully created).
    """
    os.makedirs(out_dir, exist_ok=True)
    paths = []

    hr_path = render_hr_chart(
        stream, cfg, os.path.join(out_dir, f"{activity_id}_hr.png"),
        db_path=db_path, user_id=user_id,
    )
    if hr_path:
        print(f"    Saved HR chart → {hr_path}")
        paths.append(hr_path)

    power_path = render_power_chart(
        stream, cfg, os.path.join(out_dir, f"{activity_id}_power.png"),
        db_path=db_path, user_id=user_id,
    )
    if power_path:
        print(f"    Saved power chart → {power_path}")
        paths.append(power_path)

    return paths


# ---------------------------------------------------------------------------
# Default zone definitions
# ---------------------------------------------------------------------------

def _default_hr_zones():
    return [
        {"name": "Z1 Recovery",   "max_pct": 60,  "color": "#5b9bd5"},
        {"name": "Z2 Endurance",  "max_pct": 70,  "color": "#70ad47"},
        {"name": "Z3 Tempo",      "max_pct": 80,  "color": "#ffc000"},
        {"name": "Z4 Threshold",  "max_pct": 90,  "color": "#ff7043"},
        {"name": "Z5 VO2 Max",    "max_pct": 100, "color": "#d32f2f"},
    ]


def _default_power_zones():
    return [
        {"name": "Z1 Recovery",      "max_pct": 55,  "color": "#5b9bd5"},
        {"name": "Z2 Endurance",     "max_pct": 75,  "color": "#70ad47"},
        {"name": "Z3 Tempo",         "max_pct": 90,  "color": "#ffc000"},
        {"name": "Z4 Threshold",     "max_pct": 105, "color": "#ff7043"},
        {"name": "Z5 VO2 Max",       "max_pct": 120, "color": "#d32f2f"},
        {"name": "Z6 Anaerobic",     "max_pct": 150, "color": "#9c27b0"},
        {"name": "Z7 Neuromuscular", "max_pct": 999, "color": "#424242"},
    ]
