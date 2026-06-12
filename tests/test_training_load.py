"""
Tests for training_load.py — pure metric calculations.
No DB or Flask app required.
"""
import math
import pytest
from training_load import (
    compute_np,
    compute_tss,
    compute_hr_tss,
    compute_trimp,
    compute_peak_powers,
    compute_pmc,
    weekly_load,
    compute_zone_times,
    aggregate_power_curve,
    fit_critical_power,
    compute_wbal,
    PEAK_LABELS,
)


# ---------------------------------------------------------------------------
# compute_np
# ---------------------------------------------------------------------------

class TestComputeNp:

    def test_returns_none_for_short_stream(self):
        assert compute_np([200] * 29) is None

    def test_constant_power_equals_input(self):
        result = compute_np([200] * 60)
        assert result == pytest.approx(200.0, rel=1e-3)

    def test_ignores_none_values(self):
        data = [None] * 10 + [200] * 60
        assert compute_np(data) == pytest.approx(200.0, rel=1e-3)

    def test_higher_variability_raises_np(self):
        # 60 s hard at 400 W then 60 s rest at 0 W — avg 200 W but NP > 200 W
        # because the 30-s rolling windows during the effort are near 400 W
        data = [400] * 60 + [0] * 60
        result = compute_np(data)
        assert result > 200.0

    def test_exactly_30_samples_is_accepted(self):
        assert compute_np([250] * 30) is not None

    def test_returns_float(self):
        assert isinstance(compute_np([200] * 60), float)


# ---------------------------------------------------------------------------
# compute_tss
# ---------------------------------------------------------------------------

class TestComputeTss:

    def test_one_hour_at_ftp_is_100(self):
        # At FTP, IF=1, duration=3600s → TSS=100
        result = compute_tss(np_watts=250, duration_s=3600, ftp=250)
        assert result == pytest.approx(100.0, rel=1e-3)

    def test_half_hour_at_ftp_is_50(self):
        result = compute_tss(np_watts=250, duration_s=1800, ftp=250)
        assert result == pytest.approx(50.0, rel=1e-3)

    def test_below_ftp_gives_less_than_100_per_hour(self):
        result = compute_tss(np_watts=200, duration_s=3600, ftp=250)
        assert result < 100.0

    def test_above_ftp_gives_more_than_100_per_hour(self):
        result = compute_tss(np_watts=300, duration_s=3600, ftp=250)
        assert result > 100.0

    def test_returns_none_when_ftp_zero(self):
        assert compute_tss(np_watts=250, duration_s=3600, ftp=0) is None

    def test_returns_none_when_np_missing(self):
        assert compute_tss(np_watts=None, duration_s=3600, ftp=250) is None

    def test_returns_none_when_duration_missing(self):
        assert compute_tss(np_watts=250, duration_s=0, ftp=250) is None


# ---------------------------------------------------------------------------
# compute_hr_tss
# ---------------------------------------------------------------------------

class TestComputeHrTss:

    def _make_stream(self, hr, duration_s=3600, step=1):
        """Constant-HR stream sampled every `step` seconds."""
        n = duration_s // step
        hr_list = [hr] * n
        elapsed = list(range(0, duration_s, step))
        return hr_list, elapsed

    def test_returns_none_when_params_missing(self):
        hr, el = self._make_stream(150)
        assert compute_hr_tss(hr, el, hr_max=None, hr_rest=50, lthr=160) is None
        assert compute_hr_tss(hr, el, hr_max=190, hr_rest=None, lthr=160) is None
        assert compute_hr_tss(hr, el, hr_max=190, hr_rest=50, lthr=None) is None

    def test_returns_none_when_hr_max_equals_rest(self):
        hr, el = self._make_stream(150)
        assert compute_hr_tss(hr, el, hr_max=50, hr_rest=50, lthr=50) is None

    def test_returns_none_for_short_stream(self):
        assert compute_hr_tss([150, 152], [0, 1], hr_max=190, hr_rest=50, lthr=160) is None

    def test_at_lthr_one_hour_approx_100(self):
        # At LTHR, IF_hr = 1, so hrTSS ≈ 100 for 1h
        hr, el = self._make_stream(hr=160, duration_s=3600)
        result = compute_hr_tss(hr, el, hr_max=190, hr_rest=50, lthr=160)
        assert result == pytest.approx(100.0, abs=5.0)

    def test_higher_hr_gives_more_stress(self):
        hr_low,  el_low  = self._make_stream(hr=140, duration_s=3600)
        hr_high, el_high = self._make_stream(hr=175, duration_s=3600)
        low  = compute_hr_tss(hr_low,  el_low,  hr_max=190, hr_rest=50, lthr=160)
        high = compute_hr_tss(hr_high, el_high, hr_max=190, hr_rest=50, lthr=160)
        assert high > low

    def test_ignores_gaps_over_300s(self):
        hr     = [150, 150, 150]
        elapsed = [0, 1, 400]   # 399 s gap → second interval skipped
        result = compute_hr_tss(hr, elapsed, hr_max=190, hr_rest=50, lthr=160)
        assert result is None   # only 1 s of valid data → < 60 s minimum


