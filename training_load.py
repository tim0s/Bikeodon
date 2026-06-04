"""
Training load metrics for cyclists.

References:
  - Coggan, A. (2003). "Normalized Power, Intensity Factor, and Training Stress Score."
  - Banister, E.W. (1991). "Modeling elite athletic performance."
    in Physiological Testing of Elite Athletes, pp 403-424.
  - Morton, R.H. et al (1990). "Modeling human performance in running."
    Journal of Applied Physiology, 69(3), 1171-1177.
"""
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# Durations (seconds) for peak mean power computation
PEAK_DURATIONS = [5, 30, 60, 300, 1200, 3600]
PEAK_LABELS    = ["5s", "30s", "1min", "5min", "20min", "60min"]


def compute_np(watts_list: list) -> float | None:
    """
    Coggan Normalized Power (NP):
      1. 30-second rolling average of power
      2. Raise each value to the 4th power
      3. Take the mean, then the 4th root

    Requires at least 30 seconds of data. Assumes ~1 Hz sampling.
    """
    w = [x for x in watts_list if x is not None]
    if len(w) < 30:
        return None
    window = 30
    rolled = [sum(w[i - window + 1:i + 1]) / window for i in range(window - 1, len(w))]
    mean4  = sum(x ** 4 for x in rolled) / len(rolled)
    return mean4 ** 0.25


def compute_tss(np_watts: float, duration_s: float, ftp: float) -> float | None:
    """
    Training Stress Score (Coggan):
      TSS = (duration_s × NP × IF) / (FTP × 3600) × 100
    where IF (Intensity Factor) = NP / FTP.

    Returns None if any required value is missing or FTP ≤ 0.
    """
    if not ftp or ftp <= 0 or not np_watts or not duration_s:
        return None
    intensity_factor = np_watts / ftp
    return (duration_s * np_watts * intensity_factor) / (ftp * 3600) * 100


def compute_trimp(
    hr_list:      list,
    elapsed_list: list,
    hr_max:       float,
    hr_rest:      float,
) -> float | None:
    """
    Banister TRIMP (Training Impulse):
      TRIMP = Σ Δt_min × hrr × 0.64 × e^(1.92 × hrr)
    where hrr = (HR – HR_rest) / (HR_max – HR_rest).

    Δt is in minutes to match the original formulation.
    Returns None if HR data or reference values are insufficient.
    """
    if not hr_max or not hr_rest or hr_max <= hr_rest:
        return None
    paired = [(hr, t) for hr, t in zip(hr_list, elapsed_list)
              if hr is not None and t is not None]
    if len(paired) < 2:
        return None
    trimp = 0.0
    for i in range(1, len(paired)):
        dt = paired[i][1] - paired[i - 1][1]
        if dt <= 0:
            continue
        hrr = (paired[i][0] - hr_rest) / (hr_max - hr_rest)
        hrr = max(0.0, min(1.0, hrr))
        trimp += (dt / 60.0) * hrr * 0.64 * math.exp(1.92 * hrr)
    return trimp if trimp > 0 else None


def compute_peak_powers(stream: list) -> dict | None:
    """
    Best mean power for each duration in PEAK_DURATIONS.
    stream: list of dicts with 'power' and 'elapsed_secs' keys.
    Returns {label: watts} or None.
    """
    pairs = sorted(
        [(p["elapsed_secs"], p["power"])
         for p in stream
         if p.get("power") is not None and p.get("elapsed_secs") is not None],
        key=lambda x: x[0],
    )
    if not pairs:
        return None

    n  = len(pairs)
    ts = [p[0] for p in pairs]
    ws = [p[1] for p in pairs]

    result = {}
    for dur, label in zip(PEAK_DURATIONS, PEAK_LABELS):
        best = None
        j    = 0
        for i in range(n):
            # Advance j to include as much of the dur-second window as possible
            while j + 1 < n and ts[j + 1] - ts[i] <= dur:
                j += 1
            span = ts[j] - ts[i]
            # Require at least 50 % of target duration to avoid spurious values
            if span < dur * 0.5:
                continue
            avg = sum(ws[i:j + 1]) / (j - i + 1)
            if best is None or avg > best:
                best = avg
        if best is not None:
            result[label] = round(best, 1)

    return result if result else None


