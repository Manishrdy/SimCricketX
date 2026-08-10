"""
Unit tests for engine/weather.py — forecast tiers, weather scripts, and
interruption resolution.
"""

import random

import pytest

from engine.weather import (
    CHASE_TERMINATED,
    FORECAST_TIERS,
    INNINGS_TERMINATED,
    NO_RESULT,
    RESUME,
    generate_weather_script,
    min_overs_for_result,
    resolve_interruption,
    revised_max_bowler_overs,
)


# ── Quotas and minimums ───────────────────────────────────────────────────────

class TestQuotasAndMinimums:
    def test_full_length_quotas_reproduced(self):
        assert revised_max_bowler_overs(20) == 4
        assert revised_max_bowler_overs(50) == 10

    def test_shortened_quotas(self):
        assert revised_max_bowler_overs(43) == 9
        assert revised_max_bowler_overs(14) == 3
        assert revised_max_bowler_overs(8) == 2
        assert revised_max_bowler_overs(5) == 1

    def test_minimum_overs(self):
        assert min_overs_for_result("T20") == 5
        assert min_overs_for_result("ListA") == 20
        assert min_overs_for_result("SomethingElse") == 5


# ── Weather script generation ─────────────────────────────────────────────────

class TestWeatherScript:
    def test_clear_never_rains(self):
        for seed in range(50):
            script = generate_weather_script("clear", 20, "T20", random.Random(seed))
            assert script["events"] == []

    def test_deterministic_for_same_seed(self):
        a = generate_weather_script("storm_warning", 50, "ListA", random.Random(42))
        b = generate_weather_script("storm_warning", 50, "ListA", random.Random(42))
        assert a == b

    def test_rain_chance_roughly_matches_tier(self):
        hits = sum(
            1 for seed in range(500)
            if generate_weather_script("rain_around", 50, "ListA", random.Random(seed))["events"]
        )
        # ~50% tier; allow generous tolerance.
        assert 200 <= hits <= 300

    def test_t20_capped_to_one_event(self):
        for seed in range(300):
            script = generate_weather_script("storm_warning", 20, "T20", random.Random(seed))
            assert len(script["events"]) <= 1

    def test_events_within_match_bounds_and_ordered(self):
        for seed in range(300):
            for fmt, overs in (("T20", 20), ("ListA", 50)):
                script = generate_weather_script("storm_warning", overs, fmt, random.Random(seed))
                prev = -10
                for ev in script["events"]:
                    assert 1 <= ev["at_global_over"] <= overs * 2 - 1
                    assert ev["overs_lost"] >= 1
                    assert ev["at_global_over"] - prev >= 4
                    prev = ev["at_global_over"]

    def test_severity_scales_with_format(self):
        # Same tier: List A loses proportionally similar but absolutely more overs.
        t20_losses, lista_losses = [], []
        for seed in range(400):
            t = generate_weather_script("passing_showers", 20, "T20", random.Random(seed))
            l = generate_weather_script("passing_showers", 50, "ListA", random.Random(seed))
            t20_losses.extend(e["overs_lost"] for e in t["events"])
            lista_losses.extend(e["overs_lost"] for e in l["events"])
        assert t20_losses and lista_losses
        assert max(t20_losses) <= 5      # 25% of 20
        assert max(lista_losses) <= 13   # 25% of 50 (rounding)
        assert min(t20_losses) >= 1
        assert min(lista_losses) >= 5    # 10% of 50

    def test_unknown_forecast_falls_back_to_clear(self):
        script = generate_weather_script("hurricane", 20, "T20", random.Random(1))
        assert script["events"] == []


# ── Interruption resolution ───────────────────────────────────────────────────

class TestResolveInterruptionInnings1:
    def test_resume_with_reduced_overs(self):
        # List A: rain after 13 overs, 7 lost -> both innings become 43.
        out = resolve_interruption(1, 13, 50, 7, "ListA")
        assert out == {"type": RESUME, "revised_overs": 43}

    def test_innings_terminated_when_cut_below_progress(self):
        # Rain after 30 overs, 25 lost: 50-25=25 < 30 -> innings ends at 30.
        out = resolve_interruption(1, 30, 50, 25, "ListA")
        assert out == {"type": INNINGS_TERMINATED, "revised_overs": 30}

    def test_no_result_when_below_minimum(self):
        # T20: rain after 2 overs, 17 lost -> 3 overs < 5 minimum.
        out = resolve_interruption(1, 2, 20, 17, "T20")
        assert out["type"] == NO_RESULT

    def test_lista_minimum_is_20(self):
        out = resolve_interruption(1, 10, 50, 35, "ListA")
        assert out["type"] == NO_RESULT
        out = resolve_interruption(1, 10, 50, 30, "ListA")
        assert out == {"type": RESUME, "revised_overs": 20}


class TestResolveInterruptionInnings2:
    def test_resume_with_reduced_chase(self):
        out = resolve_interruption(2, 20, 50, 7, "ListA")
        assert out == {"type": RESUME, "revised_overs": 43}

    def test_chase_terminated_on_par_after_minimum(self):
        # Chase stopped dead at 31 overs (>= 20 minimum): DLS par decides.
        out = resolve_interruption(2, 31, 50, 30, "ListA")
        assert out == {"type": CHASE_TERMINATED, "revised_overs": 31}

    def test_no_result_before_minimum(self):
        # T20 chase stopped at 3 overs with too much lost to resume.
        out = resolve_interruption(2, 3, 20, 18, "T20")
        assert out["type"] == NO_RESULT

    def test_resume_would_be_below_minimum_terminates_on_par(self):
        # Chase at 22 overs, revised allocation would be 18 (< 20 minimum)
        # but 22 >= 20 already bowled -> decided on par now.
        out = resolve_interruption(2, 22, 50, 32, "ListA")
        assert out == {"type": CHASE_TERMINATED, "revised_overs": 22}

    def test_resume_would_be_below_minimum_no_result(self):
        # Chase at 2 overs, revised would be 4 (< 5 minimum) -> No Result.
        out = resolve_interruption(2, 2, 20, 16, "T20")
        assert out["type"] == NO_RESULT

    def test_second_interruption_uses_current_allocation(self):
        # Already revised to 43; second stoppage at 35 overs losing 5 -> 38.
        out = resolve_interruption(2, 35, 43, 5, "ListA")
        assert out == {"type": RESUME, "revised_overs": 38}
