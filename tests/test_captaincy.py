"""Captain policy behavior, legal allocations, and snapshot compatibility."""
import copy
import json
import math
import random
import statistics
import time

import pytest
from engine.captaincy import (ATTRIBUTES, PRESETS, apply_tactics, batting_scores,
    bowling_scores, choose_action, default_config, parse_config)
from engine.match import Match
from tests.test_scoring_calibration import _match_data


def context(**changes):
    value = dict(team='home', innings='1', legal_balls=30, balls_remaining=90,
        score=40, wickets=1, runs_needed=None, par_rate=1.4, recent=[],
        phase='middle', pitch='Hard', pressure=.3)
    value.update(changes)
    return value


def match_data(fmt='T20', pitch='Hard', seed=11, config=None):
    data = _match_data(fmt, pitch, seed)
    data['captaincy_model_version'] = 1
    data['captaincy'] = {side: {'config': copy.deepcopy(config or default_config()),
                             'captain': {'name': data['playing_xi'][side][0]['name']}}
                        for side in ('home', 'away')}
    return data


def quiet_match(data):
    match = Match(copy.deepcopy(data))
    match._create_match_archive = lambda: True
    return match


def test_config_defaults_zero_and_validation():
    assert parse_config(None) == PRESETS['Balanced']
    assert parse_config({'composure': 0})['composure'] == 0
    for key in ATTRIBUTES:
        for value in (-1, 101, True, 50.0, '50', None):
            with pytest.raises(ValueError, match=key.replace('_', ' ').capitalize()):
                parse_config({key: value})
    for invalid in ([], 'balanced', {'schema_version': 2}, {'schema_version': True}):
        with pytest.raises(ValueError):
            parse_config(invalid)


@pytest.mark.parametrize('state,preferred', [
    (context(runs_needed=35, balls_remaining=60), 'balanced'),
    (context(wickets=6, recent=[{'wicket': True}] * 2), 'rebuild'),
    (context(runs_needed=90, balls_remaining=35), 'all_out'),
    (context(runs_needed=12, balls_remaining=3), 'all_out'),
    (context(phase='death'), 'attack'),
])
def test_situation_controls_batting(state, preferred):
    scores, _ = batting_scores(state, default_config())
    assert max(scores, key=scores.get) == preferred


def test_quality_improves_decisions_and_policy_is_fast():
    states = [context(), context(phase='death'), context(wickets=6),
              context(runs_needed=50, balls_remaining=60), context(runs_needed=90, balls_remaining=35)]
    suite = [(state, 'batting', batting_scores) for state in states]
    suite += [(state, 'bowling', bowling_scores) for state in
              [context(), context(phase='death'), context(recent=[{'runs':3}] * 18)]]
    totals = {}
    times = []
    for quality in (0, 50, 100):
        cfg = default_config()
        for key in ATTRIBUTES[:5]:
            cfg[key] = quality
        correct = 0
        for seed in range(1000):
            for state, kind, score_fn in suite:
                started = time.perf_counter()
                scores, _ = score_fn(state, cfg)
                selected = choose_action(scores, cfg, kind, state['pressure'], random.Random(seed))
                times.append(time.perf_counter() - started)
                correct += selected == max(scores, key=scores.get)
        totals[quality] = correct / (1000 * len(suite))
    # Conservative independent-proportions confidence bound.
    delta = totals[100] - totals[0]
    se = math.sqrt(sum(p * (1-p) / (1000 * len(suite)) for p in (totals[0], totals[100])))
    assert delta - 1.96 * se > 0
    assert totals[100] >= totals[50] >= totals[0]
    assert sorted(times)[int(len(times)*.95)] < .005


def test_bowling_plan_reacts_to_sustained_scoring_not_one_six():
    cfg = default_config()
    one, _ = bowling_scores(context(recent=[{'runs': 6}]), cfg)
    sustained, _ = bowling_scores(context(recent=[{'runs': 3}] * 18), cfg)
    assert max(one, key=one.get) == 'balanced'
    assert max(sustained, key=sustained.get) == 'contain'


