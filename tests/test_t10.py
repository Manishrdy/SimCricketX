"""T10 rules and delivery contracts, including a test-only T5 preset."""
import copy
import json
import logging
from collections import Counter

import pytest

import engine.match as match_module
from engine.format_config import get_format, get_any_format, FORMAT_REGISTRY
from engine.short_bowler_manager import ShortBowlerManager
from engine.weather import min_overs_for_result, generate_weather_script, resolve_interruption
from tests.test_bowler_consecutive_guards import _build_match_data, _build_team


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(match_module, 'print', lambda *a, **k: None)


def make_match(bowlers=5, **values):
    data = _build_match_data('T10', bowlers)
    data.update(values)
    return match_module.Match(data)


def test_rules_and_isolation():
    f = get_format('T10')
    assert (f.overs, f.scheduled_overs, f.max_bowler_overs) == (10, 10, 2)
    assert [f.phase_key(o) for o in (0, 2, 3, 6, 7, 9)] == ['Powerplay'] * 2 + ['Middle'] * 2 + ['Death'] * 2
    assert [f.max_fielders_outside(o) for o in (2, 3, 7)] == [2, 5, 5]
    assert set(f.par_scores) == set(range(11))
    f.revise_short_innings(5)
    assert get_format('T10').overs == 10 and get_format('T20').overs == 20
    assert min_overs_for_result('T10') == 3
    a, b = make_match(), make_match()
    a.ground_config['pitch_profiles']['Hard']['run_factor'] = 99
    assert b.ground_config['pitch_profiles']['Hard']['run_factor'] != 99
    supplied = get_format_ground()
    a, b = make_match(ground_config=supplied), make_match(ground_config=supplied)
    a.ground_config['pitch_profiles']['Hard']['run_factor'] = 99
    assert b.ground_config == supplied


@pytest.mark.parametrize('length', [5, 20, True, 10.0, 'nonsense'])
def test_invalid_scheduled_length(length):
    with pytest.raises(ValueError):
        get_any_format('T10', scheduled_overs=length)


@pytest.mark.parametrize('overs', range(3, 11))
def test_reduced_allocation(overs):
    fmt = get_format('T10'); fmt.revise_short_innings(overs)
    manager = ShortBowlerManager(_build_team('A'), fmt)
    manager.validate_attack()
    sequence = []
    for over in range(overs):
        pool = manager.get_eligible_bowlers(over, overs-over)
        assert pool
        # Adversarial preference for the most-used bowler.
        choice = max(pool, key=lambda p: manager.overs_bowled(p['name']))
        sequence.append(choice['name'])
        manager.record_over_completion(choice['name'], 0)
    used = Counter(sequence)
    assert all(a != b for a, b in zip(sequence, sequence[1:]))
    base, extra = divmod(overs, 5)
    assert max(used.values()) <= fmt.max_bowler_overs
    assert sum(n > base for n in used.values()) <= extra
    assert fmt.powerplay_phases[0].end + 1 == (1 if overs <= 4 else 2 if overs <= 8 else 3)
    assert fmt.scheduled_overs == 10
    assert set(fmt.par_scores) == set(range(overs + 1))


def test_reduction_preserves_used_overs_and_blocks_extra_slots():
    m = make_match()
    names = [p['name'] for p in m.bowling_team if p['will_bowl']]
    for name in names[:2] * 2:
        m.bowler_manager.record_over_completion(name, 0)
    m.current_over = 4
    m._set_innings_overs(6)
    pool = m.bowler_manager.get_eligible_bowlers(4, 2)
    assert pool and not ({p['name'] for p in pool} & set(names[:2]))
    assert m.bowler_history[names[0]] == 2
    assert m.original_overs == m.data['scheduled_overs'] == 10


