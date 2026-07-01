"""
Tests for workout_generator.py — pure structured-workout generation.
No DB or Flask app required.
"""
import pytest

from workout_generator import generate_workout, build_custom_workout, GOAL_LIBRARY

FTP = 250
ALL_GOALS = list(GOAL_LIBRARY.keys())


class TestFeasibilityChecks:

    def test_endurance_too_short_is_infeasible(self):
        result = generate_workout("endurance", 30, 0.5, FTP)
        assert result["ok"] is False
        assert result["error"] == "infeasible"
        assert "min_duration_min" in result

    def test_endurance_at_minimum_is_feasible(self):
        result = generate_workout("endurance", GOAL_LIBRARY["endurance"]["min_duration_min"], 0.5, FTP)
        assert result["ok"] is True

    def test_vo2max_too_short_for_min_intervals_is_infeasible(self):
        # 30 min is >= vo2max's table min_duration_min, but doesn't fit 4 intervals at hardness=1
        result = generate_workout("vo2max", 30, 1.0, FTP)
        assert result["ok"] is False
        assert result["error"] == "infeasible"

    def test_sprints_needs_at_least_six_intervals(self):
        result = generate_workout("sprints", 30, 0.0, FTP)
        assert result["ok"] is False
        assert "6" in result["message"] or result.get("min_duration_min")

    def test_unknown_goal_is_rejected(self):
        result = generate_workout("does_not_exist", 60, 0.5, FTP)
        assert result["ok"] is False
        assert result["error"] == "unknown_goal"

    @pytest.mark.parametrize("ftp", [0, None, -5])
    def test_missing_or_invalid_ftp_is_rejected(self, ftp):
        result = generate_workout("endurance", 60, 0.5, ftp)
        assert result["ok"] is False
        assert result["error"] == "no_ftp"

    def test_infeasible_response_suggests_a_minimum_duration(self):
        result = generate_workout("threshold", 35, 0.5, FTP)
        if not result["ok"]:
            assert result.get("min_duration_min") is None or result["min_duration_min"] > 35


class TestGeneratedStepShape:

    @pytest.mark.parametrize("goal", ALL_GOALS)
    def test_feasible_session_totals_requested_duration(self, goal):
        duration_min = 90  # long enough to be feasible for every goal at any hardness
        result = generate_workout(goal, duration_min, 0.5, FTP)
        assert result["ok"] is True
        total_s = sum(s["duration_s"] for s in result["steps"])
        assert total_s == duration_min * 60

    @pytest.mark.parametrize("goal", ALL_GOALS)
    def test_steps_have_expected_fields(self, goal):
        result = generate_workout(goal, 90, 0.5, FTP)
        assert result["ok"] is True
        for step in result["steps"]:
            assert isinstance(step["label"], str)
            assert step["duration_s"] > 0
            assert step["pct_ftp"] > 0
            assert step["watts"] == round(step["pct_ftp"] / 100 * FTP)
            assert isinstance(step["zone_name"], str)
            assert step["zone_color"].startswith("#")


    def test_session_starts_with_warmup_and_ends_with_cooldown(self):
        result = generate_workout("sweet_spot", 90, 0.5, FTP)
        assert result["ok"] is True
        assert result["steps"][0]["label"] == "Warmup"
        assert result["steps"][-1]["label"] == "Cooldown"

    def test_interval_goal_alternates_work_and_recovery(self):
        result = generate_workout("sweet_spot", 90, 0.3, FTP)
        assert result["ok"] is True
        labels = [s["label"] for s in result["steps"]]
        assert any(l.startswith("Sweet Spot") for l in labels)
        assert "Recovery" in labels


