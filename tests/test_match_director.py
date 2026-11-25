"""Open-result drama: bounded influence, normal deliveries and resumable state."""
import json
import random

import pytest

from engine.match import Match
from engine.match_director import MatchDirector, apply_director_weights
from engine.scenario_engine import ScenarioEngine, create_scenario_engine
from tests.test_first_innings_scenario_phase import _build_match_data, _quiet_match


def build(fmt="T20", pitch="Green"):
    data = _build_match_data(fmt)
    data.update(pitch=pitch, scenario_mode="adaptive_v1", rain_probability=0)
    return Match(data)


def situation(m, innings=1, over=8, score=40, wickets=5):
    m.innings, m.current_over, m.score, m.wickets = innings, over, score, wickets
    m.current_ball = 0
    m.current_striker, m.current_non_striker = m.batting_team[5:7]
    m.remaining_batter_indices = set(range(7, 11))
    if innings == 2:
        m.target = 101
    return m.scenario_engine


def test_open_result_and_legacy_dispatch():
    m = build()
    assert isinstance(m.scenario_engine, MatchDirector)
    assert isinstance(create_scenario_engine("last_ball_six", m), ScenarioEngine)
    m.is_fc = True
    assert create_scenario_engine("adaptive_v1", m) is None


def test_both_innings_and_no_random_draws():
    m = build()
    for innings in (1, 2):
        d = situation(m, innings)
        before = random.getstate()
        bias = d.get_scenario_bias({})
        assert bias['match_director']
        assert d.get_override_outcome(m.current_striker, m.current_bowler) is None
        assert d.get_phase() == 'adaptive'
        assert random.getstate() == before
        assert all(0.88 <= v <= 1.12 for v in bias['match_director'].values())


def test_survival_has_boundary_cost_and_aggression_has_wicket_cost():
    m = build()
    d = situation(m, wickets=7)
    factors = d.get_scenario_bias({})['match_director']
    assert factors['Four'] < 1 and factors['Wicket'] < 1
    assert factors['Single'] > 1
    situation(m, innings=2, over=17, score=78, wickets=7)
    factors = d.get_scenario_bias({})['match_director']
    assert factors['Four'] > 1 and factors['Wicket'] > 1


@pytest.mark.parametrize('score,target,wickets', [(98,101,5),(50,51,9),(0,101,10),(101,101,4),(20,101,8)])
def test_easy_finished_and_implausible_chases_are_released(score,target,wickets):
    m = build()
    d = situation(m, innings=2, over=19, score=score, wickets=wickets)
    m.target = target
    assert d.get_scenario_bias({}) == {}


def test_high_scoring_match_is_not_forced_low():
    m = build(pitch='Flat')
    d = situation(m, score=120)
    assert d.get_scenario_bias({}) == {}


def test_resources_change_intent_without_switching_winner():
    m = build()
    d = situation(m, innings=2, over=15, score=65, wickets=5)
    first = d.get_scenario_bias({})
    for p in m.bowling_team:
        p['bowling_rating'] = 99
    for p in (m.current_striker,m.current_non_striker):
        p['batting_rating'] = 25
    assert d.get_scenario_bias({}) != first


def test_adjustments_preserve_extras_and_cannot_create_impossible_outcomes():
    base = dict(Dot=30, Single=30, Double=8, Three=1, Four=15, Six=0, Wicket=5, Wide=3)
    changed = apply_director_weights(base, {'Four':1.12, 'Six':1.12,'Wicket':0.9},base)
    assert sum(changed.values()) == pytest.approx(sum(base.values()))
    assert changed['Wide'] == base['Wide'] and changed['Six'] == 0
    assert all(v >= 0 for v in changed.values())
    assert base['Four'] == 15
    extreme = dict(base, Four=45)
    assert apply_director_weights(extreme, {'Four':1.12},base) == extreme


