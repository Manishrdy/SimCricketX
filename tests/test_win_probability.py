"""Server win probability: the batting side's chance, format-aware, both innings."""
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_innings_change_reset import _match


def _limited(match_format="T20", overs=20):
    m = _match()
    m.data["match_format"] = match_format
    from engine.format_config import get_format
    m.fmt = get_format(match_format)
    m.overs = overs
    return m


def _at(m, innings, score=0, wickets=0, over=0, ball=0, target=None):
    m.innings, m.score, m.wickets = innings, score, wickets
    m.current_over, m.current_ball, m.target = over, ball, target
    return m._calculate_win_probability()


@pytest.mark.parametrize("fmt,overs,par", [("T20", 20, 165), ("ListA", 50, 300)])
def test_chase_of_par_starts_near_even(fmt, overs, par):
    # A List A chase used to be scored at T20 rates: ~98% for the chasing side
    # at the first ball of any par chase.
    assert 40 <= _at(_limited(fmt, overs), 2, target=par + 1) <= 60


@pytest.mark.parametrize("fmt,overs", [("T20", 20), ("ListA", 50)])
def test_first_innings_starts_near_even_and_follows_the_position(fmt, overs):
    m = _limited(fmt, overs)
    assert 40 <= _at(m, 1) <= 60
    ahead = _at(m, 1, score=overs * 6, wickets=1, over=overs // 2)
    behind = _at(m, 1, score=overs * 3, wickets=5, over=overs // 2)
    assert ahead > 60 > 40 > behind


def test_innings_break_hands_over_without_a_jump():
    m = _limited("ListA", 50)
    batting_first = _at(m, 1, score=250, wickets=7, over=50)
    chasing = _at(m, 2, target=251)
    assert batting_first + chasing == pytest.approx(100, abs=0.2)


def test_rain_revised_chase_uses_the_revised_overs():
    m = _limited("ListA", 20)
    assert 40 <= _at(m, 2, target=121) <= 60


def test_terminal_and_unsupported_states():
    m = _limited()
    assert _at(m, 2, score=150, over=15, target=150) == 100.0
    assert _at(m, 2, score=100, wickets=10, over=15, target=150) == 0.0
    assert _at(m, 3, target=150) is None
    assert _at(m, 4, target=10) is None
    m.is_fc = True
    assert _at(m, 1) is None
