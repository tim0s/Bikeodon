"""
Convert a recorded live-training session (power/cadence/HR/speed samples at
~1Hz, no GPS) into the shapes the existing activity pipeline expects:
  - the `stream` list process_activity()/generate_charts() consume
  - the summary dict upsert_activity() consumes
  - the Strava-streams-dict shape fit_encoder.generate_fit() consumes

No new statistics/parsing infrastructure — this only adapts already-recorded
samples into formats the upload/Strava-sync pipeline already understands.
"""
import hashlib

KMH_TO_MS = 1 / 3.6


def _activity_id(uid: int, started_at: str, n_samples: int) -> int:
    """Stable id derived the same way activity_parser._file_id hashes uploaded
    file content — disjoint enough from Strava ids (~10^10) and upload ids
    (up to ~4.5x10^15) to avoid collisions in practice."""
    digest = hashlib.sha256(f"{uid}:{started_at}:{n_samples}".encode()).hexdigest()
    return int(digest[:13], 16)


def _clean(values):
    present = [v for v in values if v is not None]
    return present


def build_activity_from_samples(samples: list, name: str, started_at: str, uid: int):
    """Returns (activity_dict, stream, fit_streams)."""
    n = len(samples)
    activity_id = _activity_id(uid, started_at, n)

    times = [round(s.get("t", 0)) for s in samples]
    powers = [s.get("power") for s in samples]
    hrs = [s.get("hr") for s in samples]
    cadences = [s.get("cadence") for s in samples]
    speeds_kmh = [s.get("speed") for s in samples]  # km/h, may contain None

    stream = [
        {"lat": None, "lon": None, "ele": None, "hr": hr, "power": power, "elapsed_secs": t}
        for t, power, hr in zip(times, powers, hrs)
    ]

    # Integrate speed -> cumulative distance (meters), treating gaps (no sensor) as 0 speed.
    cumulative_distance = []
    total_distance = 0.0
    prev_t = 0.0
    for i, s in enumerate(samples):
        t = s.get("t", 0)
        dt = max(0.0, t - prev_t)
        speed_ms = (s.get("speed") or 0.0) * KMH_TO_MS
        total_distance += speed_ms * dt
        cumulative_distance.append(total_distance)
        prev_t = t

    watts_present = _clean(powers)
    hr_present = _clean(hrs)
    speed_ms_present = [v * KMH_TO_MS for v in _clean(speeds_kmh)]
    elapsed_time = times[-1] if times else 0

    activity = {
        "id": activity_id,
        "name": name or "Training Session",
        "sport_type": "VirtualRide",
        "start_date": started_at,
        "distance": total_distance,
        "total_elevation_gain": 0,
        "moving_time": elapsed_time,
        "elapsed_time": elapsed_time,
        "average_speed": (sum(speed_ms_present) / len(speed_ms_present)) if speed_ms_present else None,
        "max_speed": max(speed_ms_present) if speed_ms_present else None,
        "average_heartrate": (sum(hr_present) / len(hr_present)) if hr_present else None,
        "max_heartrate": max(hr_present) if hr_present else None,
        "average_watts": (sum(watts_present) / len(watts_present)) if watts_present else None,
        "max_watts": max(watts_present) if watts_present else None,
    }

    fit_streams = {
        "time": {"data": times},
        "heartrate": {"data": hrs} if hr_present else {},
        "cadence": {"data": cadences} if _clean(cadences) else {},
        "watts": {"data": powers} if watts_present else {},
        "distance": {"data": cumulative_distance} if speed_ms_present else {},
        "velocity_smooth": {"data": [(s.get("speed") or 0.0) * KMH_TO_MS for s in samples]} if speed_ms_present else {},
    }

    return activity, stream, fit_streams