class TestHardnessEffect:

    def test_higher_hardness_increases_work_intensity_for_steady_goal(self):
        easy = generate_workout("endurance", 90, 0.0, FTP)
        hard = generate_workout("endurance", 90, 1.0, FTP)
        assert easy["ok"] and hard["ok"]
        easy_main = next(s for s in easy["steps"] if s["label"] == "Endurance")
        hard_main = next(s for s in hard["steps"] if s["label"] == "Endurance")
        assert hard_main["pct_ftp"] > easy_main["pct_ftp"]

    def test_higher_hardness_shortens_recovery_for_interval_goal(self):
        easy = generate_workout("sweet_spot", 90, 0.0, FTP)
        hard = generate_workout("sweet_spot", 90, 1.0, FTP)
        assert easy["ok"] and hard["ok"]
        easy_rest = next(s for s in easy["steps"] if s["label"] == "Recovery")
        hard_rest = next(s for s in hard["steps"] if s["label"] == "Recovery")
        assert hard_rest["duration_s"] < easy_rest["duration_s"]

    def test_hardness_is_clamped_to_0_1_range(self):
        below = generate_workout("endurance", 90, -5.0, FTP)
        above = generate_workout("endurance", 90, 5.0, FTP)
        assert below["ok"] and above["ok"]
        assert below["hardness"] == 0.0
        assert above["hardness"] == 1.0


class TestPlannedLoadEstimate:

    def test_planned_np_if_tss_are_populated_and_sane(self):
        result = generate_workout("sweet_spot", 90, 0.5, FTP)
        assert result["ok"] is True
        assert result["planned_np"] is not None
        assert 0 < result["planned_if"] < 1.2
        assert result["planned_tss"] > 0

    def test_harder_session_has_higher_intensity_factor(self):
        easy = generate_workout("threshold", 90, 0.0, FTP)
        hard = generate_workout("threshold", 90, 1.0, FTP)
        assert easy["ok"] and hard["ok"]
        assert hard["planned_if"] >= easy["planned_if"]


FIVE_ZONE = [
    {"name": "Z1 Recovery", "max_pct": 55, "color": "#5b9bd5"},
    {"name": "Z2 Endurance", "max_pct": 75, "color": "#70ad47"},
    {"name": "Z3 Tempo", "max_pct": 90, "color": "#ffc000"},
    {"name": "Z4 Threshold", "max_pct": 105, "color": "#ff7043"},
    {"name": "Z5 VO2 Max", "max_pct": 999, "color": "#d32f2f"},
]


class TestZoneTagging:

    def test_defaults_to_standard_7zone_bands_when_none_given(self):
        result = generate_workout("endurance", 90, 0.0, FTP)
        assert result["ok"] is True
        assert result["steps"][0]["zone_name"] == "Z1 Recovery"

    def test_vo2max_work_interval_maps_to_a_high_zone(self):
        result = generate_workout("vo2max", 90, 1.0, FTP)
        assert result["ok"] is True
        work_step = next(s for s in result["steps"] if s["label"].startswith("VO2 Max"))
        assert work_step["zone_name"] in ("Z5 VO2 Max", "Z6 Anaerobic")

    def test_sprint_step_maps_to_top_zone_of_7zone_model(self):
        result = generate_workout("sprints", 90, 1.0, FTP)
        assert result["ok"] is True
        work_step = next(s for s in result["steps"] if s["label"].startswith("Sprints"))
        assert work_step["zone_name"] in ("Z6 Anaerobic", "Z7 Neuromuscular")

    def test_custom_5zone_model_is_respected(self):
        result = generate_workout("sprints", 90, 1.0, FTP, FIVE_ZONE)
        assert result["ok"] is True
        work_step = next(s for s in result["steps"] if s["label"].startswith("Sprints"))
        # a 5-zone model has no dedicated top-end band, so a sprint effort
        # (>105% FTP) still lands in the same top zone as a VO2 max effort
        assert work_step["zone_name"] == "Z5 VO2 Max"

    def test_5zone_vs_7zone_diverge_for_a_max_effort_step(self):
        seven = generate_workout("sprints", 90, 1.0, FTP)
        five = generate_workout("sprints", 90, 1.0, FTP, FIVE_ZONE)
        work_seven = next(s for s in seven["steps"] if s["label"].startswith("Sprints"))
        work_five = next(s for s in five["steps"] if s["label"].startswith("Sprints"))
        assert work_seven["zone_name"] != work_five["zone_name"]

    def test_empty_zone_list_falls_back_to_default(self):
        result = generate_workout("endurance", 90, 0.0, FTP, [])
        assert result["ok"] is True
        assert result["steps"][0]["zone_name"] == "Z1 Recovery"


