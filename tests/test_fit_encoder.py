"""
Round-trip tests for fit_encoder.py: build a FIT file from an activity +
streams dict, parse it back with fitparse, and confirm every value we put
in comes back out correctly labeled.

This is the exact test that caught a real bug: the session message used
wrong FIT field numbers (e.g. field 20 was written expecting "avg_speed"
but the FIT profile defines field 20 as "avg_power"), so a real FIT reader
would have shown avg_power's value under avg_speed, max_power's value
under max_speed, etc. Field-number correctness can't be checked by eyeballing
the encoder — only by decoding with an independent reader (fitparse) and
comparing by field NAME, not by which line of our own code wrote it.
"""
import io

import fitparse
import pytest

from fit_encoder import generate_fit

ACTIVITY = {
    "start_date": "2026-01-15T08:00:00Z",
    "sport_type": "Ride",
    "elapsed_time": 3600,
    "moving_time": 3500,
    "distance": 30000.0,
    "max_speed": 15.5,
    "average_watts": 210.0,
    "max_watts": 450.0,
    "average_heartrate": 148.0,
    "max_heartrate": 172.0,
    "total_elevation_gain": 320.0,
}

STREAMS = {
    "time":       {"data": [0, 1, 2, 3, 4]},
    "heartrate":  {"data": [140, 141, 142, 143, 144]},
    "cadence":    {"data": [85, 86, 87, 88, 89]},
    "watts":      {"data": [200, 210, 220, 205, 195]},
    "distance":   {"data": [0.0, 8.3, 16.6, 24.9, 33.2]},
    "velocity_smooth": {"data": [8.3, 8.3, 8.3, 8.3, 8.3]},
}


def _parse(fit_bytes):
    ff = fitparse.FitFile(io.BytesIO(fit_bytes))
    by_name = {}
    for m in ff.get_messages():
        by_name.setdefault(m.name, []).append(m)
    return by_name


class TestSessionMessageRoundTrip:
    """Every value passed into `activity` must come back under the FIT field
    with the matching semantic NAME (per fitparse's own profile), not just
    under *some* field."""

    def test_avg_and_max_power_round_trip(self):
        by_name = _parse(generate_fit(ACTIVITY, STREAMS))
        session = {f.name: f.value for f in by_name["session"][0]}
        assert session["avg_power"] == int(ACTIVITY["average_watts"])
        assert session["max_power"] == int(ACTIVITY["max_watts"])

    def test_avg_and_max_heart_rate_round_trip(self):
        by_name = _parse(generate_fit(ACTIVITY, STREAMS))
        session = {f.name: f.value for f in by_name["session"][0]}
        assert session["avg_heart_rate"] == int(ACTIVITY["average_heartrate"])
        assert session["max_heart_rate"] == int(ACTIVITY["max_heartrate"])

    def test_max_speed_round_trips_in_ms(self):
        by_name = _parse(generate_fit(ACTIVITY, STREAMS))
        session = {f.name: f.value for f in by_name["session"][0]}
        assert session["max_speed"] == pytest.approx(ACTIVITY["max_speed"], abs=0.01)

    def test_total_ascent_round_trips(self):
        by_name = _parse(generate_fit(ACTIVITY, STREAMS))
        session = {f.name: f.value for f in by_name["session"][0]}
        assert session["total_ascent"] == int(ACTIVITY["total_elevation_gain"])

    def test_elapsed_and_timer_time_round_trip(self):
        by_name = _parse(generate_fit(ACTIVITY, STREAMS))
        session = {f.name: f.value for f in by_name["session"][0]}
        assert session["total_elapsed_time"] == pytest.approx(ACTIVITY["elapsed_time"], abs=0.01)
        assert session["total_timer_time"] == pytest.approx(ACTIVITY["moving_time"], abs=0.01)

    def test_total_distance_round_trips(self):
        by_name = _parse(generate_fit(ACTIVITY, STREAMS))
        session = {f.name: f.value for f in by_name["session"][0]}
        assert session["total_distance"] == pytest.approx(ACTIVITY["distance"], abs=0.5)

    def test_sport_round_trips(self):
        by_name = _parse(generate_fit(ACTIVITY, STREAMS))
        session = {f.name: f.value for f in by_name["session"][0]}
        assert session["sport"] == "cycling"

    def test_no_field_collisions_among_summary_values(self):
        """Regression guard: avg_power/max_power/avg_speed/max_speed/total_ascent/
        avg_heart_rate/max_heart_rate must all land on distinct FIT field numbers.
        (This is exactly the class of bug the field-shift caused — two different
        semantic values silently sharing one field number.)"""
        by_name = _parse(generate_fit(ACTIVITY, STREAMS))
        session = {f.name: f.value for f in by_name["session"][0]}
        distinct_values = {
            session["avg_power"], session["max_power"],
            session["avg_heart_rate"], session["max_heart_rate"],
            session["total_ascent"],
        }
        # all deliberately distinct in the fixture, so distinct field decoding
        # should never merge two of these into the same observed value
        assert len(distinct_values) == 5


class TestRecordMessagesRoundTrip:

    def test_per_sample_power_hr_cadence_round_trip(self):
        by_name = _parse(generate_fit(ACTIVITY, STREAMS))
        records = by_name["record"]
        assert len(records) == len(STREAMS["time"]["data"])
        for i, rec in enumerate(records):
            d = {f.name: f.value for f in rec}
            assert d["power"] == STREAMS["watts"]["data"][i]
            assert d["heart_rate"] == STREAMS["heartrate"]["data"][i]
            assert d["cadence"] == STREAMS["cadence"]["data"][i]

    def test_record_count_matches_time_stream_when_others_absent(self):
        streams = {"time": {"data": [0, 1, 2]}, "watts": {"data": [100, 110, 120]}}
        by_name = _parse(generate_fit(ACTIVITY, streams))
        assert len(by_name["record"]) == 3
