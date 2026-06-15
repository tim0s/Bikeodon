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

def _make_peak_durations() -> list[tuple[int, str]]:
    """~50 log-spaced durations from 1 s to 3600 s with human-readable labels."""
    import numpy as np
    seen, result = set(), []
    for x in np.logspace(0, np.log10(3600), 55):
        s = int(round(x))
        if s < 1 or s in seen:
            continue
        seen.add(s)
        if s < 60:
            label = f"{s}s"
        elif s % 60 == 0:
            label = f"{s // 60}min"
        else:
            label = f"{s}s"
        result.append((s, label))
    return result


def _label_to_secs(label: str) -> int | None:
    """Parse a duration label back to seconds. Returns None for unrecognised labels."""
    if label.endswith("min"):
        return int(label[:-3]) * 60
    if label.endswith("h"):
        return int(label[:-1]) * 3600
    if label.endswith("s"):
        return int(label[:-1])
    return None


_PEAK_DURATION_PAIRS = _make_peak_durations()
PEAK_DURATIONS = [p[0] for p in _PEAK_DURATION_PAIRS]
PEAK_LABELS    = [p[1] for p in _PEAK_DURATION_PAIRS]


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


def compute_hr_tss(
    hr_list:      list,
    elapsed_list: list,
    hr_max:       float,
    hr_rest:      float,
    lthr:         float,
) -> float | None:
    """
    Heart-rate TSS (hrTSS) — a power-TSS-compatible effort score using HR data.

    Intensity Factor is defined as the ratio of average HR reserve to threshold
    HR reserve, where HR reserve (HRR) = (HR − HR_rest) / (HR_max − HR_rest).

      IF_hr   = avg_hrr / threshold_hrr
      hrTSS   = duration_s / 3600 × IF_hr² × 100

    Requires hr_max, hr_rest, and LTHR (lactate threshold heart rate).
    Returns None if inputs are insufficient or LTHR ≤ HR_rest.

    Reference: Allen & Coggan, "Training and Racing with a Power Meter" (2nd ed.),
    appendix on HR-based TSS.
    """
    if not hr_max or not hr_rest or not lthr:
        return None
    if hr_max <= hr_rest or lthr <= hr_rest:
        return None

    threshold_hrr = (lthr - hr_rest) / (hr_max - hr_rest)
    if threshold_hrr <= 0:
        return None

    paired = [(hr, t) for hr, t in zip(hr_list, elapsed_list)
              if hr is not None and t is not None]
    if len(paired) < 2:
        return None

    total_time = 0.0
    weighted   = 0.0
    for i in range(1, len(paired)):
        dt = paired[i][1] - paired[i - 1][1]
        if dt <= 0 or dt > 300:
            continue
        hrr = (paired[i][0] - hr_rest) / (hr_max - hr_rest)
        hrr = max(0.0, min(1.0, hrr))
        weighted   += hrr * dt
        total_time += dt

    if total_time < 60:
        return None

    avg_hrr   = weighted / total_time
    if_hr     = avg_hrr / threshold_hrr
    return (total_time / 3600) * (if_hr ** 2) * 100


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
    # Anchor to Monday of the current ISO week so each step is exactly one unique week.
    monday = now - timedelta(days=now.weekday())
    result = []
    for i in range(weeks - 1, -1, -1):
        week_monday = monday - timedelta(weeks=i)
        label = week_monday.strftime("%G-W%V")
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
    Aggregates over whatever labels are present in the data, so old activities
    with the 6-label format and new ones with the 48-label format are merged
    correctly.
    """
    result = {}
    for peaks in peak_list:
        if not peaks:
            continue
        for label, v in peaks.items():
            if v is not None and (label not in result or v > result[label]):
                result[label] = v
    return result


# Map from MMP label to duration in seconds — only the aerobic range used for CP fitting.
# 5s/30s are excluded because they are neuromuscular and violate the CP model's assumptions.

_CP_FIT_MIN_S = 2 * 60    # 2 minutes — below this W' dominates, model breaks down
_CP_FIT_MAX_S = 20 * 60   # 20 minutes — above this fatigue factors beyond the 2-param model kick in


def fit_critical_power(mmp_dict: dict) -> tuple:
    """
    Estimate Critical Power (CP) and W' from the mean-maximal power curve.

    Linear work-time model (Monod & Scherrer 1965; Morton et al. 1996):
        Work(t) = P(t) × t = CP × t + W'
    OLS regression on (t, Work) gives slope=CP, intercept=W'.

    Restricted to 2–20 min where the 2-parameter severe-intensity model is
    valid (Burnley & Jones 2012). Sub-2-min efforts are neuromuscular/W'-dominated;
    efforts beyond ~20 min are confounded by pacing and substrate factors that
    the simple model doesn't capture, and their large Work values can skew OLS.

    Returns (cp_watts, w_prime_joules) rounded, or (None, None) if the fit is
    invalid (fewer than 2 points, or non-positive CP / W').
    """
    points = []
    for label, power in mmp_dict.items():
        if power is None:
            continue
        t = _label_to_secs(label)
        if t is None or t < _CP_FIT_MIN_S or t > _CP_FIT_MAX_S:
            continue
        points.append((t, power * t))

    if len(points) < 2:
        return None, None

    n    = len(points)
    sx   = sum(p[0] for p in points)
    sy   = sum(p[1] for p in points)
    sxy  = sum(p[0] * p[1] for p in points)
    sx2  = sum(p[0] ** 2 for p in points)

    denom = n * sx2 - sx * sx
    if denom == 0:
        return None, None

    cp      = (n * sxy - sx * sy) / denom
    w_prime = (sy - cp * sx) / n

    if cp <= 0 or w_prime <= 0:
        return None, None

    return round(cp, 1), round(w_prime)


def compute_wbal(stream: list, cp: float, w_prime: float) -> list | None:
    """
    W' balance over time using the Skiba (2012) differential model.

    Depletion  (P > CP): dW'bal = -(P - CP) × dt
    Reconstitution (P ≤ CP): dW'bal = (W' - W'bal) × (CP - P) / W' × dt

    Returns a list of {elapsed_secs, wbal_pct} sampled every ~10 s,
    where wbal_pct is W'bal expressed as a percentage of the full W' reserve.
    Returns None if there is insufficient power data.

    Reference:
      Skiba et al. (2012). "Modeling the expenditure and reconstitution of work
      capacity above critical power." Medicine & Science in Sports & Exercise, 44(8).
    """
    pairs = sorted(
        [(p["elapsed_secs"], p["power"])
         for p in stream
         if p.get("power") is not None and p.get("elapsed_secs") is not None],
        key=lambda x: x[0],
    )
    if len(pairs) < 10:
        return None

    wbal        = float(w_prime)
    result      = []
    sample_step = 30          # output point every ~30 s
    next_sample = pairs[0][0]

    for i in range(1, len(pairs)):
        t_prev, p_prev = pairs[i - 1]
        t_curr, _      = pairs[i]
        dt = t_curr - t_prev
        if dt <= 0 or dt > 120:   # skip gaps / pauses
            continue
        if p_prev > cp:
            wbal -= (p_prev - cp) * dt
        else:
            wbal += (w_prime - wbal) * (cp - p_prev) / w_prime * dt
        wbal = max(0.0, min(w_prime, wbal))

        if t_curr >= next_sample:
            result.append({
                "elapsed_secs": int(t_curr),
                "wbal_pct": round(wbal / w_prime * 100, 1),
            })
            next_sample = t_curr + sample_step

    return result if len(result) > 5 else None