@pytest.mark.parametrize('fmt', ['T10','T20','ListA','Hundred'])
def test_legal_ball_progress_and_rain_reassessment(fmt):
    m = build(fmt)
    d = situation(m, innings=2, over=m.fmt.overs//2, score=30)
    m.target = 60
    # Same legal-ball state (e.g. repeated inspection) is deterministic.
    assert d.get_scenario_bias({}) == d.get_scenario_bias({})
    m.fmt.overs = m.current_over
    assert d.get_scenario_bias({}) == {}


def test_hundred_checkpoint_preserves_director_and_next_delivery():
    from engine.hundred_snapshot import serialize, restore
    random.seed(901)
    m = build('Hundred')
    for _ in range(12):
        m.next_ball()
    snap = json.loads(json.dumps(serialize(m)))
    restored = build('Hundred')
    restore(restored, snap)
    assert restored.scenario_engine.match is restored
    assert restored.scenario_engine.get_scenario_bias({}) == m.scenario_engine.get_scenario_bias({})
    assert restored.next_ball() == m.next_ball()


@pytest.mark.parametrize('fmt', ['T10','T20','ListA','Hundred'])
def test_full_match_uses_normal_outcomes_and_terminates(fmt, monkeypatch):
    import engine.match as module
    actual = module.calculate_outcome
    seen = set()
    def observed(*args, **kwargs):
        seen.add(kwargs['innings'])
        return actual(*args, **kwargs)
    monkeypatch.setattr(module, 'calculate_outcome', observed)
    random.seed(120)
    m = build(fmt)
    for _ in range(1500):
        result = m.next_ball()
        assert not result.get('error'), result
        if result.get('match_over') or result.get('super_over_required'):
            break
    else:
        pytest.fail('match failed to finish')
    assert seen == {1,2}


def test_adaptive_free_hit_keeps_batter_alive(monkeypatch):
    import engine.match as module
    m = build()
    m.current_bowler = m.bowling_team[0]
    m.bowler_selected_for_over = 0
    m.free_hit_active = True
    monkeypatch.setattr(module, 'calculate_outcome', lambda **kwargs: dict(
        type='wicket', runs=0, batter_out=True, wicket_type='Bowled',
        is_extra=False, description='Bowled'))
    m.next_ball()
    assert m.wickets == 0
    assert not m.ball_history[-1]['is_wicket']


def test_adaptive_all_out_tie_uses_existing_tiebreak(monkeypatch):
    import engine.match as module
    m = build()
    m.score, m.wickets, m.current_over = 100, 3, m.overs
    m.next_ball()
    assert m.innings == 2 and m.target == 101
    m.score, m.wickets = 100, 9
    m.remaining_batter_indices = set()
    monkeypatch.setattr(module, 'calculate_outcome', lambda **kwargs: dict(
        type='wicket', runs=0, batter_out=True, wicket_type='Bowled',
        is_extra=False, description='Bowled'))
    assert not m.next_ball().get('match_over')
    assert m.next_ball().get('super_over_required')


def test_state_reconstruction_and_team_labels_do_not_change_intent():
    m = build()
    d = situation(m, innings=2)
    expected = d.get_scenario_bias({})
    # Stateless reconstruction introduces neither fresh random draws nor
    # different treatment of home/away labels.
    m.data['team_home'], m.data['team_away'] = m.data['team_away'], m.data['team_home']
    assert MatchDirector(m).get_scenario_bias({}) == expected


def test_final_probability_budget_includes_renormalization():
    baseline = dict(Dot=20, Single=20, Four=20, Six=10, Wicket=5, Wide=2)
    factors = dict(Dot=.88, Single=.88, Four=1.12, Six=1.12, Wicket=.88)
    adjusted = apply_director_weights(baseline, factors, baseline)
    assert sum(adjusted.values()) == pytest.approx(sum(baseline.values()))
    for key, value in baseline.items():
        assert 0.88 <= adjusted[key] / value <= 1.1200000001
    extreme = dict(baseline, Wicket=20)
    adjusted = apply_director_weights(extreme, {'Four':.88, 'Six':.88}, baseline)
    assert adjusted['Wicket'] <= extreme['Wicket']
