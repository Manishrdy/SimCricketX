"""Forty-over List A shares its model/identity but owns its scheduled length."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from engine.format_config import get_any_format, get_format, FORMAT_REGISTRY
from engine.match import Match
from engine.ball_outcome import _apply_dew_factor
from engine.weather import min_overs_for_result
from tests.test_lista_format import _build_match_data, _overs_to_balls


def make_match(seed=4101, pitch='Hard', bowlers=5):
    data = _build_match_data(pitch, seed)
    data['scheduled_overs'] = 40
    data['weather_script'] = {'forecast': 'clear', 'events': []}
    if bowlers > 5:
        for side in ('home', 'away'):
            for p in data['playing_xi'][side][-bowlers:]:
                p['will_bowl'] = True
    return Match(data)


def test_config_boundaries_and_isolation():
    a, b = get_any_format('ListA', scheduled_overs=40), get_any_format('ListA')
    assert (a.name, a.overs, a.max_bowler_overs, a.scheduled_overs) == ('ListA', 40, 8, 40)
    assert [(a.get_phase(i).name, a.get_phase(i).max_fielders_outside) for i in (0,7,8,31,32,39)] == [
        ('PP1',2), ('PP1',2), ('Middle',4), ('Middle',4), ('Death',5), ('Death',5)]
    assert a.par_scores[40] == 232
    assert a.par_scores[8] == b.par_scores[10] * .8
    assert a.target_scores['Hard'] == 228
    a.powerplay_phases[0].end = 99
    a.par_scores[0] = 99
    assert get_format('ListA', 40).powerplay_phases[0].end == 7
    assert b.par_scores[0] == FORMAT_REGISTRY['ListA'].par_scores[0] == 0
    assert b.overs == 50


@pytest.mark.parametrize('fmt,value', [('ListA',20), ('ListA',41), ('ListA',True), ('ListA',40.0), ('T20',40), ('FC',40)])
def test_invalid_lengths(fmt, value):
    with pytest.raises(ValueError):
        get_any_format(fmt, scheduled_overs=value)


def test_dew_uses_scheduled_not_revised_length():
    fmt = get_format('ListA', 40)
    weights = {'Dot': 40, 'Single': 30, 'Four': 15, 'Six': 5, 'Wicket': 5, 'Extras': 5}
    assert _apply_dew_factor(weights, 2, 18, True, fmt) == weights
    peak = _apply_dew_factor(weights, 2, 35, True, fmt)
    assert peak['Four'] > weights['Four']
    fmt.overs = 30
    assert _apply_dew_factor(weights, 2, 35, True, fmt) == peak


@pytest.mark.parametrize('bowlers', [5,7])
@pytest.mark.parametrize('pitch', ['Hard','Dead'])
def test_full_match_quota_and_completion(bowlers, pitch):
    m = make_match(pitch=pitch, bowlers=bowlers)
    cards = []
    for _ in range(1000):
        result = m.next_ball()
        assert not result.get('error'), result
        if result.get('innings_end'):
            cards.append(result['scorecard_data'])
            assert _overs_to_balls(cards[-1]['overs']) <= 240
        if result.get('match_over') or result.get('super_over_required'):
            break
    else:
        pytest.fail('Match did not finish')
    assert cards
    assert m.original_overs == m.data['scheduled_overs'] == m.data['overs'] == 40
    # The first innings scorecard holds the completed bowling figures.
    for stats in [m.first_innings_bowling_stats, m.bowler_stats]:
        assert all(s.get('balls_bowled', 0) <= 48 for s in stats.values())
    for history in [m.bowler_history]:
        assert all(overs <= 8 for overs in history.values())


def test_rain_keeps_length_and_resets_plan():
    m = make_match()
    m._ensure_lista_bowler_plan()
    assert sum(m.lista_bowler_plan.values()) == 40
    assert max(m.lista_bowler_plan.values()) <= 8
    m._set_innings_overs(30)
    assert not m.lista_bowler_plan
    m._ensure_lista_bowler_plan()
    assert sum(m.lista_bowler_plan.values()) == 30
    assert max(m.lista_bowler_plan.values()) <= 6
    assert m.original_overs == m.fmt.scheduled_overs == m.data['scheduled_overs'] == 40
    assert min_overs_for_result('ListA') == 20
    restored = Match(json.loads(json.dumps(m.data)))
    assert restored.original_overs == 40
    assert get_any_format('ListA').overs == 50


def test_scheduled_length_migration_idempotent():
    from contextlib import nullcontext
    from migrations.add_scheduled_overs import run_migration
    engine = create_engine('sqlite://')
    with engine.begin() as c:
        c.execute(text('CREATE TABLE matches (id INTEGER PRIMARY KEY, match_format TEXT, overs_per_side INTEGER)'))
        c.execute(text('CREATE TABLE tournaments (id INTEGER PRIMARY KEY, format_type TEXT)'))
        c.execute(text("INSERT INTO matches VALUES (1,'ListA',40), (2,'T20',12), (3,'FC',360)"))
        c.execute(text("INSERT INTO tournaments VALUES (1,'ListA'), (2,'T20'), (3,'FC')"))
    app = SimpleNamespace(app_context=nullcontext)
    run_migration(SimpleNamespace(engine=engine), app)
    with engine.begin() as c:
        c.execute(text("INSERT INTO matches VALUES (4,'ListA',40,40)"))
    run_migration(SimpleNamespace(engine=engine), app)
    with engine.connect() as c:
        assert c.execute(text('SELECT scheduled_overs FROM matches ORDER BY id')).scalars().all() == [50,20,None,40]
        assert c.execute(text('SELECT scheduled_overs FROM tournaments ORDER BY id')).scalars().all() == [50,20,None]


def test_exact_240_legal_balls_extras_consecutive_and_super_over_resume(monkeypatch):
    import engine.match as match_module
    m = make_match()
    deliveries = {}
    wide_innings = set()

    def outcome(**kwargs):
        key = (m.innings, m.current_over)
        bowler = kwargs['bowler']['name']
        if key in deliveries:
            assert deliveries[key] == bowler
        deliveries[key] = bowler
        wide = m.innings not in wide_innings
        wide_innings.add(m.innings)
        return {'runs': 1 if wide else 0, 'batter_out': False, 'is_extra': wide,
                'extra_type': 'Wide' if wide else None, 'description': 'Wide.' if wide else 'Defended.'}

    monkeypatch.setattr(match_module, 'calculate_outcome', outcome)
    for _ in range(500):
        result = m.next_ball()
        assert not result.get('error')
        if result.get('super_over_required'):
            break
    else:
        pytest.fail('Expected tied main match to start a Super Over')
    for innings in (1,2):
        names = [deliveries[(innings, over)] for over in range(40)]
        assert all(a != b for a,b in zip(names,names[1:]))
    assert sum(s['balls_bowled'] for s in m.first_innings_bowling_stats.values()) == 240
    assert sum(s['balls_bowled'] for s in m.bowler_stats.values()) == 240
    assert max(s['balls_bowled'] for s in m.bowler_stats.values()) == 48
    snapshot = json.loads(json.dumps(m.serialize_super_over_snapshot()))
    restored = Match(json.loads(json.dumps(m.data)))
    restored.restore_super_over_snapshot(snapshot)
    assert restored.data['scheduled_overs'] == restored.original_overs == 40
    assert restored.score == m.score
    assert restored.super_over_phase == m.super_over_phase


def test_manual_bowler_choices_use_eight_over_quota():
    m = make_match()
    m.simulation_mode = 'manual'
    names = [p['name'] for p in m.bowling_team if p.get('will_bowl')]
    m.bowler_history[names[0]] = 7
    m.bowler_history[names[1]] = 8
    m.bowler_history[names[2]] = 2
    options = {opt['name']:opt for opt in m._create_next_bowler_decision()['options']}
    assert options[names[0]]['overs_remaining'] == 1
    assert names[1] not in options
    assert options[names[2]]['overs_remaining'] == 6


def test_legacy_fifty_over_story_rejected_for_forty():
    data = _build_match_data('Hard',4101)
    data.update(scheduled_overs=40, scenario_pack={'format':'ListA', 'id':'legacy-story', 'beats':{}})
    with pytest.raises(ValueError, match='Story duration'):
        Match(data)
