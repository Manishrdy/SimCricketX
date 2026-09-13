"""Stage C/D acceptance: the value function and the decisions it drives.

These check the captain's REASONING — that it prices the overs batting on
costs, that it can prefer a gamble to a certain draw, and that temperament
changes the call. Whole-match distributions are gated separately in
tests/test_fc_match_calibration.py.
"""
import random

import pytest

from engine import fc_captain
from engine.fc_captain import (DEFAULT_RISK_APPETITE, DRAW_VALUE,
                               evaluate_declaration, evaluate_follow_on,
                               horizons_for, strengths_by_wickets, value)
from scripts.bench_fc import _squad


def sides(bat="standard", bowl="standard"):
    own = strengths_by_wickets(_squad("H", bat), _squad("A", bowl))
    opposition = strengths_by_wickets(_squad("A", bowl), _squad("H", bat))
    return own, opposition


def declaration(**overrides):
    own, opposition = sides()
    kwargs = dict(pitch="Hard", fc_innings=3, lead=250, overs_remaining=120,
                  own_strengths=own, opposition_strengths=opposition,
                  wickets_in_hand=6)
    kwargs.update(overrides)
    return evaluate_declaration(**kwargs)


def test_a_draw_is_worth_less_than_a_win_and_more_than_a_loss():
    assert value(1.0, 0.0) == pytest.approx(1.0)
    assert value(0.0, 1.0) == pytest.approx(0.0)
    certain_draw = value(0.0, 0.0)
    assert 0.0 < certain_draw < 1.0
    assert certain_draw == pytest.approx(DRAW_VALUE * DEFAULT_RISK_APPETITE)


def test_a_captain_will_trade_a_certain_draw_for_a_good_enough_chance():
    """Scoring a draw at zero makes every gamble look bad, so a captain bats
    on until the match is dead. That is the behaviour being prevented."""
    certain_draw = value(0.0, 0.0)
    assert value(0.45, 0.10) > certain_draw, "a strong chance must beat a draw"
    assert value(0.02, 0.45) < certain_draw, "a bad gamble must not"


def test_risk_appetite_changes_what_a_captain_settles_for():
    # A captain accepts a gamble once win/loss clears draw_value/(1-draw_value):
    # about 0.22 for a bold captain and 0.72 for a cautious one. This gamble
    # sits between the two, so temperament alone decides it.
    bold, cautious = 0.6, 1.4
    gamble = (0.15, 0.35)  # win, loss
    assert value(*gamble, bold) > value(0.0, 0.0, bold)
    assert value(*gamble, cautious) < value(0.0, 0.0, cautious)


def test_horizons_span_batting_out_the_remaining_time():
    horizons = horizons_for(200)
    assert horizons[0] == 0, "declaring now is always on the menu"
    assert max(horizons) >= 150, "so is killing the game"
    assert all(h < 200 for h in horizons)
    assert horizons_for(0) == ()
    assert list(horizons) == sorted(set(horizons))


def test_declaration_reports_every_option_it_considered():
    plan = declaration()
    assert plan["options"], "the alternatives are the explanation"
    assert {"horizon", "lead", "win", "draw", "loss", "value"} <= set(plan["options"][0])
    for option in plan["options"]:
        total = option["win"] + option["draw"] + option["loss"]
        assert total == pytest.approx(1.0, abs=1e-6)
    assert plan["best"] in plan["options"]
    assert plan["declare_now"] == (plan["best"]["horizon"] == 0)


def test_batting_on_buys_runs_and_spends_overs():
    """The property the old lead-versus-threshold rule could not represent."""
    plan = declaration()
    by_horizon = {o["horizon"]: o for o in plan["options"]}
    assert by_horizon[0]["lead"] < by_horizon[30]["lead"], "batting on gains runs"
    longest = max(by_horizon)
    assert by_horizon[longest]["win"] < by_horizon[0]["win"] + 0.9, (
        "batting to the end of the match cannot be a free win")


def test_the_captain_stops_rather_than_batting_to_the_horizon_limit():
    """A model with no stopping force bats on forever; that is how targets of
    500+ were set. With a lead already overwhelming it must close."""
    plan = declaration(lead=450, overs_remaining=150)
    assert plan["best"]["horizon"] < max(o["horizon"] for o in plan["options"])


def test_an_insufficient_lead_is_not_declared_on():
    plan = declaration(lead=40, overs_remaining=250)
    assert not plan["declare_now"]


def test_exhausted_time_yields_no_options():
    assert declaration(overs_remaining=0) is None


def test_first_innings_looks_three_innings_ahead():
    own, opposition = sides()
    plan = evaluate_declaration(
        pitch="Hard", fc_innings=1, lead=380, overs_remaining=300,
        own_strengths=own, opposition_strengths=opposition, wickets_in_hand=5)
    assert plan is not None
    for option in plan["options"]:
        assert 0.0 <= option["win"] <= 1.0
        assert 0.0 <= option["loss"] <= 1.0
        assert option["win"] + option["draw"] + option["loss"] == pytest.approx(1.0, abs=1e-6)


def test_tail_is_modelled_as_the_tail():
    own, _ = sides()
    assert own[10] > own[2], "a full XI must out-bat the last pair"
    assert own[1] < own[5] < own[9]


def test_wickets_in_hand_change_what_batting_on_is_worth():
    deep = declaration(wickets_in_hand=9)
    thin = declaration(wickets_in_hand=2)
    horizon = 30
    deep_gain = next(o for o in deep["options"] if o["horizon"] == horizon)["lead"]
    thin_gain = next(o for o in thin["options"] if o["horizon"] == horizon)["lead"]
    assert deep_gain > thin_gain, "a side two down scores faster than one nine down"


def test_follow_on_turns_on_whether_the_attack_can_back_it_up():
    own, opposition = sides()
    # A four-day follow-on margin where fatigue changes the preferred option.
    common = dict(pitch="Hard", deficit=150, overs_remaining=250,
                  own_strengths=own, opposition_strengths=opposition)
    fresh = evaluate_follow_on(attack_freshness=1.0, **common)
    spent = evaluate_follow_on(attack_freshness=0.0, **common)
    assert fresh["enforce"], "a fresh attack enforces"
    assert not spent["enforce"], "a spent one bats again"
    assert spent["fatigue_penalty"] > fresh["fatigue_penalty"]
    assert fresh["options"]["enforce"]["value"] > spent["options"]["enforce"]["value"]


def test_follow_on_with_no_time_left_is_not_a_decision():
    own, opposition = sides()
    assert evaluate_follow_on(
        pitch="Hard", deficit=250, overs_remaining=0,
        own_strengths=own, opposition_strengths=opposition) is None


def test_decisions_are_deterministic_and_consume_no_rng():
    def once():
        random.seed(3)
        plan = declaration()
        return plan["best"]["horizon"], round(plan["best"]["value"], 9), random.random()

    fc_captain._forecast.cache_clear()
    fc_captain._best_response_curves.cache_clear()
    first = once()
    fc_captain._forecast.cache_clear()
    fc_captain._best_response_curves.cache_clear()
    assert once() == first


def test_bolder_captains_declare_no_later_than_cautious_ones():
    bold = declaration(risk_appetite=0.6)["best"]["horizon"]
    cautious = declaration(risk_appetite=1.4)["best"]["horizon"]
    assert bold <= cautious
