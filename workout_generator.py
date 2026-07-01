"""
Algorithmic structured-workout generator.

Given a training goal, a target duration, a rider's FTP, and a "hardness"
knob (0.0 = easiest, 1.0 = hardest), produces a warmup / main-set / cooldown
step list expressed as (duration_s, pct_ftp). Reuses training_load's NP/TSS
math to estimate what the planned session would score.
"""
from training_load import compute_np, compute_tss

WARMUP_MIN_S, WARMUP_MAX_S = 300, 900     # 5-15 min
COOLDOWN_MIN_S, COOLDOWN_MAX_S = 180, 480  # 3-8 min

# pct_lo/pct_hi: %FTP band picked via hardness (lerp).
# For "intervals" goals, rest_ratio_easy/hard scale rest_dur = work_dur * ratio,
# except sprints which use a fixed rest duration range in seconds.
GOAL_LIBRARY = {
    "endurance": {
        "label": "Endurance", "kind": "steady",
        "pct_lo": 56, "pct_hi": 75, "min_duration_min": 45,
    },
    "tempo": {
        "label": "Tempo", "kind": "steady",
        "pct_lo": 76, "pct_hi": 90, "min_duration_min": 40,
    },
    "sweet_spot": {
        "label": "Sweet Spot", "kind": "intervals",
        "pct_lo": 88, "pct_hi": 94, "work_dur_s": 600,
        "rest_pct": 50, "rest_ratio_easy": 1.0, "rest_ratio_hard": 0.5,
        "min_intervals": 2, "min_duration_min": 40,
    },
    "threshold": {
        "label": "Threshold", "kind": "intervals",
        "pct_lo": 95, "pct_hi": 105, "work_dur_s": 600,
        "rest_pct": 50, "rest_ratio_easy": 1.0, "rest_ratio_hard": 0.5,
        "min_intervals": 2, "min_duration_min": 35,
    },
    "vo2max": {
        "label": "VO2 Max", "kind": "intervals",
        "pct_lo": 106, "pct_hi": 120, "work_dur_s": 240,
        "rest_pct": 45, "rest_ratio_easy": 1.5, "rest_ratio_hard": 0.75,
        "min_intervals": 4, "min_duration_min": 30,
    },
    "sprints": {
        "label": "Sprints", "kind": "intervals",
        "pct_lo": 150, "pct_hi": 200, "work_dur_s": 25,
        "rest_pct": 35, "rest_dur_easy_s": 300, "rest_dur_hard_s": 180,
        "min_intervals": 6, "min_duration_min": 30,
    },
}


def _lerp(lo, hi, t):
    return lo + (hi - lo) * t


def _rest_dur_s(goal_cfg, work_dur_s, hardness):
    if "rest_dur_easy_s" in goal_cfg:
        return _lerp(goal_cfg["rest_dur_easy_s"], goal_cfg["rest_dur_hard_s"], hardness)
    ratio = _lerp(goal_cfg["rest_ratio_easy"], goal_cfg["rest_ratio_hard"], hardness)
    return work_dur_s * ratio


def _warmup_steps(warmup_s):
    third = warmup_s / 3
    return [
        {"label": "Warmup", "duration_s": third, "pct_ftp": 40},
        {"label": "Warmup", "duration_s": third, "pct_ftp": 55},
        {"label": "Warmup", "duration_s": warmup_s - 2 * third, "pct_ftp": 70},
    ]


def _min_feasible_duration_min(goal_cfg, hardness):
    """Smallest whole-minute duration that satisfies both feasibility checks."""
    for candidate_min in range(goal_cfg["min_duration_min"], 241):
        result = generate_workout(
            goal_cfg["_id"], candidate_min, hardness, ftp=200,
            _skip_min_duration_check=True,
        )
        if result["ok"]:
            return candidate_min
    return None


