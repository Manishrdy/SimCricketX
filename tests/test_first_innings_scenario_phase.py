import logging

import pytest

import engine.match as match_module
from engine.scenario_engine import HistoricalScenarioEngine


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
        "match_id": f"scenario_phase_{match_format}_{bowling_count}_{simulation_mode}",
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


def test_first_innings_historical_beat_reports_convergence_not_inactive(monkeypatch):
    """Regression for A6: next_ball() used to build the GSME game-state vector
    twice, and the second (redundant) build ignored steers_first_innings,
    always forcing scenario_phase to "inactive" for innings 1 — even though
    HistoricalScenarioEngine.get_override_outcome() always returns None, which
    routes every ball through the branch that did the second build. The
    game_state actually passed to calculate_outcome must reflect the real
    phase ("convergence" here, since the checkpoint is 6 balls away)."""
    match = match_module.Match(_build_match_data())
    match.current_bowler = match.bowling_team[0]
    match.bowler_selected_for_over = 0
    match.current_over = 0
    match.current_ball = 0
    assert match.innings == 1

    pack = {"id": "test_pack", "beats": {"1": [{"at_over": 1}]}}
    match.scenario_engine = HistoricalScenarioEngine(pack, match)
    assert match.scenario_engine.steers_first_innings is True
    assert match.scenario_engine.get_phase() == "convergence"

    captured = {}

    def capturing_outcome(**kwargs):
        captured.update(kwargs)
        return {
            "runs": 0,
            "batter_out": False,
            "is_extra": False,
            "description": "Defended.",
        }

    monkeypatch.setattr(match_module, "calculate_outcome", capturing_outcome)

    match.next_ball()

    assert "game_state" in captured
    assert captured["game_state"]["scenario_phase"] == "convergence"
