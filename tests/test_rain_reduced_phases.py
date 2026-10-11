"""Rain-reduced T20 / List A innings rebuild their phases and par."""
import copy
import json

import pytest

import engine.match as match_module
from engine.format_config import get_format
from engine.game_state_engine import get_par_score
from engine.stadium_tuning import phase_ranges
from tests.test_bowler_consecutive_guards import _build_match_data

SHAPES = [('T20', 20), ('ListA', 50), ('ListA', 40)]


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(match_module, 'print', lambda *args, **kwargs: None)


@pytest.mark.parametrize('name,scheduled', SHAPES)
def test_full_length_is_unchanged(name, scheduled):
    base, fmt = get_format(name, scheduled), get_format(name, scheduled)
    fmt.revise_reduced_innings(scheduled)
    assert fmt.powerplay_phases == base.powerplay_phases
    assert (fmt.middle_phase, fmt.death_phase) == (base.middle_phase, base.death_phase)
    assert fmt.par_scores == base.par_scores


@pytest.mark.parametrize('name,scheduled,overs', [
    (name, scheduled, overs) for name, scheduled in SHAPES
    for overs in range(5 if name == 'T20' else 20, scheduled)])
def test_reduced_innings_has_every_phase(name, scheduled, overs):
    fmt = get_format(name, scheduled)
    fmt.revise_reduced_innings(overs)
    keys = [fmt.phase_key(over) for over in range(overs)]
    assert keys[0] == fmt.powerplay_phases[0].name and keys[-1] == 'Death'
    assert keys == sorted(keys, key=[fmt.powerplay_phases[0].name, 'Middle', 'Death'].index)
    assert fmt.death_phase.end == overs - 1
    ranges = phase_ranges(fmt)
    assert ranges['powerplay'][0] == 0 and ranges['death'][1] == overs * 6
    assert ranges['death'][0] < ranges['death'][1]
    assert set(fmt.par_scores) == set(range(overs + 1))
    assert all(fmt.par_scores[o] < fmt.par_scores[o + 1] for o in range(overs))
    assert fmt.scheduled_overs == scheduled


def test_repeated_reductions_scale_from_full_length():
    direct, twice = get_format('T20'), get_format('T20')
    direct.revise_reduced_innings(8)
    twice.revise_reduced_innings(15)
    twice.revise_reduced_innings(8)
    assert (twice.powerplay_phases, twice.death_phase, twice.par_scores) == \
        (direct.powerplay_phases, direct.death_phase, direct.par_scores)


@pytest.mark.parametrize('name,overs,death_start', [('T20', 5, 4), ('T20', 12, 10), ('ListA', 20, 16)])
def test_match_reduction_rebuilds_phases_and_par(name, overs, death_start):
    m = match_module.Match(_build_match_data(name))
    m.death_overs_plan, m.death_overs_plan_start = ['stale'], 16
    m._set_innings_overs(overs)
    assert m.fmt.death_phase.start == death_start and m.fmt.is_death(overs - 1)
    assert m.fmt.max_bowler_overs == max(1, -(-overs // 5))
    assert m.death_overs_plan == [] and m.death_overs_plan_start is None
    assert get_par_score(overs, m.fmt) == pytest.approx(m.fmt.par_scores[overs])
    assert m.original_overs == m.fmt.scheduled_overs


def test_reduced_t20_plays_death_overs_and_completes():
    data = _build_match_data('T20')
    data['weather_script'] = {'forecast': 'rain', 'events': [{'at_global_over': 0, 'overs_lost': 15}]}
    m = match_module.Match(data)
    phases = set()
    for _ in range(400):
        result = m.next_ball()
        assert not result.get('error'), result
        if m.innings == 1 and m.overs == 5:
            phases.add(m.fmt.phase_key(m.current_over))
        if result.get('match_over') or result.get('super_over_required'):
            break
    assert m.rain_affected and m.overs == 5
    assert {'Powerplay', 'Middle', 'Death'} <= phases


def test_super_over_restore_keeps_reduced_t20_phases(monkeypatch):
    data = _build_match_data('T20')
    data['weather_script'] = {'forecast': 'rain', 'events': [{'at_global_over': 0, 'overs_lost': 8}]}
    m = match_module.Match(copy.deepcopy(data))
    wides = set()

    def dot(**kwargs):
        wide = m.innings not in wides
        wides.add(m.innings)
        return {'runs': int(wide), 'batter_out': False, 'is_extra': wide,
                'extra_type': 'Wide' if wide else None, 'description': 'Dot'}

    monkeypatch.setattr(match_module, 'calculate_outcome', dot)
    for _ in range(400):
        result = m.next_ball()
        assert not result.get('error'), result
        if result.get('super_over_required'):
            break
    else:
        pytest.fail('Expected a tied rain-reduced match to start a Super Over')
    assert m.overs == 12
    restored = match_module.Match(copy.deepcopy(data))
    restored.restore_super_over_snapshot(json.loads(json.dumps(m.serialize_super_over_snapshot())))
    assert restored.overs == 12 and restored.fmt.death_phase.start == 10
    assert restored.fmt.max_bowler_overs == 3 and restored.rain_events_log == m.rain_events_log
    assert restored.super_over_phase == m.super_over_phase
