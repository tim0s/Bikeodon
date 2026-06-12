"""
Infer training parameters (max HR, FTP) from recorded activity data.
Used as fallbacks when the user hasn't set explicit values in config.
"""

import json

import numpy as np

from database import _conn

# points_json format: [lat, lon, ele, hr, power, elapsed_secs]
_IDX_HR      = 3
_IDX_POWER   = 4
_IDX_ELAPSED = 5


def infer_max_hr(db_path: str, user_id: int) -> float | None:
    """
    Estimate max heart rate as the 99th percentile of all recorded HR values
    for the given user. Using a high percentile rather than the absolute max
    avoids inflating the estimate from sensor spikes.
    """
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT points_json FROM activities"
            " WHERE user_id=? AND average_heartrate IS NOT NULL",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    all_hr = []
    for row in rows:
        for p in json.loads(row["points_json"] or "[]"):
            if len(p) > _IDX_HR and p[_IDX_HR] is not None:
                all_hr.append(p[_IDX_HR])

    if not all_hr:
        return None

    result = float(np.percentile(all_hr, 99))
    print(f"  Inferred max HR: {result:.0f} bpm  (99th pct of {len(all_hr):,} samples)")
    return result


def infer_ftp(db_path: str, user_id: int) -> float | None:
    """
    Estimate FTP as 95% of the best 20-minute average power found across all
    activities with power data for the given user. This is the standard
    20-min FTP test protocol.
    """
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT points_json, moving_time FROM activities"
            " WHERE user_id=? AND average_watts IS NOT NULL",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    best_20min = 0.0

    for row in rows:
        points = json.loads(row["points_json"] or "[]")

        data = [
            (p[_IDX_ELAPSED], p[_IDX_POWER])
            for p in points
            if len(p) > _IDX_ELAPSED
            and p[_IDX_POWER] is not None
            and p[_IDX_ELAPSED] is not None
        ]
        if len(data) < 20:
            continue

        times  = np.array([d[0] for d in data], dtype=float)
        powers = np.array([d[1] for d in data], dtype=float)

        window = 20 * 60  # seconds
        total_duration = times[-1] - times[0]

        # If the activity is shorter than 20 min, use the full duration as the window
        effective_window = min(window, total_duration * 0.95)
        if effective_window < 60:
            continue

        # O(n) two-pointer sliding window
        best_this = 0.0
        j = 0
        win_sum = 0.0
        win_n   = 0
        for i in range(len(times)):
            while j < len(times) and times[j] <= times[i] + effective_window:
                win_sum += powers[j]
                win_n   += 1
                j       += 1
            if win_n >= 10:
                avg = win_sum / win_n
                if avg > best_this:
                    best_this = avg
            win_sum -= powers[i]
            win_n   -= 1

        if best_this > best_20min:
            best_20min = best_this

    if best_20min == 0:
        return None

    ftp = best_20min * 0.95
    print(f"  Inferred FTP: {ftp:.0f} W  (best 20-min avg {best_20min:.0f} W × 0.95)")
    return ftp


def infer_training_params(db_path: str, user_id: int) -> dict:
    """Return {'max_hr': float|None, 'ftp': float|None} for the given user."""
    return {
        "max_hr": infer_max_hr(db_path, user_id),
        "ftp":    infer_ftp(db_path, user_id),
    }
