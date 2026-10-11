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


# --- T20 bowler-picker thresholds scale with the innings length -------------

from engine.ball_outcome import scale_boost_windows


def _t20_match(overs=None):
    m = match_module.Match(_build_match_data('T20'))
    if overs:
        m._set_innings_overs(overs)
    return m


@pytest.mark.parametrize('base', [3, 4, 5, 7, 8, 10, 14, 15])
def test_full_length_marks_are_unchanged(base):
    assert _t20_match()._t20_over_mark(base) == base


@pytest.mark.parametrize('overs,marks', [
    (5, {4: 1, 10: 3, 14: 4}),
    (8, {4: 2, 10: 4, 14: 6}),
    (12, {4: 2, 10: 6, 14: 8}),
])
def test_reduced_marks_scale(overs, marks):
    m = _t20_match(overs)
    for base, want in marks.items():
        assert m._t20_over_mark(base) == want
        assert 1 <= m._t20_over_mark(base) <= overs - 1


def _tiers(m):
    all_b = [p for p in m.bowling_team if p.get('will_bowl', False)]
    tiers = m._classify_bowlers_by_tier(all_b)
    if not tiers['star']:                      # fixture squads have no star: promote one
        tiers['star'] = [tiers['regular'].pop(0)]
    return tiers, m._analyze_quota_status(all_b)


@pytest.mark.parametrize('overs', [5, 8, 12])
def test_star_neglect_fires_in_reduced_innings(overs):
    m = _t20_match(overs)
    tiers, quota = _tiers(m)
    for b in quota.values():
        b['overs_bowled'] = 0
    m.current_bowler = None
    early = m._t20_over_mark(10) - 1
    m.current_over = early
    assert m._prevent_star_neglect(tiers, quota) is None
    m.current_over = m._t20_over_mark(10)
    assert m._prevent_star_neglect(tiers, quota) is not None


def test_star_neglect_unchanged_at_full_length():
    m = _t20_match()
    tiers, quota = _tiers(m)
    for b in quota.values():
        b['overs_bowled'] = 0
    m.current_bowler = None
    m.current_over = 9
    assert m._prevent_star_neglect(tiers, quota) is None
    m.current_over = 10
    assert m._prevent_star_neglect(tiers, quota) is not None


@pytest.mark.parametrize('overs,over,expected', [(5, 0, True), (5, 1, False), (12, 1, True), (12, 2, False)])
def test_new_ball_override_window_scales(monkeypatch, overs, over, expected):
    m = _t20_match(overs)
    calls = []
    monkeypatch.setattr(m, '_try_early_overs_fast_selection', lambda *a: calls.append(1))
    m.current_over, m.current_bowler = over, None
    m.bowler_selected_for_over = -1
    m.pick_bowler()
    assert bool(calls) is expected


def test_new_ball_override_window_unchanged_at_full_length(monkeypatch):
    for over, expected in [(3, True), (4, False)]:
        m = _t20_match()
        calls = []
        monkeypatch.setattr(m, '_try_early_overs_fast_selection', lambda *a, c=calls: c.append(1))
        m.current_over, m.current_bowler = over, None
        m.pick_bowler()
        assert bool(calls) is expected


# --- death plan is built once per death phase, even when a cut lands mid-death

def test_mid_death_cut_builds_one_plan():
    m = _t20_match()
    m.current_over = 13
    m._set_innings_overs(15)            # death now starts at 12 -> we are mid-death
    assert m.fmt.death_phase.start == 12
    plans = []
    real = m._calculate_death_overs_plan_safe
    m._calculate_death_overs_plan_safe = lambda q: plans.append(1) or real(q)
    prev = None
    for over in (13, 14):
        m.current_over, m.current_bowler = over, prev
        prev = m._pick_death_overs_bowler()
    assert len(plans) == 1 and m.death_overs_plan_start == 13


def test_death_plan_not_rebuilt_across_death_overs(monkeypatch):
    m = _t20_match(12)
    plans = []
    real = m._calculate_death_overs_plan_safe
    m._calculate_death_overs_plan_safe = lambda q: plans.append(1) or real(q)
    prev = None
    for over in range(10, 12):
        m.current_over, m.current_bowler = over, prev
        prev = m._pick_death_overs_bowler()
    assert len(plans) == 1 and m.death_overs_plan_start == 10


# --- admin boost windows scale with the cut ---------------------------------

def test_boost_windows_full_length_identity():
    assert scale_boost_windows((0, 5), (16, 19), 20, 20) == (0, 5, 16, 19)


@pytest.mark.parametrize('overs,want', [
    (5, (0, 1, 4, 4)), (8, (0, 1, 6, 7)), (12, (0, 3, 10, 11))])
def test_boost_windows_scale_default(overs, want):
    assert scale_boost_windows((0, 5), (16, 19), overs, 20) == want


@pytest.mark.parametrize('overs', range(2, 20))
@pytest.mark.parametrize('pp,death', [((0, 5), (16, 19)), ((0, 9), (10, 19)), ((0, 1), (18, 19)), ((0, 12), (12, 19))])
def test_boost_windows_valid_for_custom(overs, pp, death):
    pp_s, pp_e, d_s, d_e = scale_boost_windows(pp, death, overs, 20)
    assert d_s <= d_e <= overs - 1 and d_s > pp_e and pp_s <= pp_e + 1


def test_custom_window_applies_scaled_in_cut_innings():
    cfg_pp, cfg_death = (0, 9), (14, 19)
    # 10 of 20 overs -> powerplay 0-4, death 7-9
    assert scale_boost_windows(cfg_pp, cfg_death, 10, 20) == (0, 4, 7, 9)