def test_tactical_effects_preserve_extras_and_are_bounded():
    weights = dict(Dot=.25, Single=.25, Double=.06, Three=.01, Four=.2, Six=.1, Wicket=.08, Extras=.05)
    for batting in ('rebuild', 'balanced', 'attack', 'all_out'):
        for bowling in ('contain', 'balanced', 'attack'):
            result = apply_tactics(weights, dict(batting=batting, bowling=bowling))
            assert result['Extras'] == weights['Extras']
            assert sum(result.values()) == pytest.approx(sum(weights.values()))
            assert all(math.isfinite(x) and x >= 0 for x in result.values())
    assert apply_tactics(weights) is weights
    assert apply_tactics(weights, dict(batting='all_out', bowling='balanced'), is_fc=True) == pytest.approx(weights)


@pytest.mark.parametrize('fmt', ['T10', 'T20', 'ListA', 'FC', 'Hundred'])
def test_enabled_match_smoke_and_status_does_not_draw_randomness(fmt):
    match = quiet_match(match_data(fmt))
    for _ in range(30):
        result = match.next_ball()
        assert not result.get('error'), result
        assert result.get('captaincy')
        if match.current_over >= 2:
            break
    state = match.captaincy_rng_state
    first = match.captaincy_status()
    second = match.captaincy_status()
    assert first == second and state == match.captaincy_rng_state
    assert match.captaincy_events
    assert match.data['captaincy']['home']['config'] == default_config()


@pytest.mark.parametrize('fmt', ['T10', 'T20', 'ListA', 'Hundred'])
def test_complete_legal_allocation(fmt):
    match = quiet_match(match_data(fmt))
    previous = None
    streak = 0
    for over in range(match.overs):
        match.current_over = over
        selected = match.pick_bowler()
        streak = streak + 1 if selected['name'] == previous else 1
        assert streak <= (2 if fmt == 'Hundred' else 1)
        match.bowler_manager.record_over_completion(selected['name'], 5)
        assert match.bowler_manager.overs_bowled(selected['name']) <= match.fmt.max_bowler_overs
        previous = selected['name']


@pytest.mark.parametrize('fmt', ['FC', 'Hundred'])
def test_checkpoint_roundtrip_preserves_next_delivery(fmt):
    data = match_data(fmt)
    match = quiet_match(data)
    for _ in range(12):
        match.next_ball()
    if fmt == 'FC':
        snap = match.serialize_fc_snapshot()
    else:
        from engine.hundred_snapshot import serialize
        snap = serialize(match)
    restored = quiet_match(data)
    snap = json.loads(json.dumps(snap))
    if fmt == 'FC':
        restored.restore_fc_snapshot(snap)
    else:
        from engine.hundred_snapshot import restore
        restore(restored, snap)
    assert restored.captaincy_snapshot() == match.captaincy_snapshot()
    expected = match.next_ball()
    actual = restored.next_ball()
    for key in ('score', 'wickets', 'ball_data', 'captaincy'):
        assert actual.get(key) == expected.get(key)


def test_manual_recommendation_is_cached_and_user_choice_wins():
    data = match_data()
    data['simulation_mode'] = 'manual'
    match = quiet_match(data)
    result = match.next_ball()
    decision = result['decision'] if 'decision' in result else match.pending_decision
    assert decision['recommendation'] in [p['name'] for p in decision['options']]
    stream = match.captaincy_rng_state
    match.next_ball()
    assert match.captaincy_rng_state == stream
    chosen = next((p for p in decision['options'] if not p['recommended']), decision['options'][0])
    payload, status = match.submit_pending_decision(chosen['index'])
    assert status == 200
    assert match.current_bowler['name'] == chosen['name']


def test_legacy_has_no_captaincy_or_extra_random_consumption():
    data = _match_data('T20', 'Hard', 11)
    before = random.getstate()
    match = quiet_match(data)
    assert match.captaincy_enabled is False
    assert match.captaincy_status() is None
    before = random.getstate()
    match._init_captaincy()
    assert random.getstate() == before


