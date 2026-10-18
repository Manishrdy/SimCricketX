import copy
import pytest

import engine.match as match_module
from engine import ball_outcome
from engine.game_state_engine import make_ball_event
from engine.ground_config import get_defaults
from tests.test_first_innings_cluster_single_roll import _build_match_data


@pytest.fixture
def match_factory(monkeypatch):
    monkeypatch.setattr(match_module, 'print', lambda *a, **k: None)
    def build(fmt='T20', pitch='Flat'):
        data = _build_match_data(fmt)
        data['pitch'] = pitch
        return match_module.Match(data)
    return build


@pytest.mark.parametrize('fmt', ['T20', 'ListA', 'T10', 'Hundred'])
@pytest.mark.parametrize('kind,bat_runs,expected', [
    ('Wide', 0, 1.0), ('No Ball', 0, 1.0), ('No Ball', 4, 1.04),
    ('No Ball', 6, 1.07), ('Byes', 0, .98), ('Leg Bye', 0, .98)])
def test_form_follows_bat_not_penalty_runs(match_factory, fmt, kind, bat_runs, expected):
    match = match_factory(fmt)
    name = match.current_striker['name']
    match.batsman_stats[name].update(form=1.0, runs=0)
    outcome = dict(is_extra=True, extra_type=kind, runs=1+bat_runs,
                   bat_runs=bat_runs, batter_out=False)
    saved = copy.deepcopy(outcome)
    match._update_batter_form(name, outcome)
    assert match.batsman_stats[name]['form'] == pytest.approx(expected)
    assert outcome == saved


@pytest.mark.parametrize('fmt', ['T20', 'ListA', 'T10', 'Hundred'])
def test_next_ball_wide_has_no_form_or_balls_cost(match_factory, monkeypatch, fmt):
    match = match_factory(fmt)
    name = match.current_striker['name']
    match.batsman_stats[name].update(form=1.0, runs=0)
    match.current_bowler = match.bowling_team[0]
    match.bowler_selected_for_over = 0
    monkeypatch.setattr(match_module, 'calculate_outcome', lambda **kw: dict(
        is_extra=True, extra_type='Wide', runs=1, batter_out=False, description='Wide.'))
    match.next_ball()
    assert match.batsman_stats[name]['form'] == 1.0
    assert match.batsman_stats[name]['balls'] == 0


def event(wicket=False, extra=None):
    return make_ball_event(dict(batter_out=wicket, runs=0, is_extra=bool(extra), extra_type=extra))


@pytest.mark.parametrize('fmt', ['T20', 'T10', 'Hundred'])
def test_third_wicket_fades_and_fourth_does_not_restart(match_factory, fmt):
    match = match_factory(fmt)
    match.current_over, match.current_ball, match.wickets = 1, 0, 2
    assert match._flat_track_strength() == 1
    match.wickets = 3
    match.ball_history = [event(wicket=True)]
    assert match._flat_track_strength() == 1
    match.ball_history += [event(extra='Wide'), event(extra='No Ball')]
    assert match._flat_track_strength() == 1
    match.ball_history += [event()] * 2
    assert match._flat_track_strength() == pytest.approx(2/3)
    match.ball_history += [event(wicket=True)]
    match.wickets = 4
    assert match._flat_track_strength() == .5
    match.ball_history += [event(extra='Byes')] * 3
    assert match._flat_track_strength() == 0
    assert match._get_dynamic_game_mode() == 'natural_game'


@pytest.mark.parametrize('fmt', ['T20', 'T10', 'Hundred'])
def test_phase_exit_tapers_on_legal_ball_clock(match_factory, fmt):
    match = match_factory(fmt)
    cutoff = (match.fmt.overs / 2 if match.fmt.strict_short_bowling else 10) * match.balls_per_over
    values = []
    for left in (6, 3, 1, 0):
        match.current_over, match.current_ball = divmod(int(cutoff-left), match.balls_per_over)
        values.append(match._flat_track_strength())
    assert values == pytest.approx([1, .5, 1/6, 0])


def test_pinned_mode_and_lista_are_not_faded(match_factory):
    match = match_factory()
    match.ground_config = get_defaults('T20', mutable=True)
    match.ground_config['active_game_mode'] = 'flat_track_bully'
    match.wickets, match.current_over = 8, 15
    assert match._resolve_game_mode() == 'flat_track_bully'
    assert match_factory('ListA')._flat_track_strength() == 0