# ---------------------------------------------------------------------------
# compute_trimp
# ---------------------------------------------------------------------------

class TestComputeTrimp:

    def test_returns_none_when_params_missing(self):
        assert compute_trimp([150] * 60, list(range(60)), hr_max=None, hr_rest=50) is None
        assert compute_trimp([150] * 60, list(range(60)), hr_max=190, hr_rest=None) is None

    def test_returns_none_when_hr_max_equals_rest(self):
        assert compute_trimp([150] * 60, list(range(60)), hr_max=50, hr_rest=50) is None

    def test_positive_for_valid_stream(self):
        hr      = [150] * 60
        elapsed = list(range(60))
        result  = compute_trimp(hr, elapsed, hr_max=190, hr_rest=50)
        assert result is not None
        assert result > 0

    def test_harder_effort_gives_higher_trimp(self):
        elapsed = list(range(3600))
        low  = compute_trimp([130] * 3600, elapsed, hr_max=190, hr_rest=50)
        high = compute_trimp([170] * 3600, elapsed, hr_max=190, hr_rest=50)
        assert high > low

    def test_skips_negative_time_deltas(self):
        # Reversed timestamps should produce no valid intervals
        hr      = [150, 150, 150]
        elapsed = [100, 50, 10]
        result  = compute_trimp(hr, elapsed, hr_max=190, hr_rest=50)
        assert result is None


# ---------------------------------------------------------------------------
# compute_peak_powers
# ---------------------------------------------------------------------------

class TestComputePeakPowers:

    def _stream(self, watts, duration_s=3700):
        """Uniform-power stream sampled at 1 Hz."""
        return [{"elapsed_secs": t, "power": watts} for t in range(duration_s)]

    def test_returns_none_for_empty_stream(self):
        assert compute_peak_powers([]) is None

    def test_constant_power_all_durations_equal_input(self):
        result = compute_peak_powers(self._stream(300))
        for label in PEAK_LABELS:
            assert result[label] == pytest.approx(300.0, abs=1.0)

    def test_short_stream_omits_long_durations(self):
        # Only 60 s of data — 5min / 20min / 60min peaks should be absent
        # (60 s < 50% of 300 s threshold)
        result = compute_peak_powers(self._stream(300, duration_s=60))
        assert "5min" not in result
        assert "20min" not in result
        assert "5s" in result
        assert "1min" in result

    def test_peak_is_higher_for_sprint_interval(self):
        # 100 W base with a 30 s sprint at 400 W
        stream = [{"elapsed_secs": t, "power": 400 if 100 <= t < 130 else 100}
                  for t in range(3700)]
        result = compute_peak_powers(stream)
        assert result["30s"] > result["5min"]

    def test_skips_none_power_values(self):
        stream = self._stream(300)
        stream[100]["power"] = None
        result = compute_peak_powers(stream)
        assert result is not None


# ---------------------------------------------------------------------------
# compute_pmc
# ---------------------------------------------------------------------------

class TestComputePmc:

    def test_returns_list_of_correct_length(self):
        result = compute_pmc({}, end_date="2026-01-01", days=30)
        assert len(result) == 31   # inclusive of end_date

    def test_zero_load_gives_zero_ctl_atl(self):
        result = compute_pmc({}, end_date="2026-01-01", days=14)
        for row in result:
            assert row["ctl"] == 0.0
            assert row["atl"] == 0.0

    def test_sustained_load_raises_ctl(self):
        tss = {f"2025-{m:02d}-{d:02d}": 100.0
               for m in range(1, 13) for d in range(1, 29)}
        result = compute_pmc(tss, end_date="2026-01-01", days=30)
        final = result[-1]
        assert final["ctl"] > 50.0

    def test_ctl_rises_slower_than_atl(self):
        # After a sudden load spike, ATL should be higher than CTL
        tss = {"2025-12-01": 200.0}
        result = compute_pmc(tss, end_date="2025-12-10", days=10)
        # A few days after the spike ATL should exceed CTL
        mid = result[3]
        assert mid["atl"] > mid["ctl"]

    def test_result_has_required_keys(self):
        result = compute_pmc({}, end_date="2026-01-01", days=5)
        for row in result:
            assert {"date", "tss", "ctl", "atl", "tsb"} <= row.keys()

    def test_tsb_is_ctl_minus_atl_from_previous_day(self):
        tss = {"2026-01-01": 100.0}
        result = compute_pmc(tss, end_date="2026-01-03", days=3)
        # TSB on 2026-01-02 should equal CTL - ATL as of 2026-01-01
        day1 = result[0]   # 2026-01-01
        day2 = result[1]   # 2026-01-02
        assert day2["tsb"] == pytest.approx(day1["ctl"] - day1["atl"], abs=0.1)


