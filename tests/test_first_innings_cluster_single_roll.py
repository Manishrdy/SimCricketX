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
        "match_id": f"cluster_single_roll_{match_format}_{bowling_count}_{simulation_mode}",
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


def test_first_innings_cluster_check_rolled_once_per_ball(monkeypatch):
    """Regression for A5: on a first-innings ball where both the risk-based
    cluster check and the "first innings collapse psychology" block are
    reached, should_trigger_wicket_cluster() must be evaluated exactly once
    (a single RNG draw), not once per block — otherwise the effective trigger
    probability roughly doubles and a lucky double-hit stacks 1.3x * 1.25x."""
    match = match_module.Match(_build_match_data())
    match.current_bowler = match.bowling_team[0]
    match.bowler_selected_for_over = -1
    match.current_over = 15  # T20 pre-death over -> risk_factor > 1.1
    match.current_ball = 0
    match.wickets = 5        # wickets_in_hand == 5 -> risk_factor += 0.08
    match.recent_wickets_count = 2
    match.score = 100

    assert match.innings == 1

    call_count = {"n": 0}

    def counting_stub(match_state, recent_wickets=0):
        call_count["n"] += 1
        return False

    monkeypatch.setattr(match.pressure_engine, "should_trigger_wicket_cluster", counting_stub)

    def dot_outcome(**_kwargs):
        return {
            "runs": 0,
            "batter_out": False,
            "is_extra": False,
            "description": "Defended.",
        }

    monkeypatch.setattr(match_module, "calculate_outcome", dot_outcome)

    match.next_ball()

    assert call_count["n"] == 1