def test_actual_distribution_changes_gradually(match_factory, monkeypatch):
    match = match_factory()
    captured = {}
    def choose(population, weights=None, **kw):
        if 'Wicket' in population and 'Dot' in population:
            captured.update(zip(population, weights))
            return ['Dot']
        return [population[0]]
    monkeypatch.setitem(vars(ball_outcome.random), 'choices', choose)
    rows = []
    for strength in (1, 5/6, .5, 1/6, 0):
        ball_outcome.calculate_outcome(
            batter=match.current_striker, bowler=match.bowling_team[0], pitch='Flat',
            streak={}, over_number=4, batter_runs=10, balls_faced=10,
            format_config=match.fmt, ground_config_override=match.ground_config,
            game_mode_override='flat_track_bully', pressure_effects={
                'automatic_flat_blend': dict(strength=strength, replacement='natural_game')})
        rows.append(dict(captured))
    for before, after in zip(rows, rows[1:]):
        assert before['Wicket'] < after['Wicket']
        assert before['Four'] > after['Four']
        assert before['Six'] > after['Six']
    assert rows[1]['Wicket'] / rows[0]['Wicket'] < 1.10


@pytest.mark.parametrize('fmt', ['T20', 'ListA', 'T10', 'Hundred'])
def test_next_ball_no_ball_boundary_credits_form(match_factory, monkeypatch, fmt):
    match = match_factory(fmt)
    name = match.current_striker['name']
    match.batsman_stats[name].update(form=1.0, runs=0)
    match.current_bowler = match.bowling_team[0]
    match.bowler_selected_for_over = 0
    outcomes = iter([
        dict(is_extra=True, extra_type='No Ball', runs=1, batter_out=False, description='No ball.'),
        dict(is_extra=False, runs=4, batter_out=False, description='Four.')])
    monkeypatch.setattr(match_module, 'calculate_outcome', lambda **kw: next(outcomes))
    match.next_ball()
    assert match.batsman_stats[name]['form'] == pytest.approx(1.04)


@pytest.mark.parametrize('pinned', [False, True])
def test_next_ball_supplies_blend_only_for_automatic_mode(match_factory, monkeypatch, pinned):
    match = match_factory()
    if pinned:
        match.ground_config = get_defaults('T20', mutable=True)
        match.ground_config['active_game_mode'] = 'flat_track_bully'
    match.wickets, match.current_over = 3, 2
    match.ball_history = [event(wicket=True)] + [event()] * 3
    match.current_bowler = match.bowling_team[0]
    match.bowler_selected_for_over = 2
    captured = {}
    def outcome(**kwargs):
        captured.update(kwargs['pressure_effects'])
        return dict(runs=0, batter_out=False, is_extra=False, description='Dot.')
    monkeypatch.setattr(match_module, 'calculate_outcome', outcome)
    match.next_ball()
    if pinned:
        assert 'automatic_flat_blend' not in captured
    else:
        assert captured['automatic_flat_blend'] == dict(strength=.5, replacement='natural_game')


def test_many_extras_cannot_expire_the_fade(match_factory):
    match = match_factory()
    match.wickets, match.current_over, match.current_ball = 3, 2, 1
    match.recent_wickets_tracker = [12]
    match.ball_history = [event(extra='Wide')] * 18
    assert match._flat_track_strength() == 1


def test_aggressive_replacement_has_no_endpoint_jump(match_factory, monkeypatch):
    match = match_factory()
    captured = {}
    def choose(population, weights=None, **kwargs):
        if 'Wicket' in population and 'Dot' in population:
            captured.update(zip(population, weights))
            return ['Dot']
        return [population[0]]
    monkeypatch.setitem(vars(ball_outcome.random), 'choices', choose)
    rows = []
    for mode in ('flat_track_bully', 'aggressive'):
        effects = dict(wicket_modifier=3, wicket_risk_factors=[('urgency', 3)])
        if mode == 'flat_track_bully':
            effects['automatic_flat_blend'] = dict(strength=0, replacement='aggressive')
        else:
            effects['rate_driven_game_mode'] = True
        ball_outcome.calculate_outcome(
            batter=match.current_striker, bowler=match.bowling_team[0], pitch='Flat',
            streak={}, over_number=4, batter_runs=10, balls_faced=10, innings=2,
            format_config=match.fmt, ground_config_override=match.ground_config,
            game_mode_override=mode, pressure_effects=effects)
        rows.append(dict(captured))
    assert rows[0] == pytest.approx(rows[1])
