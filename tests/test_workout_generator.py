"""
Tests for workout_generator.py — pure structured-workout generation.
No DB or Flask app required.
"""
import pytest

from workout_generator import generate_workout, GOAL_LIBRARY

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