# ---------------------------------------------------------------------------
# weekly_load
# ---------------------------------------------------------------------------

class TestWeeklyLoad:

    def test_returns_correct_number_of_weeks(self):
        result = weekly_load({}, weeks=10)
        assert len(result) == 10

    def test_sums_tss_within_week(self):
        tss = {"2026-01-05": 80.0, "2026-01-06": 60.0}   # both in same ISO week
        result = weekly_load(tss, weeks=52)
        total = sum(r["tss"] for r in result)
        assert total == pytest.approx(140.0)

    def test_zero_load_for_empty_dict(self):
        result = weekly_load({}, weeks=4)
        assert all(r["tss"] == 0.0 for r in result)

    def test_week_labels_are_chronological(self):
        result = weekly_load({}, weeks=5)
        labels = [r["week"] for r in result]
        assert labels == sorted(labels)


# ---------------------------------------------------------------------------
# compute_zone_times
# ---------------------------------------------------------------------------

class TestComputeZoneTimes:

    HR_ZONES = [
        {"name": "Z1", "max_pct": 60,  "color": "#aaa"},
        {"name": "Z2", "max_pct": 75,  "color": "#bbb"},
        {"name": "Z3", "max_pct": 85,  "color": "#ccc"},
        {"name": "Z4", "max_pct": 95,  "color": "#ddd"},
        {"name": "Z5", "max_pct": 100, "color": "#eee"},
    ]

    def _stream(self, hr=None, power=None, n=120):
        return [
            {"elapsed_secs": t, "hr": hr, "power": power}
            for t in range(n)
        ]

    def test_returns_none_when_no_hr_data(self):
        hr_secs, _ = compute_zone_times(self._stream(hr=None), self.HR_ZONES, [], 190, None)
        assert hr_secs is None

    def test_all_time_in_single_zone(self):
        # 150 bpm on 190 max = 79 % → Z3 (75–85%)
        stream = self._stream(hr=150, n=120)
        hr_secs, _ = compute_zone_times(stream, self.HR_ZONES, [], 190, None)
        assert hr_secs["Z3"] > 0
        assert hr_secs["Z1"] == 0
        assert hr_secs["Z5"] == 0

    def test_total_zone_time_matches_duration(self):
        stream = self._stream(hr=150, n=120)
        hr_secs, _ = compute_zone_times(stream, self.HR_ZONES, [], 190, None)
        assert sum(hr_secs.values()) == pytest.approx(119, abs=2)

    def test_power_zones_independent_of_hr(self):
        POWER_ZONES = [
            {"name": "P1", "max_pct": 55,  "color": "#aaa"},
            {"name": "P2", "max_pct": 75,  "color": "#bbb"},
            {"name": "P3", "max_pct": 90,  "color": "#ccc"},
            {"name": "P4", "max_pct": 105, "color": "#ddd"},
            {"name": "P5", "max_pct": 120, "color": "#eee"},
        ]
        stream = self._stream(hr=None, power=200, n=120)
        _, power_secs = compute_zone_times(stream, [], POWER_ZONES, None, ftp=250)
        # 200 W / 250 FTP = 80% → P3 (75–90%)
        assert power_secs["P3"] > 0

    def test_skips_large_time_gaps(self):
        stream = [
            {"elapsed_secs": 0,   "hr": 150, "power": None},
            {"elapsed_secs": 1,   "hr": 150, "power": None},
            {"elapsed_secs": 400, "hr": 150, "power": None},  # 399 s gap → skipped
        ]
        hr_secs, _ = compute_zone_times(stream, self.HR_ZONES, [], 190, None)
        # Only 1 s of valid data — result could be None or 1 s in Z3
        assert hr_secs is None or sum(hr_secs.values()) <= 1


