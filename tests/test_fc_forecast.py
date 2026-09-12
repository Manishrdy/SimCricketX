"""Stage B acceptance: the frontier and the exact innings forecast.

The gate that matters is the last one. A captain reasoning about a forward
model that disagrees with the ball engine is optimising against a fiction,
so `test_forecast_reproduces_engine_innings` compares the model's predicted
innings against the innings the engine actually played in the sweep.
"""
import json
import math
import os
import random
import statistics

import pytest

from engine import fc_forecast
from engine.fc_batting_intent import ability
from engine.fc_forecast import frontier, innings_forecast, probability_runs_below
from scripts.bench_fc import _squad

DATASET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "reports", "fc_frontier_dataset.json")

AGGRESSIONS = (-1.0, -0.5, 0.0, 0.5, 1.0)


def strength_by_wickets(bat_tier="standard", bowl_tier="standard"):
    """Batting-minus-bowling strength for each wickets-in-hand state.

    With w wickets in hand, w+1 batters remain, and they are the LAST w+1 in
    the order — so the tail is modelled as the tail rather than as the XI's
    average, which is what the old estimator did.
    """
    batting = _squad("H", bat_tier)
    attack = [p for p in _squad("A", bowl_tier) if p["will_bowl"]]
    bowling = sum(p["bowling_rating"] for p in attack) / len(attack)
    return {w: sum(ability(p) for p in batting[11 - (w + 1):]) / (w + 1) - bowling
            for w in range(1, 11)}


def test_frontier_is_monotone_in_aggression():
    for pitch in fc_forecast.PITCHES:
        rates, wickets = [], []
        for aggression in AGGRESSIONS:
            rate, wicket_rate = frontier(pitch, 0.3, 30, -15, aggression)
            rates.append(rate)
            wickets.append(wicket_rate)
        assert rates == sorted(rates), f"{pitch}: scoring must rise with aggression"
        assert wickets == sorted(wickets), f"{pitch}: risk must rise with aggression"


def test_attacking_is_cheaper_on_a_belter_than_a_seamer():
    """The pitch x aggression interaction is the reason those terms exist."""
    def risk_multiple(pitch):
        calm = frontier(pitch, 0.3, 30, -15, -1.0)[1]
        hard = frontier(pitch, 0.3, 30, -15, 1.0)[1]
        return hard / calm

    assert risk_multiple("Green") > risk_multiple("Dead") * 1.5


def test_frontier_stays_inside_its_guard_rails():
    for pitch in list(fc_forecast.PITCHES) + ["NotAPitch"]:
        for wear in (-5.0, 0.0, 0.5, 1.0, 9.0):
            for diff in (-200, 0, 200):
                rate, wicket_rate = frontier(pitch, wear, 200, diff, 5.0)
                assert fc_forecast.MIN_RATE <= rate <= fc_forecast.MAX_RATE
                assert (fc_forecast.MIN_WICKET_RATE <= wicket_rate
                        <= fc_forecast.MAX_WICKET_RATE)
                assert math.isfinite(rate) and math.isfinite(wicket_rate)


@pytest.mark.parametrize("overs,wickets", [(0, 10), (50, 0), (0, 0)])
def test_exhausted_states_are_safe(overs, wickets):
    forecast = innings_forecast(
        pitch="Hard", overs_available=overs, wickets_in_hand=wickets,
        strength_by_wickets=strength_by_wickets(), aggression=0.0)
    assert forecast["expected_runs"] == 0
    assert forecast["total_mass"] == pytest.approx(1.0, abs=1e-6)


def test_probability_mass_is_conserved():
    for aggression in AGGRESSIONS:
        for overs in (10, 90, 250):
            forecast = innings_forecast(
                pitch="Dead", overs_available=overs, wickets_in_hand=10,
                strength_by_wickets=strength_by_wickets(), aggression=aggression)
            assert forecast["total_mass"] == pytest.approx(1.0, abs=1e-6)
            assert (forecast["runs_pmf"] >= -1e-12).all()
            assert 0.0 <= forecast["p_all_out"] <= 1.0


def test_more_time_cannot_reduce_the_chance_of_bowling_a_side_out():
    previous = -1.0
    for overs in range(20, 240, 20):
        forecast = innings_forecast(
            pitch="Green", overs_available=overs, wickets_in_hand=10,
            strength_by_wickets=strength_by_wickets(), aggression=0.0)
        assert forecast["p_all_out"] >= previous - 1e-9
        previous = forecast["p_all_out"]


def test_chase_probability_falls_as_the_target_rises():
    previous = 1.0
    for target in range(100, 600, 50):
        forecast = innings_forecast(
            pitch="Hard", overs_available=90, wickets_in_hand=10,
            strength_by_wickets=strength_by_wickets(), aggression=0.5,
            target=target, bucket=1)
        assert forecast["p_target"] <= previous + 1e-9
        previous = forecast["p_target"]
    assert previous < 0.02, "a 550 target in 90 overs is not a live chase"


