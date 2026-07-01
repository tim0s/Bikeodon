"""Tests for zwo_writer.py — Zwift .zwo (workout XML) writer."""
import xml.etree.ElementTree as ET

import pytest

from zwo_writer import build_zwo_workout
from workout_generator import generate_workout

FTP = 250


def _sample_workout():
    result = generate_workout("vo2max", 45, 0.6, FTP)
    assert result["ok"]
    return result


class TestXmlStructure:

    def test_output_is_well_formed_xml(self):
        xml_str = build_zwo_workout(_sample_workout())
        ET.fromstring(xml_str)  # raises on malformed XML

    def test_root_tag_and_metadata(self):
        source = _sample_workout()
        root = ET.fromstring(build_zwo_workout(source))
        assert root.tag == "workout_file"
        assert root.find("author").text == "Bikeodon"
        assert root.find("sportType").text == "bike"
        assert source["goal_label"] in root.find("name").text

    def test_step_count_matches_source(self):
        source = _sample_workout()
        root = ET.fromstring(build_zwo_workout(source))
        elements = list(root.find("workout"))
        assert len(elements) == len(source["steps"])

    def test_total_duration_matches_source(self):
        source = _sample_workout()
        root = ET.fromstring(build_zwo_workout(source))
        elements = list(root.find("workout"))
        total = sum(int(e.get("Duration")) for e in elements)
        assert total == sum(s["duration_s"] for s in source["steps"])


class TestStepMapping:

    @pytest.mark.parametrize("label,expected_tag", [
        ("Warmup", "Warmup"),
        ("Cooldown", "Cooldown"),
        ("Recovery", "SteadyState"),
        ("VO2 Max 1", "SteadyState"),
    ])
    def test_label_maps_to_expected_element_tag(self, label, expected_tag):
        workout = {"goal_label": "Test", "duration_min": 10,
                   "steps": [{"label": label, "duration_s": 60, "pct_ftp": 80}]}
        root = ET.fromstring(build_zwo_workout(workout))
        el = list(root.find("workout"))[0]
        assert el.tag == expected_tag

    def test_steady_state_power_is_fraction_of_ftp(self):
        workout = {"goal_label": "Test", "duration_min": 10,
                   "steps": [{"label": "Threshold 1", "duration_s": 60, "pct_ftp": 95}]}
        root = ET.fromstring(build_zwo_workout(workout))
        el = list(root.find("workout"))[0]
        assert el.get("Power") == "0.95"

    def test_warmup_uses_power_low_high_not_power(self):
        workout = {"goal_label": "Test", "duration_min": 10,
                   "steps": [{"label": "Warmup", "duration_s": 180, "pct_ftp": 55}]}
        root = ET.fromstring(build_zwo_workout(workout))
        el = list(root.find("workout"))[0]
        assert el.get("PowerLow") == el.get("PowerHigh") == "0.55"
        assert el.get("Power") is None