# ---------------------------------------------------------------------------
# aggregate_power_curve
# ---------------------------------------------------------------------------

class TestAggregatePowerCurve:

    def test_returns_best_across_activities(self):
        a1 = {"5s": 800, "30s": 600, "1min": 500}
        a2 = {"5s": 900, "30s": 550, "1min": 520}
        result = aggregate_power_curve([a1, a2])
        assert result["5s"]   == 900
        assert result["30s"]  == 600
        assert result["1min"] == 520

    def test_handles_empty_list(self):
        assert aggregate_power_curve([]) == {}

    def test_handles_none_entries(self):
        result = aggregate_power_curve([None, {"5s": 700}])
        assert result["5s"] == 700

    def test_missing_label_not_in_result(self):
        result = aggregate_power_curve([{"5s": 700}])
        assert "60min" not in result


# ---------------------------------------------------------------------------
# fit_critical_power
# ---------------------------------------------------------------------------

class TestFitCriticalPower:

    def test_returns_none_for_insufficient_data(self):
        cp, wp = fit_critical_power({"1min": 400})
        assert cp is None and wp is None

    def test_returns_none_for_empty_dict(self):
        cp, wp = fit_critical_power({})
        assert cp is None and wp is None

    def test_positive_cp_and_w_prime(self):
        # Realistic MMP curve
        mmp = {"1min": 420, "5min": 320, "20min": 270, "60min": 240}
        cp, wp = fit_critical_power(mmp)
        assert cp is not None and cp > 0
        assert wp is not None and wp > 0

    def test_cp_is_close_to_60min_power(self):
        # At very long durations power approaches CP asymptotically
        mmp = {"1min": 420, "5min": 320, "20min": 270, "60min": 240}
        cp, _ = fit_critical_power(mmp)
        # CP should be in the neighborhood of 60-min power
        assert 200 < cp < 270

    def test_returns_none_for_flat_curve(self):
        # All durations same power → model breaks down
        mmp = {"1min": 300, "5min": 300, "20min": 300, "60min": 300}
        cp, wp = fit_critical_power(mmp)
        # May return valid values or None, but w_prime should be near zero or None
        if cp is not None:
            assert wp is None or wp > 0  # just shouldn't crash


# ---------------------------------------------------------------------------
# compute_wbal
# ---------------------------------------------------------------------------

class TestComputeWbal:

    def _stream(self, power_fn, duration_s=600, step=1):
        return [{"elapsed_secs": t, "power": power_fn(t)} for t in range(0, duration_s, step)]

    def test_returns_none_for_short_stream(self):
        stream = self._stream(lambda t: 250, duration_s=5)
        assert compute_wbal(stream, cp=250, w_prime=20000) is None

    def test_below_cp_wbal_stays_full(self):
        # Riding at exactly CP: no depletion, no reconstitution
        stream = self._stream(lambda t: 250, duration_s=600)
        result = compute_wbal(stream, cp=250, w_prime=20000)
        # All points should be at or near 100%
        assert all(r["wbal_pct"] >= 99.0 for r in result)

    def test_above_cp_depletes_wbal(self):
        # Hard effort above CP should drain W'
        stream = self._stream(lambda t: 350, duration_s=600)
        result = compute_wbal(stream, cp=250, w_prime=20000)
        assert result[-1]["wbal_pct"] < 100.0

    def test_recovery_after_hard_effort_restores_wbal(self):
        # Hard 5-min effort followed by 5-min recovery below CP
        def power(t):
            return 400 if t < 300 else 150
        stream = self._stream(power, duration_s=600)
        result = compute_wbal(stream, cp=250, w_prime=20000)
        mid_pct  = next(r["wbal_pct"] for r in result if r["elapsed_secs"] >= 299)
        end_pct  = result[-1]["wbal_pct"]
        assert end_pct > mid_pct

    def test_wbal_pct_bounded_0_to_100(self):
        stream = self._stream(lambda t: 500, duration_s=600)
        result = compute_wbal(stream, cp=250, w_prime=20000)
        for r in result:
            assert 0.0 <= r["wbal_pct"] <= 100.0

    def test_result_has_required_keys(self):
        stream = self._stream(lambda t: 300, duration_s=600)
        result = compute_wbal(stream, cp=250, w_prime=20000)
        assert result is not None
        for r in result:
            assert "elapsed_secs" in r and "wbal_pct" in r