def test_a_reachable_target_prefers_attack_and_a_hopeless_one_does_not():
    """The frontier exists so a captain can buy runs with risk, not only
    with overs. A gettable target should be likelier when going hard."""
    def chase(aggression, target):
        return innings_forecast(
            pitch="Hard", overs_available=70, wickets_in_hand=10,
            strength_by_wickets=strength_by_wickets(), aggression=aggression,
            target=target, bucket=1)["p_target"]

    assert chase(1.0, 260) > chase(-1.0, 260)
    # Batting out time is a different objective: survival keeps more wickets.
    calm = innings_forecast(
        pitch="Hard", overs_available=70, wickets_in_hand=10,
        strength_by_wickets=strength_by_wickets(), aggression=-1.0)
    bold = innings_forecast(
        pitch="Hard", overs_available=70, wickets_in_hand=10,
        strength_by_wickets=strength_by_wickets(), aggression=1.0)
    assert calm["p_all_out"] < bold["p_all_out"]


def test_forecast_is_deterministic_and_consumes_no_rng():
    def once():
        random.seed(7)
        forecast = innings_forecast(
            pitch="Flat", overs_available=120, wickets_in_hand=8,
            strength_by_wickets=strength_by_wickets(), aggression=0.25,
            target=300, bucket=1)
        return forecast["expected_runs"], forecast["p_target"], random.random()

    assert once() == once()


def test_dynamic_programming_agrees_with_sampling():
    """Validates the recursion itself, independently of the fitted numbers.

    Monte Carlo is kept as an oracle for the DP, not as the production path.
    """
    pitch, overs, aggression = "Hard", 80, 0.0
    strengths = strength_by_wickets()
    forecast = innings_forecast(
        pitch=pitch, overs_available=overs, wickets_in_hand=10,
        strength_by_wickets=strengths, aggression=aggression,
        wear_start=0.3, wear_end=0.3, ball_age_start=0.0, bucket=1)

    rng = random.Random(11)
    totals, all_out = [], 0
    for _ in range(4000):
        runs, wickets_in_hand = 0, 10
        for over in range(overs):
            rate, wicket_rate = frontier(pitch, 0.3, over % 80,
                                         strengths[wickets_in_hand], aggression)
            # Negative binomial as a gamma-Poisson mixture, matching the
            # runs-per-over model the DP convolves.
            size = rate / (fc_forecast.OVER_DISPERSION - 1.0)
            scale = fc_forecast.OVER_DISPERSION - 1.0
            runs += _poisson(rng, rng.gammavariate(size, scale))
            lost = _poisson(rng, wicket_rate)
            wickets_in_hand -= lost
            if wickets_in_hand <= 0:
                all_out += 1
                break
        totals.append(runs)

    assert forecast["expected_runs"] == pytest.approx(statistics.mean(totals),
                                                      rel=0.10)
    assert forecast["p_all_out"] == pytest.approx(all_out / 4000, abs=0.06)


def _poisson(rng, mean):
    limit, total, count = math.exp(-mean), rng.random(), 0
    while total > limit:
        total *= rng.random()
        count += 1
    return count


@pytest.mark.skipif(not os.path.exists(DATASET), reason="run scripts/fc_sweep.py")
def test_forecast_reproduces_engine_innings():
    """THE Stage B gate: the forward model must agree with the ball engine.

    Compares predicted innings totals against the innings the engine really
    played, per pitch and aggression level. If this fails after a scoring
    change, regenerate the dataset and refit — do not widen the tolerance.
    """
    dataset = json.load(open(DATASET))
    errors = []
    for pitch in fc_forecast.PITCHES:
        for aggression in AGGRESSIONS:
            sample = [r for r in dataset["innings"]
                      if r["pitch"] == pitch and r["aggression"] == aggression]
            if len(sample) < 8:
                continue
            observed = statistics.mean(r["runs"] for r in sample)
            predicted = statistics.mean(
                innings_forecast(
                    pitch=pitch, overs_available=250, wickets_in_hand=10,
                    strength_by_wickets=strength_by_wickets(bat, bowl),
                    aggression=aggression, wear_start=0.0, wear_end=0.12,
                )["expected_runs"]
                for bat, bowl in sorted({(r["bat_tier"], r["bowl_tier"])
                                         for r in sample}))
            errors.append(abs(predicted - observed) / observed)

    assert errors, "dataset carried no usable cohorts"
    assert statistics.mean(errors) < 0.15, (
        f"mean error {statistics.mean(errors):.1%} against the ball engine")
    assert max(errors) < 0.30, f"worst cohort error {max(errors):.1%}"


@pytest.mark.skipif(not os.path.exists(DATASET), reason="run scripts/fc_sweep.py")
def test_fitted_model_records_its_provenance():
    model = fc_forecast.MODEL
    assert model["provenance"]["dataset_sha256"]
    assert model["provenance"]["engine_source_hash"]
    assert model["fit"]["legal_balls"] > 50_000, "fit is too thin to trust"
    assert model["fit"]["weighted_r2_rate"] > 0.3


def test_runs_below_threshold_reads_the_distribution():
    forecast = innings_forecast(
        pitch="Green", overs_available=90, wickets_in_hand=10,
        strength_by_wickets=strength_by_wickets(), aggression=0.0, bucket=1)
    assert probability_runs_below(forecast, 0) == 0.0
    assert probability_runs_below(forecast, 10_000) == pytest.approx(1.0, abs=1e-6)
