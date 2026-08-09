import logging

import pytest

import engine.match as match_module


def _build_team(prefix: str, bowling_count: int = 5):
    bowling_types = ["Fast", "Fast-medium", "Medium-fast", "Off spin", "Leg spin"]
    players = []
    for i in range(11):
        will_bowl = i < bowling_count
        players.append({
            "name": f"{prefix}_P{i + 1}",
            "role": "Bowler" if will_bowl else "Batsman",
            "batting_rating": 72 - i,
            "bowling_rating": 82 - i,
            "fielding_rating": 70,
            "batting_hand": "Right",
            "bowling_type": bowling_types[i % len(bowling_types)],
            "bowling_hand": "Right",
            "will_bowl": will_bowl,
            "is_captain": i == 0,
        })
    return players


def _build_match_data(match_format="T20", bowling_count=5, simulation_mode="auto"):
    return {
        "match_id": f"collapse_decay_{match_format}_{bowling_count}_{simulation_mode}",
        "created_by": "pytest",
        "team_home": "HOM_pytest",
        "team_away": "AWY_pytest",
        "stadium": "Pytest Ground",
        "pitch": "Hard",
        "toss": "Heads",
        "toss_winner": "HOM",
        "toss_decision": "Bat",
        "simulation_mode": simulation_mode,
        "match_format": match_format,
        "playing_xi": {
            "home": _build_team("H", bowling_count),
            "away": _build_team("A", bowling_count),
        },
        "substitutes": {"home": [], "away": []},
        "is_day_night": False,
    }


@pytest.fixture(autouse=True)
def _quiet_match(monkeypatch):
    monkeypatch.setattr(match_module, "print", lambda *args, **kwargs: None)
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def test_recent_wickets_count_decays_without_further_wickets(monkeypatch):
    """recent_wickets_count must fall back to 0 once the wicket that put it
    there is more than 12 balls in the past, even though no further wicket
    ever falls to trigger a recompute."""
    match = match_module.Match(_build_match_data())
    match.current_bowler = match.bowling_team[0]
    match.bowler_selected_for_over = 0
    match.current_over = 0
    match.current_ball = 0

    call_count = {"n": 0}

    def outcome_fn(**_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {
                "runs": 0,
                "batter_out": True,
                "wicket_type": "Bowled",
                "is_extra": False,
                "description": "Bowled him!",
            }
        return {
            "runs": 0,
            "batter_out": False,
            "is_extra": False,
            "description": "Defended.",
        }

    monkeypatch.setattr(match_module, "calculate_outcome", outcome_fn)

    match.next_ball()  # ball 0.1: the only wicket in this test
    assert match.wickets == 1
    assert match.recent_wickets_count == 1

    # 12 more deliveries (through call #13) are still within the 12-ball
    # window, so the stale wicket must still be counted.
    for _ in range(12):
        match.next_ball()
    assert match.recent_wickets_count == 1

    # The 14th call finally pushes the wicket outside the window. Nothing
    # about this ball is special (still a dot ball, no wicket) — the count
    # must decay on its own instead of staying frozen at 1 forever.
    match.next_ball()
    assert match.recent_wickets_count == 0
    assert match.recent_wickets_tracker == []


def test_reset_innings_state_clears_collapse_tracker():
    """First-innings wicket-cluster tracking must not leak into the second
    innings. Ball numbering (over*6+ball) restarts at 0 for the new innings,
    so stale entries from a 20-over first innings would otherwise never be
    filtered out (the ball-number difference goes permanently negative)."""
    match = match_module.Match(_build_match_data())
    match.recent_wickets_tracker = [110, 114, 118]
    match.recent_wickets_count = 3

    match._reset_innings_state()

    assert match.recent_wickets_count == 0
    assert match.recent_wickets_tracker == []
