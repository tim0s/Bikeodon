"""
Tests for fit_writer.py — hand-rolled binary FIT workout writer.

Verified primarily by round-tripping through fitparse (already a project
dependency, read-only) since that's the same technique that caught real
field-number/encoding bugs during development.
"""
import io
import struct

import fitparse
import pytest

from fit_writer import build_fit_workout, _fit_crc16
from workout_generator import generate_workout

FTP = 250


def _sample_workout():
    result = generate_workout("sweet_spot", 60, 0.5, FTP)
    assert result["ok"]
    return result


def _parse(fit_bytes):
    ff = fitparse.FitFile(io.BytesIO(fit_bytes))
    by_name = {}
    for m in ff.get_messages():
        by_name.setdefault(m.name, []).append(m)
    return by_name


class TestFileStructure:

    def test_header_has_fit_signature(self):
        fit_bytes = build_fit_workout(_sample_workout())
        assert fit_bytes[8:12] == b".FIT"

    def test_header_size_byte_is_12(self):
        fit_bytes = build_fit_workout(_sample_workout())
        assert fit_bytes[0] == 12

    def test_data_size_matches_body_length(self):
        fit_bytes = build_fit_workout(_sample_workout())
        declared_size = struct.unpack("<I", fit_bytes[4:8])[0]
        assert declared_size == len(fit_bytes) - 12 - 2  # minus header and trailing CRC

    def test_trailing_crc_is_valid(self):
        fit_bytes = build_fit_workout(_sample_workout())
        body, trailing_crc = fit_bytes[:-2], struct.unpack("<H", fit_bytes[-2:])[0]
        assert _fit_crc16(body) == trailing_crc

    def test_fitparse_can_parse_without_raising(self):
        fit_bytes = build_fit_workout(_sample_workout())
        fitparse.FitFile(io.BytesIO(fit_bytes)).parse()


class TestMessageContent:

    def test_file_id_marks_type_workout(self):
        by_name = _parse(build_fit_workout(_sample_workout()))
        fid = {f.name: f.value for f in by_name["file_id"][0]}
        assert fid["type"] == "workout"

    def test_workout_message_step_count_and_name(self):
        source = _sample_workout()
        by_name = _parse(build_fit_workout(source))
        wkt = {f.name: f.value for f in by_name["workout"][0]}
        assert wkt["sport"] == "cycling"
        assert wkt["num_valid_steps"] == len(source["steps"])
        assert wkt["wkt_name"] == source["goal_label"]

    def test_workout_steps_round_trip_duration_and_power(self):
        source = _sample_workout()
        by_name = _parse(build_fit_workout(source))
        parsed_steps = by_name["workout_step"]
        assert len(parsed_steps) == len(source["steps"])
        for parsed, orig in zip(parsed_steps, source["steps"]):
            d = {f.name: f.value for f in parsed}
            assert d["duration_time"] == pytest.approx(orig["duration_s"], abs=1)
            assert d["custom_target_power_low"] - 1000 == orig["watts"]
            assert d["custom_target_power_high"] - 1000 == orig["watts"]
            assert d["target_type"] == "power"

    def test_message_index_is_sequential(self):
        by_name = _parse(build_fit_workout(_sample_workout()))
        indices = [
            next(f.value for f in m if f.name == "message_index")
            for m in by_name["workout_step"]
        ]
        assert indices == list(range(len(indices)))

    @pytest.mark.parametrize("label,expected", [
        ("Warmup", "warmup"),
        ("Cooldown", "cooldown"),
        ("Recovery", "rest"),
        ("Sweet Spot 1", "active"),
    ])
    def test_step_intensity_mapping(self, label, expected):
        workout = {
            "goal_label": "Test",
            "steps": [{"label": label, "duration_s": 60, "watts": 100}],
        }
        by_name = _parse(build_fit_workout(workout))
        step = {f.name: f.value for f in by_name["workout_step"][0]}
        assert step["intensity"] == expected


class TestEdgeCases:

    def test_single_step_workout(self):
        workout = {"goal_label": "Solo", "steps": [{"label": "Warmup", "duration_s": 300, "watts": 120}]}
        by_name = _parse(build_fit_workout(workout))
        assert len(by_name["workout_step"]) == 1

    def test_step_name_longer_than_field_size_is_truncated_not_corrupted(self):
        long_label = "A very long interval label that exceeds the field size"
        workout = {"goal_label": "Test", "steps": [{"label": long_label, "duration_s": 60, "watts": 100}]}
        fit_bytes = build_fit_workout(workout)
        # must still be parseable and produce exactly one step
        by_name = _parse(fit_bytes)
        assert len(by_name["workout_step"]) == 1
