"""Declaration regressions: long innings, surviving openers, and real review times."""
import json

import numpy as np
import pytest

from engine import fc_captain, fc_declaration
from tests.test_fc_format import _fc_match
from scripts.bench_fc import _squad


def test_surviving_opener_is_not_replaced_by_number_ten():
    xi, attack = _squad("H"), _squad("A")
    tail = fc_captain.remaining_strengths_by_wickets([xi[-2], xi[-1]], [], attack)
    opener = fc_captain.remaining_strengths_by_wickets([xi[0], xi[-1]], [], attack)
    assert opener[1] > tail[1] + 15
    assert tail[1] == pytest.approx(fc_captain.strengths_by_wickets(xi, attack)[1])


def test_unbatted_players_enter_after_the_actual_pair():
    xi, attack = _squad("H"), _squad("A")
    pair, waiting = [xi[0], xi[5]], xi[6:]
    before = list(waiting)
    strengths = fc_captain.remaining_strengths_by_wickets(pair, waiting, attack)
    assert set(strengths) == set(range(1, 7))
    assert strengths[6] > strengths[1]
    assert waiting == before


def test_live_inputs_keep_opener_and_current_wear(monkeypatch):
    match = _fc_match(days=5, pitch="Dead")
    match.batting_team[0]["batting_rating"] = 95
    match.batting_team[0]["technique_rating"] = 95
    match.batting_team[-2]["batting_rating"] = 10
    match.batting_team[-2]["technique_rating"] = 10
    match.wickets = 9
    match.current_striker = match.batting_team[0]
    match.current_non_striker = match.batting_team[-1]
    for player in match.batting_team[1:-1]:
        match.batsman_stats[player["name"]]["wicket_type"] = "Bowled"
    monkeypatch.setattr(match, "_compute_pitch_wear", lambda: 0.83)
    inputs = match._fc_declaration_inputs()
    assert inputs["pitch_wear"] == 0.83
    assert inputs["follow_on_margin"] == 200
    assert inputs["continuation_strengths"][1] > inputs["own_strengths"][1]
    assert set(inputs["continuation_strengths"]) == {1}


def test_all_out_uses_its_own_time_and_score_distribution(monkeypatch):
    # Half the innings end after one over with 20 runs added; the other half
    # survive ten overs with no runs. These must remain separate outcomes.
    absorption = np.zeros((10, 5))
    absorption[0, 4] = 0.5
    forecast = dict(absorption=absorption, survived_pmf=np.array([0.5, 0, 0, 0, 0]),
                    runs_axis=np.arange(5) * 5, p_all_out=0.5,
                    expected_runs=10, expected_overs=5.5)
    monkeypatch.setattr(fc_captain, "_forecast", lambda *args: forecast)
    # Chance to bowl them out increases with available time and target.
    def chase(pitch, overs, *args):
        wins = np.zeros(100)
        losses = overs / 100 + np.arange(100) / 1000
        return wins, losses, 1 - losses
    monkeypatch.setattr(fc_captain, "_best_response_curves", chase)
    result = fc_captain.evaluate_declaration(
        pitch="Dead", fc_innings=3, lead=100, overs_remaining=20,
        own_strengths={1: 0}, opposition_strengths={1: 0},
        wickets_in_hand=1, horizons=[10])
    option = result["best"]
    # Time is evaluated on the existing ten-over grid: 19 -> 20.
    expected_win = 0.5 * (0.20 + 0.025) + 0.5 * (0.10 + 0.021)
    assert option["win"] == pytest.approx(expected_win)
    assert option["expected_overs"] == 5.5
    assert option["win"] + option["draw"] + option["loss"] == pytest.approx(1)


def test_first_innings_follow_on_is_only_available_at_the_margin(monkeypatch):
    # Reply is all out for 100 after ten overs. Batting again guarantees a
    # draw; enforcing guarantees a win, to isolate the eligibility branch.
    mass = np.zeros(21)
    mass[20] = 1
    absorption = np.zeros((30, 21))
    absorption[9] = mass
    monkeypatch.setattr(fc_captain, "_forecast", lambda *args: dict(
        runs_axis=np.arange(21) * 5, survived_pmf=np.zeros(21), absorption=absorption))
    def decline(*args):
        size = args[-1] + 1
        return np.zeros(size), np.ones(size), np.zeros(size)
    def enforce(*args):
        size = args[-1] + 1
        return np.ones(size), np.zeros(size), np.zeros(size)
    monkeypatch.setattr(fc_captain, "_third_innings_curves", decline)
    monkeypatch.setattr(fc_captain, "_lead_declaration_curves", enforce)
    common = dict(pitch="Dead", overs_remaining=30, own_strengths={1: 0},
                  opposition_strengths={1: 0}, follow_on_margin=150)
    fc_captain._first_innings_curves.cache_clear()
    try:
        assert fc_captain.innings_one_declaration_outcome(score=245, **common) == (0, 1, 0)
        assert fc_captain.innings_one_declaration_outcome(score=250, **common) == (1, 0, 0)
    finally:
        fc_captain._first_innings_curves.cache_clear()