class TestBuildCustomWorkout:

    def test_valid_steps_produce_same_shape_as_generate_workout(self):
        result = build_custom_workout(
            [{"label": "Warmup", "duration_s": 300, "pct_ftp": 50},
             {"label": "Push", "duration_s": 600, "pct_ftp": 110}],
            FTP,
        )
        assert result["ok"] is True
        assert result["goal"] == "custom"
        assert result["goal_label"] == "Custom"
        assert result["hardness"] is None
        assert result["duration_min"] == 15
        for step in result["steps"]:
            assert set(step.keys()) == {"label", "duration_s", "pct_ftp", "watts", "zone_name", "zone_color"}

    def test_computes_planned_np_if_tss(self):
        result = build_custom_workout(
            [{"label": "Steady", "duration_s": 1200, "pct_ftp": 90}], FTP,
        )
        assert result["ok"] is True
        assert result["planned_np"] == pytest.approx(225, abs=1)
        assert result["planned_if"] == pytest.approx(0.9, abs=0.01)
        assert result["planned_tss"] is not None

    def test_custom_goal_label_is_used(self):
        result = build_custom_workout(
            [{"label": "X", "duration_s": 60, "pct_ftp": 100}], FTP, goal_label="Climbing repeats",
        )
        assert result["ok"] is True
        assert result["goal_label"] == "Climbing repeats"

    def test_blank_goal_label_falls_back_to_custom(self):
        result = build_custom_workout(
            [{"label": "X", "duration_s": 60, "pct_ftp": 100}], FTP, goal_label="   ",
        )
        assert result["ok"] is True
        assert result["goal_label"] == "Custom"

    def test_missing_label_gets_a_default(self):
        result = build_custom_workout(
            [{"label": "", "duration_s": 60, "pct_ftp": 100}], FTP,
        )
        assert result["ok"] is True
        assert result["steps"][0]["label"] == "Step 1"

    def test_empty_step_list_is_rejected(self):
        result = build_custom_workout([], FTP)
        assert result["ok"] is False
        assert result["error"] == "bad_input"

    def test_no_ftp_is_rejected(self):
        result = build_custom_workout([{"label": "X", "duration_s": 60, "pct_ftp": 100}], 0)
        assert result["ok"] is False
        assert result["error"] == "no_ftp"

    @pytest.mark.parametrize("bad_step", [
        {"label": "X", "duration_s": 1, "pct_ftp": 100},        # too short
        {"label": "X", "duration_s": 999999, "pct_ftp": 100},   # too long
        {"label": "X", "duration_s": 60, "pct_ftp": 1},         # too low
        {"label": "X", "duration_s": 60, "pct_ftp": 9999},      # too high
        {"label": "X", "duration_s": "abc", "pct_ftp": 100},    # bad type
    ])
    def test_out_of_range_or_malformed_steps_are_rejected(self, bad_step):
        result = build_custom_workout([bad_step], FTP)
        assert result["ok"] is False
        assert result["error"] == "bad_input"

    def test_total_duration_over_8_hours_is_rejected(self):
        result = build_custom_workout(
            [{"label": "X", "duration_s": 9 * 3600, "pct_ftp": 100}], FTP,
        )
        assert result["ok"] is False
        assert result["error"] == "bad_input"

    def test_respects_custom_power_zones(self):
        result = build_custom_workout(
            [{"label": "Max", "duration_s": 60, "pct_ftp": 200}], FTP, FIVE_ZONE,
        )
        assert result["ok"] is True
        assert result["steps"][0]["zone_name"] == "Z5 VO2 Max"
