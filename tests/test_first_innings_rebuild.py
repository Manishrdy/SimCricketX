"""The first innings' answer to a cluster of wickets.

Until 2026-09-21 a side batting first had no way to dig in. Every layer in the
engine pushed the same direction once wickets fell — more wickets AND fewer
boundaries — and at seven down `_get_dynamic_game_mode` flipped the whole
innings into "bowlers_day" (Four x0.65, Six x0.50, Wicket x1.50). Measured on a
Green T20, crossing that line halved the chance of a boundary and raised the
chance of a wicket by three quarters, so past it an innings could not come
back: 12 first innings in 200 ended under 40, five of them under 20.

`calculate_defensive_factor` — the equivalent brake — only ever applied to a
chase. These pin the two halves of the fix.
"""

import pytest

from engine.format_config import get_format
from engine.pressure_engine import (PressureEngine, REBUILD_EARLY, REBUILD_DEEP,
                                    REBUILD_BOUNDARY_CUT, REBUILD_WICKET_CUT)
import engine.match as match_module
from tests.test_bowler_consecutive_guards import _build_match_data


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(match_module, 'print', lambda *a, **k: None)


def _engine(fmt='T20'):
    return PressureEngine(format_config=get_format(fmt))


def _state(over, wickets, fmt='T20', innings=1):
    f = get_format(fmt)
    return {'innings': innings, 'current_over': over, 'wickets': wickets,
            'overs_remaining': f.overs - over, 'pitch': 'Green', 'score': 40,
            'current_run_rate': 6.0}


# --- the trapdoor -----------------------------------------------------------

def test_seven_down_no_longer_flips_the_innings():
    m = match_module.Match(_build_match_data('T20'))
    m.innings, m.wickets, m.current_over = 1, 8, 6
    assert m._get_dynamic_game_mode() != 'bowlers_day'
    # Nine down mid-innings is still not a reason to stop scoring.
    m.wickets = 9
    assert m._get_dynamic_game_mode() != 'bowlers_day'


def test_bowlers_day_still_available_when_a_user_pins_it():
    """Only the automatic trigger went; the mode itself is untouched."""
    from engine.ground_config import get_defaults
    cfg = get_defaults('T20', mutable=True)
    cfg['active_game_mode'] = 'bowlers_day'
    data = _build_match_data('T20')
    data['ground_config'] = cfg
    m = match_module.Match(data)
    assert m._resolve_game_mode() == 'bowlers_day'


# --- digging in -------------------------------------------------------------

def test_a_side_in_trouble_can_dig_in():
    # Five down in the sixth over is well ahead of an all-out-at-the-close pace.
    effects = _engine().calculate_rebuild_factor(_state(over=6, wickets=5))
    assert effects and effects['rebuild_active']
    # The trade has to work: wickets are protected by more than scoring costs.
    assert effects['wicket_reduction'] > effects['boundary_reduction']
    assert effects['single_boost'] > 1.0      # rotate the strike, keep moving
    assert effects['wicket_reduction'] <= REBUILD_WICKET_CUT
    assert effects['boundary_reduction'] <= REBUILD_BOUNDARY_CUT


def test_trouble_is_judged_against_the_stage_of_the_innings():
    """Five down in the sixth over is a crisis; in the fourteenth it is an
    ordinary innings about to accelerate."""
    engine = _engine()
    assert REBUILD_EARLY < REBUILD_DEEP
    assert engine.calculate_rebuild_factor(_state(over=3, wickets=REBUILD_EARLY))
    assert engine.calculate_rebuild_factor(
        _state(over=13, wickets=REBUILD_EARLY)) is None
    # Deeper trouble still registers in the second half.
    assert engine.calculate_rebuild_factor(_state(over=13, wickets=REBUILD_DEEP))


def test_no_rebuilding_at_the_death_or_when_chasing():
    engine = _engine()
    # At the death a side seven down swings — that is what the phase is for.
    assert engine.calculate_rebuild_factor(_state(over=18, wickets=7)) is None
    # Out of overs to rebuild in.
    assert engine.calculate_rebuild_factor(_state(over=15, wickets=8)) is None
    # A chase has calculate_defensive_factor instead.
    assert engine.calculate_rebuild_factor(_state(over=6, wickets=5, innings=2)) is None


def test_short_formats_carry_no_brake_in_either_innings():
    """T10 keeps both halves of the match playing alike — see docs/t10.md."""
    engine = _engine('T10')
    assert engine.calculate_rebuild_factor(_state(over=3, wickets=6, fmt='T10')) is None
    assert engine.calculate_defensive_factor(
        _state(over=8, wickets=8, fmt='T10', innings=2)) is None


def test_rebuild_strength_scales_with_how_bad_it_is():
    engine = _engine()
    mild = engine.calculate_rebuild_factor(_state(over=6, wickets=5))
    dire = engine.calculate_rebuild_factor(_state(over=6, wickets=8))
    assert dire['wicket_reduction'] > mild['wicket_reduction']
    assert dire['rebuild_level'] > mild['rebuild_level']


# --- the state gap that made the above impossible ---------------------------

def test_overs_remaining_is_available_in_the_first_innings():
    """It used to be set only when chasing, so no first-innings logic could
    ask how much of the innings was left."""
    m = match_module.Match(_build_match_data('T20'))
    m.innings, m.current_over, m.current_ball = 1, 5, 3
    state = m._calculate_current_match_state()
    assert state['overs_remaining'] == pytest.approx(20 - 5.5)
    assert 'required_run_rate' not in state      # still chase-only, correctly