def test_fc_risk_comes_from_team_personality_and_zero_is_valid():
    cfg = default_config()
    cfg['batting_aggression'] = 0
    match = quiet_match(match_data('FC', config=cfg))
    assert match._fc_risk_appetite() == pytest.approx(1.4)
    match.captaincy_configs['home']['batting_aggression'] = 100
    assert match._fc_risk_appetite() == pytest.approx(.6)

@pytest.mark.parametrize('fmt', ['T20', 'Hundred'])
def test_shootout_checkpoint_restores_decisions_and_random_stream(fmt):
    data = match_data(fmt)
    data['is_knockout'] = True
    match = quiet_match(data)
    match.innings = 4
    match._setup_super_over()
    started = match.start_super_over('home')
    assert started.get('super_over_started'), started
    match.next_super_over_ball()
    snapshot = json.loads(json.dumps(match.serialize_super_over_snapshot()))
    restored = quiet_match(data)
    restored.restore_super_over_snapshot(snapshot)
    expected = match.next_super_over_ball()
    actual = restored.next_super_over_ball()
    for key in ('ball_data', 'captaincy', 'score', 'wickets'):
        assert actual.get(key) == expected.get(key)
    assert restored.captaincy_snapshot() == match.captaincy_snapshot()


def test_archive_contains_frozen_settings_and_committed_events(tmp_path):
    from match_archiver import MatchArchiver
    data = match_data()
    data['timestamp'] = '2026-10-10T12:00:00'
    data['captaincy_events'] = [{'id': 'captain:1', 'action': 'contain'}]
    match = quiet_match(data)
    archive = MatchArchiver(data, match)
    archive.archive_path = tmp_path / 'archive'
    archive.archive_path.mkdir()
    archive.filenames = {'json': 'match.json'}
    original = tmp_path / 'source.json'
    original.write_text(json.dumps({'match_id': 'test'}))
    archive._copy_json_file(str(original))
    saved = json.loads((archive.archive_path / 'match.json').read_text())
    for key in ('captaincy_model_version', 'captaincy', 'captaincy_events'):
        assert saved[key] == data[key]


def test_adaptability_changes_response_speed_without_delivery_bonuses():
    decisions = {}
    for adaptability in (0, 100):
        cfg = default_config()
        for key in ATTRIBUTES[:5]: cfg[key] = 100
        cfg['adaptability'] = adaptability
        match = quiet_match(match_data(config=cfg))
        match._captain_evaluate()
        side = match._captain_side(match.batting_team)
        bowling = 'away' if side == 'home' else 'home'
        match._captain_state(side)['recent'] = [{'runs': 3, 'wicket': False}] * 18
        match.current_over = 3
        match._captain_evaluate()
        decisions[adaptability] = match._captain_state(bowling)['bowling']
    assert decisions == {0: 'balanced', 100: 'contain'}


def test_fc_captain_evaluation_does_not_start_another_forecast(monkeypatch):
    match = quiet_match(match_data('FC'))
    def forbidden():
        pytest.fail('Policy must reuse the existing FC decision budget')
    monkeypatch.setattr(match, '_fc_prepare_declaration', forbidden)
    match._captain_evaluate()
    match._fc_pick_bowler()


@pytest.mark.parametrize('fmt', ['T10', 'Hundred'])
def test_rain_revised_allocations_finish_with_captaincy(fmt, monkeypatch):
    import engine.match as module
    from tests.test_hundred import dot
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    data = match_data(fmt)
    data['weather_script'] = {'forecast': 'rain_around',
        'events': [{'at_global_over': 3, 'overs_lost': 2}]}
    match = quiet_match(data)
    for _ in range(400):
        response = match.next_ball()
        assert not response.get('error'), response
        if response.get('match_over'): break
    else: pytest.fail('Revised bowling allocation did not finish')
