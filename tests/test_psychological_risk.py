"""Behavioral guards for shared chase urgency and recoverable collapse pressure."""
import itertools
import math

import pytest

from engine import ball_outcome
from engine.format_config import get_format
from engine.game_state_engine import (apply_game_state_to_probs,
    compute_game_state_vector, make_ball_event)
from engine.pressure_engine import PressureEngine
from engine.psychological_risk import (combine_risk_factors, collapse_recovery,
    limit_psychological_odds, WicketRisk, LIMITED_OVERS_WICKET_CAP)
from tests.test_first_innings_cluster_single_roll import _build_match_data
import engine.match as match_module

WEIGHTS = dict(Dot=.30, Single=.30, Double=.08, Three=.01, Four=.15,
               Six=.08, Wicket=.05, Extras=.03)


def event(runs=1, wicket=False, extra=None):
    return make_ball_event(dict(runs=runs, batter_out=wicket,
                                is_extra=bool(extra), extra_type=extra))


def state(history, fmt='T20', rebuilding=False, **kwargs):
    args = dict(ball_history=history, score=60, current_over=8, current_ball=0,
                wickets=4, innings=1, partnership_balls=6, partnership_runs=5,
                format_config=get_format(fmt), rebuilding=rebuilding)
    args.update(kwargs)
    return compute_game_state_vector(**args)


def test_overlap_keeps_single_effects_and_discounts_only_large_stacks():
    assert combine_risk_factors([1.6]) == 1.6
    assert combine_risk_factors([1.10, 1.12]) == pytest.approx(1.232)
    factors = [1.60, 1.27, 1.12, 1.25]
    values = [combine_risk_factors(p) for p in itertools.permutations(factors)]
    assert max(factors) < values[0] < math.prod(factors)
    assert values == pytest.approx([values[0]] * len(values))


@pytest.mark.parametrize('fmt,old', [('T20', 8.875), ('ListA', 11.125)])
def test_extreme_risk_preserves_aggression_without_internal_multiplication(fmt, old):
    f = get_format(fmt)
    result = PressureEngine(f).get_risk_based_effects(dict(
        innings=2, current_over=f.overs-2, current_ball=0, overs_remaining=2,
        wickets=4, required_run_rate=20))
    assert 3.0 < result['wicket_boost'] < old * .70
    assert result['boundary_boost'] > 4
    assert result['mode'] == 'ABSOLUTE_CHAOS'


def test_death_progress_has_same_endpoints_across_formats():
    values = []
    for fmt in ('T20', 'ListA'):
        f = get_format(fmt)
        engine = PressureEngine(f)
        values.append([engine.calculate_unified_risk_factor(dict(
            innings=2, current_over=int(f.death_phase.start + fraction * (f.overs-f.death_phase.start)),
            current_ball=round((fraction * (f.overs-f.death_phase.start) % 1) * 6),
            overs_remaining=3,
            required_run_rate=10)) for fraction in (0, .5, .75)])
    assert values[0] == pytest.approx(values[1])
    assert values[0][0] < values[0][1] < values[0][2]


def test_recovery_uses_legal_balls_and_rebuilds_faster():
    history = [event(wicket=True), event(wicket=True)]
    assert collapse_recovery(history) == 1
    assert collapse_recovery(history + [event(extra='Wide')] * 8) == 1
    assert collapse_recovery(history + [event(extra='No Ball')] * 8) == 1
    history += [event(extra='Byes')] * 6
    assert 0 < collapse_recovery(history, True) < collapse_recovery(history) < 1
    assert collapse_recovery(history + [event(wicket=True)]) == 1


@pytest.mark.parametrize('fmt', ['T20', 'ListA'])
def test_collapse_risk_recovers_without_becoming_immune(fmt):
    history = [event(wicket=True), event(wicket=True), event(wicket=True)]
    fresh = state(history, fmt)
    recovered = state(history + [event()] * 6, fmt)
    rebuilding = state(history + [event()] * 6, fmt, rebuilding=True)
    # Isolate collapse from changing momentum/run-rate/partnership context.
    def collapse_multiplier(s):
        s.update(momentum=0, rr_ratio=1, resource_index=.5)
        ledger = WicketRisk()
        apply_game_state_to_probs(WEIGHTS, s, wicket_risk=ledger)
        return ledger.multiplier()
    assert 1 < collapse_multiplier(rebuilding) < collapse_multiplier(recovered) < collapse_multiplier(fresh)


def test_soft_limit_counts_boundary_suppression_and_preserves_other_ratios():
    extreme = dict(WEIGHTS, Wicket=.8, Four=.01, Six=.005)
    result = limit_psychological_odds(extreme, WEIGHTS)
    assert result['Wicket'] < extreme['Wicket']
    assert {k: v for k, v in result.items() if k != 'Wicket'} == {
        k: v for k, v in extreme.items() if k != 'Wicket'}
    assert limit_psychological_odds(WEIGHTS, WEIGHTS) == WEIGHTS