def test_manual_choices_preserve_finish_and_revalidate():
    m = make_match(simulation_mode='manual')
    names = [p['name'] for p in m.bowling_team if p['will_bowl']]
    # One bowler has two overs left with only two innings overs remaining.
    m.current_over = 8
    m.bowler_history.update(dict(zip(names, (0, 2, 2, 2, 2))))
    assert m._get_manual_bowler_candidates() == []
    m.bowler_history.update(dict(zip(names, (1, 1, 2, 2, 2))))
    options = m._create_next_bowler_decision()['options']
    assert len(options) == 2
    m.bowler_history[names[0]] = 2
    selected = next(o['index'] for o in options if o['name'] == names[0])
    result, status = m.submit_pending_decision(selected)
    assert status == 400


@pytest.mark.parametrize('bowlers', [5, 6, 8, 11])
def test_sixty_legal_balls_extras_and_tie(monkeypatch, bowlers):
    m = make_match(bowlers)
    delivered = {}
    wides = set()
    def dot(**kwargs):
        key = (m.innings, m.current_over)
        name = kwargs['bowler']['name']
        assert delivered.setdefault(key, name) == name
        extra = m.innings not in wides
        wides.add(m.innings)
        return {'runs': int(extra), 'batter_out': False, 'is_extra': extra,
                'extra_type': 'Wide' if extra else None, 'description': 'Dot'}
    monkeypatch.setattr(match_module, 'calculate_outcome', dot)
    for _ in range(150):
        result = m.next_ball()
        assert not result.get('error'), result
        if result.get('super_over_required'):
            break
    else:
        pytest.fail('Expected a tie after 60 legal balls per side')
    for innings in (1, 2):
        names = [delivered[(innings, o)] for o in range(10)]
        assert max(Counter(names).values()) <= 2
        assert all(a != b for a, b in zip(names, names[1:]))
    for stats in (m.bowler_stats, m.first_innings_bowling_stats):
        assert sum(s['balls_bowled'] for s in stats.values()) == 60
    restored = match_module.Match(copy.deepcopy(m.data))
    restored.restore_super_over_snapshot(json.loads(json.dumps(m.serialize_super_over_snapshot())))
    assert restored.fmt.name == 'T10' and restored.original_overs == 10
    assert restored.score == m.score and restored.super_over_phase == m.super_over_phase


def test_five_over_preset(monkeypatch):
    from engine.format_catalog import FORMAT_CATALOG
    fmt = get_format('T10'); fmt.name = 'T5'; fmt.scheduled_overs = 5; fmt.revise_short_innings(5)
    monkeypatch.setitem(FORMAT_REGISTRY, 'T5', fmt)
    monkeypatch.setitem(FORMAT_CATALOG, 'T5', {'lengths': [5]})
    data = _build_match_data('T5'); data['ground_config'] = get_format_ground()
    m = match_module.Match(data)
    monkeypatch.setattr(match_module, 'calculate_outcome', lambda **kw: {
        'runs': 0, 'batter_out': False, 'is_extra': False, 'description': 'Dot'})
    for _ in range(65):
        response = m.next_ball()
        assert not response.get('error')
        if response.get('super_over_required'): break
    else: pytest.fail('T5 did not complete')
    assert sum(s['balls_bowled'] for s in m.bowler_stats.values()) == 30
    assert max(s['balls_bowled'] for s in m.bowler_stats.values()) == 6


def get_format_ground():
    from engine.ground_config import get_defaults
    return get_defaults('T10', mutable=True)


def test_weather_thresholds_and_single_event():
    import random
    for seed in range(30):
        assert len(generate_weather_script('storm_warning', 10, 'T10', random.Random(seed))['events']) <= 1
    assert resolve_interruption(2, 2, 10, 9, 'T10')['type'] == 'no_result'
    assert resolve_interruption(2, 3, 10, 9, 'T10')['type'] == 'chase_terminated'


def test_invalid_attack():
    with pytest.raises(ValueError, match='five designated'):
        make_match(4)


