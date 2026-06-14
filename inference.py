"""
Infer training parameters (max HR, FTP, CP, W') from stored activity metrics.

All functions read from pre-computed columns (max_heartrate, peak_power_json)
rather than raw sample data, so they work without points_json.
"""

import json

import numpy as np

from database import _conn


def infer_max_hr(db_path: str, user_id: int) -> float | None:
    """
    Estimate max heart rate as the 99th percentile of per-activity max_heartrate
    values. Using a high percentile rather than the absolute max avoids inflating
    the estimate from sensor spikes.
    """
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT max_heartrate FROM activities"
            " WHERE user_id=? AND max_heartrate IS NOT NULL AND max_heartrate <= 220",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    values = [row["max_heartrate"] for row in rows]
    result = float(np.percentile(values, 99))
    print(f"  Inferred max HR: {result:.0f} bpm  (99th pct of {len(values)} activities)")
    return result


def infer_ftp(db_path: str, user_id: int) -> float | None:
    """
    Estimate FTP as 95% of the best ~20-minute MMP found across all activities.
    Looks for any stored duration in the 18–22 min window (1080–1320 s) to handle
    both the old fixed label "20min" and the new dense log-spaced grid.
    """
    from training_load import _label_to_secs
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT peak_power_json FROM activities"
            " WHERE user_id=? AND peak_power_json IS NOT NULL",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    best_20min = 0.0
    for row in rows:
        peaks = json.loads(row["peak_power_json"] or "{}")
        for label, power in peaks.items():
            if power is None:
                continue
            t = _label_to_secs(label)
            if t is None or not (1080 <= t <= 1320):
                continue
            if power > best_20min:
                best_20min = power

    if best_20min == 0:
        return None

    ftp = best_20min * 0.95
    print(f"  Inferred FTP: {ftp:.0f} W  (best ~20-min MMP {best_20min:.0f} W × 0.95)")
    return ftp


def infer_training_params(db_path: str, user_id: int) -> dict:
    """Return {'max_hr': float|None, 'ftp': float|None} for the given user."""
    return {
        "max_hr": infer_max_hr(db_path, user_id),
        "ftp":    infer_ftp(db_path, user_id),
    }