def capture_delivery(monkeypatch, fmt='T20', innings=2, rrr=20, recent=True,
                     pinned=False):
    data = _build_match_data(fmt)
    if pinned:
        from engine.ground_config import get_defaults
        data['ground_config'] = get_defaults(fmt, mutable=True)
        data['ground_config']['active_game_mode'] = 'aggressive'
    m = match_module.Match(data)
    m.innings, m.current_over, m.current_ball = innings, m.overs - 2, 0
    m.score, m.wickets = 150, 4
    m.target = m.score + 2 * rrr if innings == 2 else None
    m.current_bowler = m.bowling_team[0]
    m.bowler_selected_for_over = m.current_over
    m.ball_history = [event(wicket=True)] * 3 if recent else []
    m.recent_wickets_count = 3 if recent else 0
    m.batsman_stats[m.current_striker['name']]['balls'] = 20
    m.current_partnership_balls = 1 if recent else 20
    m.current_partnership_runs = 0 if recent else 20
    monkeypatch.setattr(m.pressure_engine, 'should_trigger_wicket_cluster', lambda *a: True)
    monkeypatch.setattr(match_module, 'print', lambda *a, **k: None)
    captured = {}
    real_outcome = ball_outcome.calculate_outcome
    def outcome(**kwargs):
        captured['effects'] = dict(kwargs['pressure_effects'])
        return real_outcome(**kwargs)
    def choose(population, weights=None, **kwargs):
        if 'Wicket' in population and 'Dot' in population:
            captured['probabilities'] = dict(zip(population, weights))
            return ['Dot']
        return [population[0]]
    monkeypatch.setattr(match_module, 'calculate_outcome', outcome)
    monkeypatch.setitem(vars(ball_outcome.random), 'choices', choose)
    m.next_ball()
    return captured


@pytest.mark.parametrize('fmt', ['T20', 'ListA'])
def test_full_match_path_pools_pressure_cluster_and_gsme(monkeypatch, fmt):
    captured = capture_delivery(monkeypatch, fmt)
    probs = captured['probabilities']
    assert sum(probs.values()) == pytest.approx(1)
    assert all(math.isfinite(v) and v >= 0 for v in probs.values())
    assert 0 < probs['Wicket'] < .35  # composition works before the .40 backstop
    groups = captured['effects']['wicket_risk_factors']
    assert any(group == 'collapse' for group, _ in groups)
    assert any(group == 'urgency' for group, _ in groups)


def test_pinned_game_mode_is_not_treated_as_rate_driven(monkeypatch):
    assert capture_delivery(monkeypatch)['effects']['rate_driven_game_mode']
    assert not capture_delivery(monkeypatch, pinned=True)['effects']['rate_driven_game_mode']


@pytest.mark.parametrize('innings', [1, 2])
def test_emergency_cap_both_innings(monkeypatch, innings):
    captured = {}
    def choose(population, weights=None, **kwargs):
        if 'Wicket' in population and 'Dot' in population:
            captured.update(zip(population, weights))
            return ['Dot']
        return [population[0]]
    monkeypatch.setitem(vars(ball_outcome.random), 'choices', choose)
    ball_outcome.calculate_outcome(
        batter={'name': 'B', 'batting_rating': 1, 'batting_hand': 'Right'},
        bowler={'name': 'W', 'bowling_rating': 99, 'bowling_type': 'Fast',
                'bowling_hand': 'Right', 'fielding_rating': 70},
        pitch='Green', streak={}, over_number=18, batter_runs=0,
        innings=innings, format_config=get_format('T20'),
        pressure_effects={'wicket_modifier': 1000, 'boundary_modifier': .01})
    assert captured['Wicket'] <= LIMITED_OVERS_WICKET_CAP
    assert sum(captured.values()) == pytest.approx(1)


@pytest.mark.parametrize('fmt', ['T20', 'ListA'])
def test_aggression_still_buys_runs_and_costs_wickets(monkeypatch, fmt):
    normal = capture_delivery(monkeypatch, fmt, rrr=8, recent=False)['probabilities']
    aggressive = capture_delivery(monkeypatch, fmt, rrr=20, recent=False)['probabilities']
    run_values = dict(Single=1, Double=2, Three=3, Four=4, Six=6)
    expected = lambda p: sum(p[k] * v for k, v in run_values.items())
    assert expected(aggressive) > expected(normal)
    assert aggressive['Wicket'] > normal['Wicket']


@pytest.mark.parametrize('fmt', ['T20', 'ListA'])
def test_individual_settling_in_is_preserved(fmt):
    def wicket_weight(faced):
        return ball_outcome.compute_weighted_prob(
            'Wicket', .05, 75, 75, 70, 'Hard', 'Fast', {}, 0,
            balls_faced=faced, format_name=fmt)
    assert wicket_weight(0) > wicket_weight(20)
