import random

import pytest

from engine.fc_batting_intent import (
    batting_intent,
    tail_protection,
    apply_tail_protection,
)
from engine.pressure_engine import FCPressureEngine
from tests.test_fc_format import _fc_match


def context(**kw):
    return dict(
        fc_innings=4,
        wickets=2,
        overs_remaining=40,
        runs_needed=100,
        pitch="Hard",
        remaining_strength=65,
        overs_today=10,
        **kw
    )


def test_final_day_achievable_chase_stays_live():
    state = context()
    intent = batting_intent(state)
    assert intent["survival"] == 0
    effects = FCPressureEngine().get_pressure_effects(
        dict(state, intent=intent, days_remaining=1, striker_balls_faced=50)
    )
    assert effects["boundary_modifier"] >= 1
    assert intent["stumps"] == 0


def test_intent_is_continuous_and_exclusive():
    previous = None
    for step in range(1000):
        state = context()
        state["runs_needed"] = step / 100 * 40
        intent = batting_intent(state)
        assert sum(
            intent[k] for k in ("survival", "neutral", "attack")
        ) == pytest.approx(1)
        assert all(
            0 <= intent[k] <= 1 for k in ("survival", "neutral", "attack", "stumps")
        )
        if previous:
            assert (
                max(abs(intent[k] - previous[k]) for k in ("survival", "attack")) < 0.02
            )
        previous = intent
    assert previous["survival"] == 1
    assert previous["attack"] == 0


@pytest.mark.parametrize("overs,needed", [(0, 100), (40, 0)])
def test_finished_context_is_safe(overs, needed):
    state = context()
    state.update(overs_remaining=overs, runs_needed=needed)
    assert batting_intent(state)["reason"] == "complete"


def test_stumps_caution_is_not_restricted_to_final_day():
    state = context()
    state.update(fc_innings=1, runs_needed=None, overs_today=2)
    assert batting_intent(state)["stumps"] > 0.9


def test_legacy_pressure_does_not_force_final_day_survival():
    effects = FCPressureEngine().get_pressure_effects(
        dict(fc_innings=4, days_remaining=1, striker_balls_faced=50)
    )
    assert effects["boundary_modifier"] == 1


def test_resume_derives_identical_tactics():
    m = _fc_match()
    m.current_ball = 4
    m.wickets = 8
    before = m._fc_build_match_state()
    snap = m.serialize_fc_snapshot()
    restored = _fc_match()
    restored.restore_fc_snapshot(snap)
    assert restored._fc_build_match_state() == before


def test_tail_transfers_only_dot_and_single_mass():
    state = dict(
        wickets=9,
        striker_ability=85,
        partner_ability=15,
        striker_balls_faced=90,
        ball_in_over=0,
        runs_needed=None,
    )
    intent = dict(attack=0)
    early = tail_protection(state, intent)
    late = tail_protection(dict(state, ball_in_over=5), intent)
    weights = dict(Dot=0.6, Single=0.2, Four=0.12, Wicket=0.04, Extras=0.04)
    changed = apply_tail_protection(weights, early)
    assert changed["Single"] < weights["Single"]
    assert sum(changed.values()) == pytest.approx(sum(weights.values()))
    assert all(changed[k] == weights[k] for k in ("Four", "Wicket", "Extras"))
    assert apply_tail_protection(weights, late)["Single"] > weights["Single"]
    assert tail_protection(dict(state, runs_needed=1), intent)["refuse_single"] == 0
    assert (
        tail_protection(dict(state, partner_ability=80), intent)["refuse_single"] == 0
    )


def test_tail_policy_increases_specialist_strike_share():
    # Sample full overs, including ordinary end changes. No wickets/extras:
    # this isolates the tactical policy from unrelated survival differences.
    def share(enabled):
        rng = random.Random(71)
        faced = total = 0
        specialist = True
        for _ in range(5000):
            for ball in range(6):
                faced += specialist
                total += 1
                w = {"Dot": 0.65, "Single": 0.25, "Four": 0.10}
                if enabled:
                    state = dict(
                        wickets=9,
                        striker_ability=85 if specialist else 15,
                        partner_ability=15 if specialist else 85,
                        striker_balls_faced=90,
                        ball_in_over=ball,
                    )
                    w = apply_tail_protection(w, tail_protection(state, {"attack": 0}))
                result = rng.choices(list(w), weights=list(w.values()))[0]
                if result == "Single":
                    specialist = not specialist
            specialist = not specialist
        return faced / total

    assert share(True) > share(False) + 0.04


def test_resumed_scoring_matches_uninterrupted_rng_state():
    import copy
    import json

    random.seed(815)
    m = _fc_match()
    for _ in range(80):
        m.next_ball()
    snap = json.loads(json.dumps(m.serialize_fc_snapshot()))
    rng = random.getstate()

    def continue_match(match):
        result = []
        for _ in range(80):
            before = match.match_balls_bowled
            match.next_ball()
            if match.match_balls_bowled != before:
                result.append(copy.deepcopy(match._fc_last_delivery))
        return (
            result,
            copy.deepcopy(match.batsman_stats),
            copy.deepcopy(match.bowler_stats),
        )

    uninterrupted = continue_match(m)
    restored = _fc_match()
    restored.restore_fc_snapshot(snap)
    random.setstate(rng)
    assert continue_match(restored) == uninterrupted


def test_extreme_attacking_policy_never_samples_negative_weights(monkeypatch):
    from engine.ball_outcome import calculate_outcome
    from engine.format_config import get_any_format

    original = random.choices

    def checked(population, weights=None, **kw):
        if weights is not None:
            assert all(w >= 0 for w in weights)
        return original(population, weights=weights, **kw)

    monkeypatch.setattr(random, "choices", checked)
    m = _fc_match()
    calculate_outcome(
        batter=m.current_striker,
        bowler=next(p for p in m.bowling_team if p["will_bowl"]),
        pitch="Hard",
        streak={},
        over_number=30,
        batter_runs=50,
        format_config=get_any_format("FC"),
        pressure_effects={"dot_bonus": -5},
    )
