"""
Infer training parameters (max HR, FTP) from recorded activity data.
Used as fallbacks when the user hasn't set explicit values in config.
"""

import json
import sqlite3

import numpy as np


def infer_max_hr(db_path: str) -> float | None:
    """
    Estimate max heart rate as the 99th percentile of all recorded HR values
    across every activity. Using a high percentile rather than the absolute max
    avoids inflating the estimate from sensor spikes.
    """
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT points_json FROM activities WHERE average_heartrate IS NOT NULL"
    ).fetchall()
    conn.close()

    all_hr = []
    for (pj,) in rows:
        for p in json.loads(pj or "[]"):
            if len(p) > 3 and p[3] is not None:
                all_hr.append(p[3])

    if not all_hr:
        return None

    result = float(np.percentile(all_hr, 99))
    print(f"  Inferred max HR: {result:.0f} bpm  (99th pct of {len(all_hr):,} samples)")
    return result


def infer_ftp(db_path: str) -> float | None:
    """
    Estimate FTP as 95% of the best 20-minute average power found across all
    activities with power data. This is the standard 20-min FTP test protocol.
    """
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT points_json, moving_time FROM activities WHERE average_watts IS NOT NULL"
    ).fetchall()
    conn.close()

    best_20min = 0.0

    for pj, moving_time in rows:
        points = json.loads(pj or "[]")

        # Extract (elapsed_secs, power) — indices 5 and 4
        data = [
            (p[5], p[4])
            for p in points
            if len(p) > 5 and p[4] is not None and p[5] is not None
        ]
        if len(data) < 20:
            continue

        times  = np.array([d[0] for d in data], dtype=float)
        powers = np.array([d[1] for d in data], dtype=float)

        window = 20 * 60  # 20 minutes in seconds
        total_duration = times[-1] - times[0]

        # If the activity is shorter than 20 min, use the full duration as the window
        effective_window = min(window, total_duration * 0.95)
        if effective_window < 60:
            continue

        # Sliding window: for each start point find all points within the window
        best_this = 0.0
        for i in range(len(times)):
            mask = (times >= times[i]) & (times <= times[i] + effective_window)
            if mask.sum() < 10:
                continue
            avg = float(powers[mask].mean())
            if avg > best_this:
                best_this = avg

        if best_this > best_20min:
            best_20min = best_this

    if best_20min == 0:
        return None

    # Scale by 0.95 only when we had a full 20-min window available
    ftp = best_20min * 0.95
    print(f"  Inferred FTP: {ftp:.0f} W  (best 20-min avg {best_20min:.0f} W × 0.95)")
    return ftp


def infer_training_params(db_path: str) -> dict:
    """Return {'max_hr': float|None, 'ftp': float|None}."""
    return {
        "max_hr": infer_max_hr(db_path),
        "ftp":    infer_ftp(db_path),
    }
