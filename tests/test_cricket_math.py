"""
Unit tests for the shared balls<->overs helpers (engine/cricket_math.py).

These consolidate ~5 previously-duplicated inline implementations, one of
which (MatchScorecard.overs persistence) fed a truncated whole-overs counter
instead of the exact balls_bowled count. Regression coverage: every call
site must agree on the same ball count.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cricket_math import balls_to_overs_str, balls_to_overs_float


def test_balls_to_overs_str_basic_cases():
    assert balls_to_overs_str(0) == "0.0"
    assert balls_to_overs_str(6) == "1.0"
    assert balls_to_overs_str(23) == "3.5"
    assert balls_to_overs_str(119) == "19.5"
    assert balls_to_overs_str(120) == "20.0"


def test_balls_to_overs_str_handles_none_and_negative():
    assert balls_to_overs_str(None) == "0.0"
    assert balls_to_overs_str(-5) == "0.0"


def test_balls_to_overs_float_basic_cases():
    assert balls_to_overs_float(0) == 0.0
    assert balls_to_overs_float(6) == 1.0
    assert balls_to_overs_float(23) == 3.5
    assert balls_to_overs_float(119) == 19.5


def test_str_and_float_agree_across_range():
    for balls in (0, 1, 5, 6, 7, 23, 24, 100, 119, 120):
        s = balls_to_overs_str(balls)
        f = balls_to_overs_float(balls)
        whole, frac = s.split(".")
        assert f == float(f"{whole}.{frac}")