def compute_pmc(
    daily_tss: dict,
    end_date:  str | None = None,
    days:      int        = 180,
) -> list:
    """
    Performance Management Chart: CTL, ATL, TSB over time.

    daily_tss: {ISO-date-str: tss_float}
    Returns list of {date, tss, ctl, atl, tsb} for the last `days` days,
    in chronological order.

    CTL (Chronic Training Load / fitness): 42-day EWA  — K = 1 − e^(−1/42)
    ATL (Acute Training Load / fatigue):    7-day EWA  — K = 1 − e^(−1/7)
    TSB (Training Stress Balance / form):   CTL − ATL  (computed before today's load)

    See Morton et al. (1990) for the two-component performance model.
    """
    k_ctl = 1 - math.exp(-1 / 42)
    k_atl = 1 - math.exp(-1 / 7)

    if end_date:
        end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
    else:
        end = datetime.now(timezone.utc)

    # Start far enough back for CTL to stabilize (42-day constant → ~6× = 252 days)
    warmup_days = 120
    start = end - timedelta(days=days + warmup_days)
    cutoff = end - timedelta(days=days)

    ctl = 0.0
    atl = 0.0
    result = []

    current = start
    while current <= end:
        ds  = current.strftime("%Y-%m-%d")
        tss = daily_tss.get(ds, 0.0)

        tsb = ctl - atl           # form = yesterday's fitness minus yesterday's fatigue
        ctl = ctl + k_ctl * (tss - ctl)
        atl = atl + k_atl * (tss - atl)

        if current >= cutoff:
            result.append({
                "date": ds,
                "tss":  round(tss, 1),
                "ctl":  round(ctl, 1),
                "atl":  round(atl, 1),
                "tsb":  round(tsb, 1),
            })

        current += timedelta(days=1)

    return result


def weekly_load(daily_tss: dict, weeks: int = 26) -> list:
    """
    Aggregate TSS by ISO week for the last `weeks` weeks.
    Returns list of {week: "YYYY-WNN", tss: float} in chronological order.
    """
    by_week = defaultdict(float)
    for ds, tss in daily_tss.items():
        try:
            dt = datetime.fromisoformat(ds)
            by_week[dt.strftime("%G-W%V")] += tss
        except ValueError:
            pass

    now = datetime.now(timezone.utc)
    result = []
    for i in range(weeks - 1, -1, -1):
        week_start = now - timedelta(weeks=i)
        label = week_start.strftime("%G-W%V")
        result.append({"week": label, "tss": round(by_week.get(label, 0.0), 1)})
    return result


def compute_zone_times(
    stream:      list,
    hr_zones:    list,
    power_zones: list,
    hr_max:      float | None,
    ftp:         float | None,
) -> tuple:
    """
    Compute seconds spent in each HR and power zone across a stream.

    Zones are defined as a list of {name, max_pct, color} dicts where max_pct is the
    upper boundary of each zone as a percentage of hr_max / FTP.

    Returns (hr_zone_secs, power_zone_secs): each is {zone_name: seconds} or None
    if there was no data of that type.
    """
    def build_bounds(zones, ref):
        """List of (lower, upper, name) in absolute units (bpm / watts)."""
        if not zones or not ref:
            return []
        bounds, prev = [], 0.0
        for z in zones:
            upper = z["max_pct"] / 100.0 * ref
            bounds.append((prev, upper, z["name"]))
            prev = upper
        return bounds

    def find_zone(value, bounds):
        for lower, upper, name in bounds[:-1]:
            if lower <= value < upper:
                return name
        return bounds[-1][2] if bounds else None  # last zone catches everything above

    hr_bounds    = build_bounds(hr_zones,    hr_max)
    power_bounds = build_bounds(power_zones, ftp)

    hr_secs    = {z["name"]: 0.0 for z in hr_zones}    if hr_zones    else {}
    power_secs = {z["name"]: 0.0 for z in power_zones} if power_zones else {}

    for i in range(1, len(stream)):
        prev_t = stream[i - 1].get("elapsed_secs")
        curr_t = stream[i].get("elapsed_secs")
        if prev_t is None or curr_t is None:
            continue
        dt = curr_t - prev_t
        if dt <= 0 or dt > 300:   # skip gaps / large pauses
            continue

        hr = stream[i].get("hr")
        if hr is not None and hr_bounds:
            zone = find_zone(hr, hr_bounds)
            if zone and zone in hr_secs:
                hr_secs[zone] += dt

        pw = stream[i].get("power")
        if pw is not None and power_bounds:
            zone = find_zone(pw, power_bounds)
            if zone and zone in power_secs:
                power_secs[zone] += dt

    return (
        hr_secs    if any(v > 0 for v in hr_secs.values())    else None,
        power_secs if any(v > 0 for v in power_secs.values()) else None,
    )


def aggregate_power_curve(peak_list: list) -> dict:
    """
    Best power at each duration across a list of per-activity peak dicts.
    Returns {label: watts} for PEAK_LABELS.
    """
    result = {}
    for label in PEAK_LABELS:
        best = None
        for peaks in peak_list:
            if peaks and label in peaks:
                v = peaks[label]
                if best is None or v > best:
                    best = v
        if best is not None:
            result[label] = best
    return result