def generate_workout(goal: str, duration_min: int, hardness: float, ftp: float,
                      _skip_min_duration_check: bool = False) -> dict:
    if goal not in GOAL_LIBRARY:
        return {"ok": False, "error": "unknown_goal", "message": f"Unknown goal '{goal}'."}
    if not ftp or ftp <= 0:
        return {"ok": False, "error": "no_ftp", "message": "Set your FTP in Settings before generating a workout."}

    hardness = max(0.0, min(1.0, hardness))
    goal_cfg = dict(GOAL_LIBRARY[goal])
    goal_cfg["_id"] = goal
    total_s = duration_min * 60

    if not _skip_min_duration_check and duration_min < goal_cfg["min_duration_min"]:
        return {
            "ok": False, "error": "infeasible",
            "message": (
                f"{goal_cfg['label']} needs at least {goal_cfg['min_duration_min']} min "
                f"for a meaningful session."
            ),
            "min_duration_min": goal_cfg["min_duration_min"],
        }

    warmup_s = max(WARMUP_MIN_S, min(WARMUP_MAX_S, 0.15 * total_s))
    cooldown_s = max(COOLDOWN_MIN_S, min(COOLDOWN_MAX_S, 0.08 * total_s))
    main_set_s = total_s - warmup_s - cooldown_s

    if main_set_s <= 0:
        return {
            "ok": False, "error": "infeasible",
            "message": f"{duration_min} min isn't enough time for warmup, a main set, and cooldown.",
            "min_duration_min": goal_cfg["min_duration_min"],
        }

    steps = _warmup_steps(warmup_s)

    if goal_cfg["kind"] == "steady":
        pct = _lerp(goal_cfg["pct_lo"], goal_cfg["pct_hi"], hardness)
        steps.append({"label": goal_cfg["label"], "duration_s": main_set_s, "pct_ftp": pct})
    else:
        work_pct = _lerp(goal_cfg["pct_lo"], goal_cfg["pct_hi"], hardness)
        work_dur_s = goal_cfg["work_dur_s"]
        rest_dur_s = _rest_dur_s(goal_cfg, work_dur_s, hardness)
        block_s = work_dur_s + rest_dur_s
        num_intervals = int(main_set_s // block_s)

        if num_intervals < goal_cfg["min_intervals"]:
            if _skip_min_duration_check:
                return {"ok": False, "error": "infeasible"}
            min_min = _min_feasible_duration_min(goal_cfg, hardness)
            suggestion = (
                f"try at least {min_min} min, or lower the hardness to shorten recoveries"
                if min_min else "try a longer duration or lower hardness"
            )
            return {
                "ok": False, "error": "infeasible",
                "message": (
                    f"{duration_min} min only fits {num_intervals} {goal_cfg['label']} "
                    f"interval(s) (need {goal_cfg['min_intervals']}); {suggestion}."
                ),
                "min_duration_min": min_min,
            }

        leftover_s = main_set_s - num_intervals * block_s
        for i in range(num_intervals):
            steps.append({"label": f"{goal_cfg['label']} {i + 1}", "duration_s": work_dur_s, "pct_ftp": work_pct})
            if i < num_intervals - 1:
                steps.append({"label": "Recovery", "duration_s": rest_dur_s, "pct_ftp": goal_cfg["rest_pct"]})
            else:
                cooldown_s += rest_dur_s + leftover_s

    steps.append({"label": "Cooldown", "duration_s": cooldown_s, "pct_ftp": 50})

    for step in steps:
        step["duration_s"] = round(step["duration_s"])
        step["pct_ftp"] = round(step["pct_ftp"], 1)
        step["watts"] = round(step["pct_ftp"] / 100 * ftp)

    watts_series = [s["watts"] for s in steps for _ in range(s["duration_s"])]
    planned_np = compute_np(watts_series)
    planned_tss = compute_tss(planned_np, len(watts_series), ftp) if planned_np else None

    return {
        "ok": True,
        "goal": goal, "goal_label": goal_cfg["label"],
        "ftp": ftp, "duration_min": duration_min, "hardness": hardness,
        "steps": steps,
        "planned_np": round(planned_np) if planned_np else None,
        "planned_if": round(planned_np / ftp, 2) if planned_np else None,
        "planned_tss": round(planned_tss) if planned_tss else None,
    }