def test_model_horizon_reaches_the_match_and_is_reviewed_mid_session(monkeypatch):
    match = _fc_match(pitch="Dead")
    match.score = 400
    match.current_over = 30
    match.fc_day_overs_bowled_today = 30
    match.fc_day_balls_bowled_today = 180
    match.fc_sessions_taken_today = 1
    calls = []
    def model(**kwargs):
        calls.append(kwargs)
        return dict(declare_now=len(calls) > 1, best={"horizon": 0 if len(calls) > 1 else 10,
                    "value": 0.5}, now={"win": 0.5, "draw": 0.5, "loss": 0})
    monkeypatch.setattr(fc_captain, "evaluate_declaration", model)
    match._fc_check_declaration_and_follow_on()
    assert match._fc_declaration_review_over == 40
    assert not match.fc_innings_declared
    match.current_over = 39
    match.fc_day_overs_bowled_today = 39
    match._fc_pre_ball_checks()
    assert len(calls) == 1
    match.current_over = 40
    match.fc_day_overs_bowled_today = 40
    match._fc_pre_ball_checks()
    assert len(calls) == 2
    assert match.fc_innings_declared


def test_review_time_survives_resume_and_resets_with_innings():
    match = _fc_match(pitch="Dead")
    match.current_over = 30
    match._fc_apply_declaration_result(dict(declare_now=False, review_after_overs=10))
    snapshot = json.loads(json.dumps(match.serialize_fc_snapshot()))
    restored = _fc_match(pitch="Dead")
    restored.restore_fc_snapshot(snapshot)
    assert restored._fc_declaration_review_over == 40
    restored._fc_start_next_innings(2, restored.bowling_team, restored.batting_team)
    assert restored._fc_declaration_review_over is None
    # Old snapshots have no pending review.
    snapshot.pop("fc_declaration_review_over")
    restored.restore_fc_snapshot(snapshot)
    assert restored._fc_declaration_review_over is None


@pytest.mark.parametrize("stamina", [0, 50, 100])
def test_stamina_never_rewards_a_longer_innings(stamina):
    match = _fc_match()
    factors = [match._fc_batter_stamina_multiplier({"stamina_rating": stamina}, balls)
               for balls in (0, 120, 200, 360, 433, 1000)]
    assert factors == sorted(factors, reverse=True)
    assert 0.9 <= min(factors) <= max(factors) == 1.0


@pytest.mark.parametrize("score,overs,wickets", [(690, 150, 1), (910, 120, 9)])
def test_dead_pitch_runaway_first_innings_declares(score, overs, wickets):
    strengths = fc_captain.strengths_by_wickets(_squad("H"), _squad("A"))
    assert fc_declaration.should_declare(
        fc_innings=1, wickets=wickets, overs_bowled_this_innings=150,
        score=score, lead=0, days_remaining=2, pitch="Dead",
        overs_remaining_in_match=overs, own_strengths=strengths,
        opposition_strengths=strengths, pitch_wear=0.5)


def test_last_day_follow_on_uses_the_model_instead_of_a_days_veto(monkeypatch):
    monkeypatch.setattr(fc_captain, "evaluate_follow_on", lambda **kwargs: {
        "enforce": True, "fatigue_penalty": 0,
        "options": {"enforce": {"value": 0.8}, "decline": {"value": 0.3}}})
    assert fc_declaration.should_enforce_follow_on(
        deficit=300, follow_on_margin=200, days_remaining=1,
        pitch="Dead", own_strengths={1: 0}, opposition_strengths={1: 0},
        overs_remaining_in_match=60)


def test_snapshot_during_deferred_decision_rechecks_before_a_delivery(monkeypatch):
    import engine.match as match_module
    match = _fc_match(pitch="Dead")
    match.current_over = 30
    match.fc_day_overs_bowled_today = 30
    match.fc_day_balls_bowled_today = 180
    match.fc_sessions_taken_today = 1
    monkeypatch.setattr(match_module, "start_offloaded", lambda *args, **kwargs: object())
    match._fc_start_declaration_decision()
    restored = _fc_match(pitch="Dead")
    restored.restore_fc_snapshot(json.loads(json.dumps(match.serialize_fc_snapshot())))
    assert restored._fc_declaration_job is None
    assert restored._fc_declaration_review_over == 30
    monkeypatch.setattr(fc_declaration, "declaration_decision", lambda **kwargs: {
        "declare_now": True, "review_after_overs": 0})
    restored._fc_pre_ball_checks()
    assert restored.fc_innings_declared
    assert restored.current_over == 30


def test_projected_wear_never_rejuvenates_an_aging_pitch():
    values = [fc_captain._wear_after(0.83, 0.95, elapsed, 100)
              for elapsed in (0, 1, 10, 50, 100)]
    assert values[0] == 0.83
    assert values == sorted(values)
    assert max(values) <= 0.95
