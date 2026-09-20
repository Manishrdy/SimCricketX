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
