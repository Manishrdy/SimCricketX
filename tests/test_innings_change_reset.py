"""Innings-change state reset.

`current_bowler` used to survive the innings change. Since the sides swap, it
then pointed at a bowler from the team that was now *batting*, and next_ball()
calls `_ensure_current_bowler_stats_entry()` before picking the new over's
bowler — so the first ball of the second innings created a phantom, all-zero
`bowler_stats` row for an opposition player.

It was inert downstream (the bowling table filters `balls_bowled > 0`, and the
archiver's team-scoped `_lookup_player` rejects the name), but it produced a
"Scorecard skipped: player not found" warning every match and put an
opposition name in a live innings' bowling card.
"""
import os
import sys
import uuid

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.match as match_module


def _squad(names):
    return [{
        "name": n,
        "role": "Bowler" if i >= 6 else "Batsman",
        "batting_rating": 70, "bowling_rating": 70, "fielding_rating": 70,
        "batting_hand": "Right", "bowling_type": "Medium", "bowling_hand": "Right",
        "will_bowl": i >= 6, "is_captain": i == 0, "is_wicketkeeper": i == 4,
    } for i, n in enumerate(names)]


HOME = _squad([f"HOME_P{i+1}" for i in range(11)])
AWAY = _squad([f"AWAY_P{i+1}" for i in range(11)])


def _match(toss_winner="HOM", toss_decision="Bat"):
    import copy
    return match_module.Match({
        "match_id": str(uuid.uuid4()), "created_by": 1,
        "timestamp": "2026-08-08T12:00:00",
        "team_home": "HOM_1", "team_away": "AWY_1",
        "stadium": "Test Ground", "pitch": "Hard",
        "toss": "Heads", "toss_winner": toss_winner, "toss_decision": toss_decision,
        "match_format": "T20", "overs": 20, "simulation_mode": "auto",
        "rain_probability": 0.0,
        "playing_xi": {"home": copy.deepcopy(HOME), "away": copy.deepcopy(AWAY)},
        "substitutes": {"home": [], "away": []},
    })


def _play_to_innings_break(m, limit=400):
    balls = 0
    while m.innings == 1 and balls < limit:
        m.next_ball()
        balls += 1
    assert m.innings == 2, "match never reached the innings break"
    return balls


@pytest.mark.parametrize("winner,decision", [
    ("HOM", "Bat"), ("HOM", "Bowl"), ("AWY", "Bat"), ("AWY", "Bowl"),
])
def test_second_innings_bowling_card_has_no_opposition_players(app, winner, decision):
    m = _match(winner, decision)
    _play_to_innings_break(m)

    # The stale pointer is cleared at the break...
    assert m.current_bowler is None

    # ...so the first ball of the new innings cannot mint a phantom row.
    m.next_ball()

    bowling_side = {p["name"] for p in m.bowling_team}
    batting_side = {p["name"] for p in m.batting_team}
    leaked = set(m.bowler_stats) & batting_side
    assert not leaked, f"opposition players in the bowling card: {sorted(leaked)}"
    assert set(m.bowler_stats) <= bowling_side


def test_innings_change_still_selects_a_bowler(app):
    """Clearing current_bowler must not leave the new innings without one."""
    m = _match("HOM", "Bat")
    _play_to_innings_break(m)

    m.next_ball()
    assert m.current_bowler is not None
    assert m.current_bowler["name"] in {p["name"] for p in m.bowling_team}
    assert m.current_bowler.get("will_bowl") is True


def test_innings_change_resets_the_consecutive_over_guard(app):
    """The previous innings' last bowler must not constrain the new innings —
    BowlerManager._last_bowler is reset alongside current_bowler."""
    m = _match("HOM", "Bat")
    _play_to_innings_break(m)
    assert m.bowler_manager._last_bowler is None