@pytest.mark.parametrize('overs', range(3, 10))
def test_rain_reduced_match_and_super_over_recovery(monkeypatch, overs):
    m = make_match(weather_script={'forecast': 'rain_around', 'events': [
        {'at_global_over': 0, 'overs_lost': 10 - overs}]})
    monkeypatch.setattr(match_module, 'calculate_outcome', lambda **kw: {
        'runs': 0, 'batter_out': False, 'is_extra': False, 'description': 'Dot'})
    for _ in range(130):
        response = m.next_ball()
        assert not response.get('error'), response
        if response.get('super_over_required'):
            break
    else:
        pytest.fail('Reduced match did not reach a tie')
    assert m.overs == overs and m.original_overs == 10
    assert sum(s['balls_bowled'] for s in m.bowler_stats.values()) == overs * 6
    restored = match_module.Match(copy.deepcopy(m.data))
    restored.restore_super_over_snapshot(json.loads(json.dumps(m.serialize_super_over_snapshot())))
    assert restored.overs == restored.fmt.overs == overs
    assert restored.fmt.scheduled_overs == 10
    assert restored.fmt.powerplay_phases == m.fmt.powerplay_phases
    assert restored.rain_events_log == m.rain_events_log
    assert restored.dls_ledger_innings1.to_dict() == m.dls_ledger_innings1.to_dict()


@pytest.mark.parametrize('kind', ['No Ball', 'Byes', 'Leg Bye'])
def test_extra_legal_delivery_accounting(monkeypatch, kind):
    m = make_match()
    monkeypatch.setattr(match_module, 'calculate_outcome', lambda **kw: {
        'runs': 1, 'batter_out': False, 'is_extra': True, 'extra_type': kind, 'description': kind})
    result = m.next_ball()
    assert not result.get('error')
    assert m.current_ball == (0 if kind == 'No Ball' else 1)


def test_mid_over_chase_completion(monkeypatch):
    m = make_match()
    monkeypatch.setattr(match_module, 'calculate_outcome', lambda **kw: {
        'runs': 0 if m.innings == 1 else 1, 'batter_out': False,
        'is_extra': False, 'description': 'Single'})
    for _ in range(65):
        response = m.next_ball()
        if response.get('match_over'):
            break
    else:
        pytest.fail('Chase did not complete on its first legal ball')
    assert m.score == 1 and m.current_over == 0 and m.current_ball == 1


@pytest.mark.parametrize('pitch', ['Green', 'Dry', 'Hard', 'Flat', 'Dead'])
def test_player_quality_and_pitch_style_effects(pitch):
    from engine.ball_outcome import compute_weighted_prob
    cfg = get_format_ground()
    def profile(batting, bowling, style='Fast'):
        weights = {outcome: compute_weighted_prob(outcome, weight, batting, bowling, 70,
            pitch, style, {}, 0, 10, 'T10', cfg)
            for outcome, weight in cfg['pitch_profiles'][pitch]['scoring_matrix'].items()}
        return {outcome: value / sum(weights.values()) for outcome, value in weights.items()}
    weak, strong = profile(25, 70), profile(95, 70)
    assert strong['Wicket'] < weak['Wicket']
    assert strong['Four'] + strong['Six'] > weak['Four'] + weak['Six']
    assert profile(70, 95)['Wicket'] > profile(70, 25)['Wicket']
    if pitch == 'Green':
        assert profile(70, 70)['Wicket'] > profile(70, 70, 'Off spin')['Wicket']
    elif pitch == 'Dry':
        assert profile(70, 70)['Wicket'] < profile(70, 70, 'Off spin')['Wicket']


@pytest.mark.parametrize('final_ball', [False, True])
def test_ten_wickets_and_final_ball_wicket(monkeypatch, final_ball):
    m = make_match()
    def outcome(**kwargs):
        ball = m.current_over * 6 + m.current_ball
        wicket = ball < (9 if final_ball else 10) or ball == 59
        return {'runs': 0, 'batter_out': wicket, 'is_extra': False,
                'wicket_type': 'Bowled' if wicket else None, 'description': 'Bowled' if wicket else 'Dot'}
    monkeypatch.setattr(match_module, 'calculate_outcome', outcome)
    for _ in range(65):
        response = m.next_ball()
        assert not response.get('error')
        if response.get('innings_end'): break
    else: pytest.fail('Ten wickets did not end innings')
    assert sum(s['balls_bowled'] for s in m.first_innings_bowling_stats.values()) == (60 if final_ball else 10)


