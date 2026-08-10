"""
Shared balls <-> overs conversion helpers.

balls_bowled is the exact, authoritative count. overs (cricket notation,
e.g. "4.3" for 4 overs + 3 balls) should always be derived from it rather
than tracked independently — several call sites used to each reimplement
this formula slightly differently, and one of them (MatchScorecard.overs)
was fed a truncated whole-overs counter instead, corrupting the value.
"""


def balls_to_overs_str(balls: int) -> str:
    """Return cricket-notation overs as a string, e.g. 23 balls -> "3.5"."""
    balls = max(0, int(balls or 0))
    return f"{balls // 6}.{balls % 6}"


def balls_to_overs_float(balls: int) -> float:
    """Return cricket-notation overs as a float, e.g. 23 balls -> 3.5.

    Note this is NOT true decimal overs (23/6 = 3.8333); it's the
    cricket convention where the fractional part is balls-in-the-over
    (0-5), matching how overs are always displayed/scored.
    """
    balls = max(0, int(balls or 0))
    return (balls // 6) + (balls % 6) / 10.0
