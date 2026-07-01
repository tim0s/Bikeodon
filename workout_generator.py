"""
Algorithmic structured-workout generator.

Given a training goal, a target duration, a rider's FTP, and a "hardness"
knob (0.0 = easiest, 1.0 = hardest), produces a warmup / main-set / cooldown
step list expressed as (duration_s, pct_ftp). Reuses training_load's NP/TSS
math to estimate what the planned session would score.
"""
from database import POWER_ZONE_PRESETS
from training_load import compute_np, compute_tss

WARMUP_MIN_S, WARMUP_MAX_S = 300, 900     # 5-15 min
COOLDOWN_MIN_S, COOLDOWN_MAX_S = 180, 480  # 3-8 min

# Fallback when the caller has no zones configured yet (matches database.py's
# own _DEFAULT_POWER_ZONES) — same shape as database.get_zones()'s return value.
_DEFAULT_POWER_ZONES = [
    {"name": name, "max_pct": max_pct, "color": color}
    for _, name, max_pct, color in POWER_ZONE_PRESETS["7zone"]
]


def _zone_for_pct(pct_ftp: float, power_zones: list) -> tuple:
    for zone in power_zones:
        if pct_ftp <= zone["max_pct"]:
            return zone["name"], zone["color"]
    last = power_zones[-1]
    return last["name"], last["color"]


def finalize_steps(steps: list, ftp: float, power_zones: list = None) -> tuple:
    """Round durations, compute watts + zone tagging, and derive planned NP/IF/TSS
    for a raw step list [{label, duration_s, pct_ftp}, ...]. Shared by the algorithmic
    generator and the custom-workout builder so both produce identical step shapes
    and use the same load-estimate math (training_load.compute_np/compute_tss).

    Returns (steps, load_summary) where load_summary has planned_np/planned_if/planned_tss."""
    power_zones = power_zones or _DEFAULT_POWER_ZONES
    for step in steps:
        step["duration_s"] = round(step["duration_s"])
        step["pct_ftp"] = round(step["pct_ftp"], 1)
        step["watts"] = round(step["pct_ftp"] / 100 * ftp)
        step["zone_name"], step["zone_color"] = _zone_for_pct(step["pct_ftp"], power_zones)

    watts_series = [s["watts"] for s in steps for _ in range(s["duration_s"])]
    planned_np = compute_np(watts_series)
    planned_tss = compute_tss(planned_np, len(watts_series), ftp) if planned_np else None

    return steps, {
        "planned_np": round(planned_np) if planned_np else None,
        "planned_if": round(planned_np / ftp, 2) if planned_np else None,
        "planned_tss": round(planned_tss) if planned_tss else None,
    }

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
                      power_zones: list = None,
                      _skip_min_duration_check: bool = False) -> dict:
    """power_zones: [{"name", "max_pct", "color"}, ...] ordered ascending by max_pct —
    same shape as database.get_zones(db_path, uid, "power"). Falls back to the standard
    Coggan 7-zone bands if the caller has none configured yet."""
    if goal not in GOAL_LIBRARY:
        return {"ok": False, "error": "unknown_goal", "message": f"Unknown goal '{goal}'."}
    if not ftp or ftp <= 0:
        return {"ok": False, "error": "no_ftp", "message": "Set your FTP in Settings before generating a workout."}
    power_zones = power_zones or _DEFAULT_POWER_ZONES

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

    steps, load_summary = finalize_steps(steps, ftp, power_zones)

    return {
        "ok": True,
        "goal": goal, "goal_label": goal_cfg["label"],
        "ftp": ftp, "duration_min": duration_min, "hardness": hardness,
        "steps": steps,
        **load_summary,
    }


_STEP_MIN_DURATION_S, _STEP_MAX_DURATION_S = 5, 6 * 3600   # 5s - 6h per step
_STEP_MIN_PCT_FTP, _STEP_MAX_PCT_FTP = 10, 300
_WORKOUT_MAX_DURATION_S = 8 * 3600                          # 8h total, generous ceiling


def build_custom_workout(raw_steps: list, ftp: float, power_zones: list = None,
                          goal_label: str = "Custom") -> dict:
    """Validate and finalize a user-authored step list into the same result shape
    as generate_workout(), so the rest of the app (preview, export, save) doesn't
    need to know whether a workout was generated or hand-built."""
    if not ftp or ftp <= 0:
        return {"ok": False, "error": "no_ftp", "message": "Set your FTP in Settings before building a workout."}
    if not raw_steps:
        return {"ok": False, "error": "bad_input", "message": "Add at least one step."}

    steps = []
    for i, raw in enumerate(raw_steps):
        try:
            duration_s = float(raw.get("duration_s"))
            pct_ftp = float(raw.get("pct_ftp"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_input", "message": f"Step {i + 1} has an invalid duration or %FTP."}
        if not (_STEP_MIN_DURATION_S <= duration_s <= _STEP_MAX_DURATION_S):
            return {"ok": False, "error": "bad_input",
                    "message": f"Step {i + 1}'s duration must be between 5 seconds and 6 hours."}
        if not (_STEP_MIN_PCT_FTP <= pct_ftp <= _STEP_MAX_PCT_FTP):
            return {"ok": False, "error": "bad_input",
                    "message": f"Step {i + 1}'s %FTP must be between {_STEP_MIN_PCT_FTP} and {_STEP_MAX_PCT_FTP}."}
        label = (raw.get("label") or "").strip() or f"Step {i + 1}"
        steps.append({"label": label, "duration_s": duration_s, "pct_ftp": pct_ftp})

    total_s = sum(s["duration_s"] for s in steps)
    if total_s > _WORKOUT_MAX_DURATION_S:
        return {"ok": False, "error": "bad_input", "message": "Total workout duration can't exceed 8 hours."}

    steps, load_summary = finalize_steps(steps, ftp, power_zones)

    return {
        "ok": True,
        "goal": "custom", "goal_label": (goal_label or "Custom").strip() or "Custom",
        "ftp": ftp, "duration_min": round(total_s / 60), "hardness": None,
        "steps": steps,
        **load_summary,
    }