def test_rain_below_three_overs_is_no_result(monkeypatch):
    m = make_match(weather_script={'forecast': 'storm_warning', 'events': [
        {'at_global_over': 0, 'overs_lost': 8}]})
    monkeypatch.setattr(m, '_create_match_archive', lambda: None)
    response = m.next_ball()
    assert response.get('match_over') and m.match_status == 'no_result'
    assert m.original_overs == 10


def test_repeated_super_overs_use_t10_and_finish(monkeypatch):
    m = make_match()
    monkeypatch.setattr(m, '_create_match_archive', lambda: None)
    dot = {'runs': 0, 'batter_out': False, 'is_extra': False, 'description': 'Dot'}
    monkeypatch.setattr(match_module, 'calculate_outcome', lambda **kw: dict(dot))
    for _ in range(125):
        if m.next_ball().get('super_over_required'): break
    else: pytest.fail('Expected a tied main match')
    monkeypatch.setattr(match_module, 'calculate_super_over_outcome', lambda **kw: dict(
        dot, runs=int(m.super_over_round >= 3 and m.super_over_innings == 2)))
    for _ in range(70):
        if m.super_over_phase == 'complete': break
        if m.super_over_phase == 'awaiting_innings1_selection':
            response = m.start_super_over(getattr(m, '_super_over_next_first_batting', None) or 'home')
        elif m.super_over_phase == 'awaiting_innings2_selection':
            response = m.start_super_over_innings2()
        else:
            response = m.next_super_over_ball()
        assert not response.get('error'), response
    else: pytest.fail('Third Super Over did not produce a result')
    assert m.super_over_round == 3 and len(m.super_over_history) == 3
    assert m.fmt.name == 'T10' and m.original_overs == 10


# ---------------------------------------------------------------------------
# Innings parity
#
# A T10 is meant to read the same in both halves: if the side batting first
# hits big, the chase hits big back, and wickets fall in the last three overs
# as the price of that hitting rather than as a collapse. Before this, six
# separate chase-only wicket amplifiers stacked while the boundary payoff was
# capped, and a T10 chase lost 27-88% more wickets per ball than the first
# innings while scoring no faster. These pin the shape so it cannot drift back;
# the measured behaviour is gated separately by scripts/bench_t10.py.
# ---------------------------------------------------------------------------

def _pressure(fmt_name):
    from engine.pressure_engine import PressureEngine
    return PressureEngine(format_config=get_format(fmt_name))


def _chase_state(**over):
    state = {'innings': 2, 'current_over': 8, 'wickets': 7, 'overs_remaining': 2,
             'pitch': 'Hard', 'required_run_rate': 18.0, 'runs_needed': 36,
             'current_run_rate': 11.0, 'score': 90}
    state.update(over)
    return state


def test_no_defensive_shutdown_in_a_short_chase():
    # Death overs, 8 down: T20 switches to blocking (boundaries x0.7, dots
    # +0.3). There is no draw to bat out in a 10-over game.
    state = _chase_state(wickets=8)
    assert _pressure('T10').calculate_defensive_factor(state) is None
    t20 = _pressure('T20').calculate_defensive_factor(dict(state, current_over=18))
    assert t20 and t20['defensive_active']


def test_chasing_advantage_is_exactly_neutral_for_short_formats():
    t10 = _pressure('T10').get_chasing_advantage(_chase_state())
    assert (t10['boundary_boost'], t10['wicket_reduction'],
            t10['strike_rotation_boost']) == (1.00, 1.00, 1.00)
    # List A keeps its scoreboard-pressure bias; the branches are separate.
    lista = _pressure('ListA').get_chasing_advantage(
        {'innings': 2, 'current_over': 34, 'wickets': 3})
    assert lista['wicket_reduction'] > 1.0


