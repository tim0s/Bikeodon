"""
Tests for training_activity.py — converting recorded live-training samples
into the shapes the existing activity pipeline (upsert_activity/
process_activity/fit_encoder.generate_fit) already expects.
No DB or Flask app required.
"""
import pytest

from training_activity import build_activity_from_samples

STARTED_AT = "2026-01-15T08:00:00.000Z"
UID = 7


def _samples(n=60, power=200, cadence=85, hr=140, speed=30.0):
    return [
        {"t": i, "power": power, "cadence": cadence, "hr": hr, "speed": speed}
        for i in range(n)
    ]


class TestActivitySummary:

    def test_basic_fields(self):
        act, _stream, _fit = build_activity_from_samples(_samples(), "My Ride", STARTED_AT, UID)
        assert act["name"] == "My Ride"
        assert act["sport_type"] == "VirtualRide"
        assert act["start_date"] == STARTED_AT
        assert act["moving_time"] == 59
        assert act["elapsed_time"] == 59

    def test_power_and_hr_averages(self):
        samples = [
            {"t": 0, "power": 100, "cadence": 80, "hr": 120, "speed": 30},
            {"t": 1, "power": 200, "cadence": 80, "hr": 140, "speed": 30},
        ]
        act, _stream, _fit = build_activity_from_samples(samples, "Ride", STARTED_AT, UID)
        assert act["average_watts"] == 150
        assert act["max_watts"] == 200
        assert act["average_heartrate"] == 130
        assert act["max_heartrate"] == 140

    def test_distance_integrated_from_speed(self):
        # 30 km/h for 60 samples (59s of motion) -> ~492m
        act, _stream, _fit = build_activity_from_samples(_samples(n=60, speed=30.0), "Ride", STARTED_AT, UID)
        assert act["distance"] == pytest.approx(491.7, abs=1)

    def test_no_speed_sensor_gives_zero_distance_and_null_speed(self):
        samples = [{"t": i, "power": 200, "cadence": None, "hr": 140, "speed": None} for i in range(10)]
        act, _stream, _fit = build_activity_from_samples(samples, "Ride", STARTED_AT, UID)
        assert act["distance"] == 0
        assert act["average_speed"] is None
        assert act["max_speed"] is None

    def test_missing_hr_and_power_give_null_averages(self):
        samples = [{"t": i, "power": None, "cadence": None, "hr": None, "speed": None} for i in range(10)]
        act, _stream, _fit = build_activity_from_samples(samples, "Ride", STARTED_AT, UID)
        assert act["average_watts"] is None
        assert act["max_watts"] is None
        assert act["average_heartrate"] is None
        assert act["max_heartrate"] is None

    def test_id_is_stable_for_same_inputs(self):
        act1, _, _ = build_activity_from_samples(_samples(), "Ride", STARTED_AT, UID)
        act2, _, _ = build_activity_from_samples(_samples(), "Ride", STARTED_AT, UID)
        assert act1["id"] == act2["id"]

    def test_id_differs_for_different_users(self):
        act1, _, _ = build_activity_from_samples(_samples(), "Ride", STARTED_AT, UID)
        act2, _, _ = build_activity_from_samples(_samples(), "Ride", STARTED_AT, UID + 1)
        assert act1["id"] != act2["id"]

    def test_default_name_when_none_given(self):
        act, _stream, _fit = build_activity_from_samples(_samples(), None, STARTED_AT, UID)
        assert act["name"] == "Training Session"


class TestStreamShape:

    def test_stream_has_expected_keys_and_length(self):
        _act, stream, _fit = build_activity_from_samples(_samples(n=5), "Ride", STARTED_AT, UID)
        assert len(stream) == 5
        for point in stream:
            assert set(point.keys()) == {"lat", "lon", "ele", "hr", "power", "elapsed_secs"}
            assert point["lat"] is None
            assert point["lon"] is None
            assert point["ele"] is None

    def test_stream_carries_power_and_hr_through(self):
        samples = [{"t": 0, "power": 250, "cadence": 90, "hr": 155, "speed": 35}]
        _act, stream, _fit = build_activity_from_samples(samples, "Ride", STARTED_AT, UID)
        assert stream[0]["power"] == 250
        assert stream[0]["hr"] == 155
        assert stream[0]["elapsed_secs"] == 0


class TestFitStreamsShape:

    def test_fit_streams_time_always_present(self):
        _act, _stream, fit_streams = build_activity_from_samples(_samples(n=5), "Ride", STARTED_AT, UID)
        assert fit_streams["time"]["data"] == [0, 1, 2, 3, 4]

    def test_fit_streams_omit_absent_metrics(self):
        samples = [{"t": i, "power": None, "cadence": None, "hr": None, "speed": None} for i in range(5)]
        _act, _stream, fit_streams = build_activity_from_samples(samples, "Ride", STARTED_AT, UID)
        assert fit_streams["heartrate"] == {}
        assert fit_streams["cadence"] == {}
        assert fit_streams["watts"] == {}
        assert fit_streams["distance"] == {}
        assert fit_streams["velocity_smooth"] == {}

    def test_fit_streams_present_metrics_have_matching_length(self):
        _act, _stream, fit_streams = build_activity_from_samples(_samples(n=10), "Ride", STARTED_AT, UID)
        assert len(fit_streams["heartrate"]["data"]) == 10
        assert len(fit_streams["watts"]["data"]) == 10
        assert len(fit_streams["cadence"]["data"]) == 10
        assert len(fit_streams["distance"]["data"]) == 10

    def test_fit_streams_distance_is_cumulative_and_increasing(self):
        _act, _stream, fit_streams = build_activity_from_samples(_samples(n=10, speed=20.0), "Ride", STARTED_AT, UID)
        distances = fit_streams["distance"]["data"]
        assert all(b >= a for a, b in zip(distances, distances[1:]))
        assert distances[-1] > 0


class TestRoundTripThroughFitEncoder:
    """Confirms the produced shapes are actually accepted by the existing
    fit_encoder.generate_fit() — the real integration point in the route."""

    def test_generates_valid_parseable_fit(self):
        import io
        import fitparse
        from fit_encoder import generate_fit

        act, _stream, fit_streams = build_activity_from_samples(_samples(n=30), "Ride", STARTED_AT, UID)
        fit_bytes = generate_fit(act, fit_streams)
        ff = fitparse.FitFile(io.BytesIO(fit_bytes))
        records = list(ff.get_messages("record"))
        assert len(records) == 30
        first = {f.name: f.value for f in records[0]}
        assert first["power"] == 200
        assert first["heart_rate"] == 140
