"""Inspect the actual sampling distribution, without Monte Carlo noise."""
import math
from types import SimpleNamespace

import pytest

import engine.ball_outcome as outcomes
import engine.super_over_outcome as super_outcomes
from engine.format_config import get_any_format
from engine.ground_config import get_defaults


class _SampleCaptured(Exception):
    pass


def _profile(monkeypatch, fmt, batting, bowling, *, config=None):
    sampled = {}

    def capture(population, weights, **kwargs):
        sampled.update(zip(population, weights))
        raise _SampleCaptured

    module = super_outcomes if fmt == "SuperOver" else outcomes
    # Replace this module's RNG reference, not attributes of random_source:
    # restoring dynamic module attributes can otherwise bypass per-match RNGs.
    with monkeypatch.context() as patch:
        patch.setattr(module, "random", SimpleNamespace(choices=capture))
        arguments = dict(
            batter=dict(name="Bat", batting_rating=batting, batting_hand="Right"),
            bowler=dict(name="Bowl", bowling_rating=bowling, fielding_rating=70,
                        bowling_hand="Right", bowling_type="Medium"),
            pitch="Hard", streak={}, batter_runs=0, balls_faced=10,
            ground_config_override=config or get_defaults("T20" if fmt == "SuperOver" else fmt),
        )
        with pytest.raises(_SampleCaptured):
            if fmt == "SuperOver":
                super_outcomes.calculate_super_over_outcome(**arguments)
            else:
                outcomes.calculate_outcome(**arguments, over_number=7,
                                           format_config=get_any_format(fmt))
    assert all(math.isfinite(v) and v >= 0 for v in sampled.values())
    assert sum(sampled.values()) == pytest.approx(1)
    return sampled


def _run_shape(profile):
    mass = sum(profile[k] for k in ("Dot", "Single", "Double", "Three", "Four", "Six"))
    return profile["Dot"] / mass, (profile["Four"] + profile["Six"]) / mass


@pytest.mark.parametrize("fmt", ["T20", "ListA", "FC", "T10", "Hundred", "SuperOver"])
def test_ratings_change_scoring_in_each_delivery_path(monkeypatch, fmt):
    # Hold position, phase, handedness, pressure and pitch constant. Ratings
    # should progressively change shot selection, even within specialist tiers.
    batting_profiles = [_profile(monkeypatch, fmt, r, 70) for r in (25, 55, 75, 95)]
    bowling_profiles = [_profile(monkeypatch, fmt, 75, r) for r in (25, 55, 75, 95)]
    batting = [_run_shape(p) for p in batting_profiles]
    bowling = [_run_shape(p) for p in bowling_profiles]
    for weak, strong in zip(batting, batting[1:]):
        assert strong[0] < weak[0]
        assert strong[1] > weak[1]
    for weak, strong in zip(bowling, bowling[1:]):
        assert strong[0] > weak[0]
        assert strong[1] < weak[1]
    assert batting[-1][0] < batting[0][0] - .05
    assert batting[-1][1] > batting[0][1] * 1.3

    def expected_runs(p):
        return sum(p[k] * v for k, v in
                   {"Single": 1, "Double": 2, "Three": 3, "Four": 4, "Six": 6}.items())

    # Check the unconditional run rate too, so a change in wicket/extra mass
    # cannot hide a reversed scoring effect behind conditional percentages.
    assert all(expected_runs(a) < expected_runs(b)
               for a, b in zip(batting_profiles, batting_profiles[1:]))
    assert all(expected_runs(a) > expected_runs(b)
               for a, b in zip(bowling_profiles, bowling_profiles[1:]))


@pytest.mark.parametrize("fmt", ["T20", "ListA", "FC", "T10", "Hundred"])
def test_pitch_only_blending_disables_rating_shape(monkeypatch, fmt):
    config = get_defaults(fmt, mutable=True)
    config["blending"] = {"pitch_weight": 1, "skill_weight": 0}
    weak = _run_shape(_profile(monkeypatch, fmt, 35, 70, config=config))
    strong = _run_shape(_profile(monkeypatch, fmt, 95, 70, config=config))
    assert strong == pytest.approx(weak)


@pytest.mark.parametrize("batting,bowling", [(0, 0), (0, 100), (100, 0), (250, 200)])
def test_extreme_effective_ratings_remain_sampleable(monkeypatch, batting, bowling):
    for fmt in ("T20", "ListA", "FC", "T10", "Hundred", "SuperOver"):
        _profile(monkeypatch, fmt, batting, bowling)


def test_rating_shape_preserves_disabled_outcomes():
    for rating in (0, 25, 75, 100, 250):
        for outcome in ("Dot", "Single", "Double", "Three", "Four", "Six"):
            assert outcomes.compute_weighted_prob(
                outcome, 0, rating, 70, 70, "Hard", "Medium", {}
            ) == 0