def test_risk_pays_the_same_in_both_innings_of_a_short_format():
    engine = _pressure('T10')
    first = engine.get_risk_based_effects(
        {'innings': 1, 'current_over': 8, 'wickets': 2, 'score': 80,
         'pitch': 'Hard', 'overs_remaining': 2})
    second = engine.get_risk_based_effects(_chase_state())
    for effects in (first, second):
        assert effects and effects['risk_active']
        # Aggression must buy more runs than wickets, or hitting is a losing
        # trade and the innings shuts down instead.
        assert effects['boundary_boost'] > effects['wicket_boost']
        assert effects['dot_increase'] == 0
    # The two innings read different situations, so their risk FACTORS differ.
    # What has to match is the mapping from risk to reward and to cost — that
    # is what made second-innings aggression a worse trade than first-innings
    # aggression (boundaries x1.2 either way, wickets x1.0 then x1.5).
    for effects in (first, second):
        risk = effects['risk_factor'] - 1.0
        assert round((effects['boundary_boost'] - 1) / risk, 6) == 1.6
        assert round((effects['wicket_boost'] - 1) / risk, 6) == 1.0
    for key in ('dot_increase', 'single_floor'):
        assert first[key] == second[key], key
    # T20 keeps the asymmetric model its bands were calibrated on.
    t20 = _pressure('T20')
    t20_second = t20.get_risk_based_effects(dict(_chase_state(), current_over=18))
    assert t20_second['wicket_boost'] > t20_second['boundary_boost'] / 2


def test_short_chase_keeps_its_shots_when_wickets_fall():
    m = make_match()
    m.innings, m.target, m.score, m.wickets = 2, 130, 70, 8
    m.current_over, m.current_ball = 7, 0
    assert m._get_dynamic_game_mode() == 'aggressive'
    # ...and a first innings seven down is not handed "bowlers_day" either.
    m.innings, m.target, m.wickets = 1, None, 8
    assert m._get_dynamic_game_mode() == 'natural_game'


def test_game_state_vector_publishes_short_format_flags():
    from engine.game_state_engine import compute_game_state_vector
    fmt = get_format('T10')
    death = compute_game_state_vector([], 90, 8, 0, 3, 2, target=130,
                                      pitch='Hard', format_config=fmt)
    assert death['_is_short'] and death['_in_death']
    assert not compute_game_state_vector([], 40, 3, 0, 1, 1, pitch='Hard',
                                         format_config=fmt)['_in_death']
    t20 = compute_game_state_vector([], 90, 8, 0, 3, 1, pitch='Hard',
                                    format_config=get_format('T20'))
    assert not t20['_is_short']


def test_new_batter_at_the_death_still_swings():
    from engine.game_state_engine import apply_game_state_to_probs
    probs = {'Dot': .25, 'Single': .34, 'Double': .12, 'Three': .01,
             'Four': .13, 'Six': .08, 'Wicket': .05, 'Extras': .02}
    fresh = dict(partnership_balls=1, partnership_runs=0, innings=1,
                 _is_short=True, _partnership_thresholds=(15, 30, 45, 60))
    at_death = apply_game_state_to_probs(dict(probs), dict(fresh, _in_death=True))
    mid_innings = apply_game_state_to_probs(dict(probs), dict(fresh, _in_death=False))
    assert at_death['Four'] > mid_innings['Four']
    assert at_death['Six'] > mid_innings['Six']
    # The wicket bump survives either way — a new batter is still the easier out.
    assert at_death['Wicket'] > probs['Wicket'] / sum(probs.values())


def test_t10_par_curve_matches_its_targets():
    fmt = get_format('T10')
    assert fmt.par_scores[10] == fmt.target_scores['Hard'] == 125
    assert fmt.pitch_par_factors['Hard'] == 1.0
    assert fmt.rrr_baseline == {p: t / 10 for p, t in fmt.target_scores.items()}
    # Derived from expected_rr, not hand-written: 3x12.0 + 4x11.0 + 3x15.0.
    assert fmt.expected_rr == {'Powerplay': 12.0, 'Middle': 11.0, 'Death': 15.0}
