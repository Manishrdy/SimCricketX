import random
import statistics

import pytest

import engine.match as match_module
from engine.format_config import get_format
from engine.pressure_engine import PressureEngine


SEEDS = [4101, 4102, 4103, 4104, 4105, 4106, 4107, 4108]


def _build_team_players(prefix: str):
    players = []
    for i in range(11):
        if i < 6:
            bat = 78 - i * 2
            bowl = 45 + i
            role = "Batsman"
            will_bowl = i >= 4
            bowling_type = "Medium-fast" if i >= 4 else "Medium"
        else:
            bat = 48 - (i - 6) * 2
            bowl = 74 - (i - 6) * 3
            role = "Bowler"
            will_bowl = True
            bowling_type = ["Fast", "Fast-medium", "Medium-fast", "Off spin", "Leg spin"][min(i - 6, 4)]

        players.append(
            {
                "name": f"{prefix}_P{i+1}",
                "role": role,
                "batting_rating": max(20, bat),
                "bowling_rating": max(20, bowl),
                "fielding_rating": 70,
                "batting_hand": "Right" if i % 3 else "Left",
                "bowling_type": bowling_type,
                "bowling_hand": "Right" if i % 2 else "Left",
                "will_bowl": will_bowl,
                "is_captain": i == 0,
            }
        )

    # Keep a realistic 5-bowler setup.
    bowling_options = [p for p in players if p["will_bowl"]]
    for p in players:
        p["will_bowl"] = False
    for p in bowling_options[:5]:
        p["will_bowl"] = True
    return players


def _build_match_data(pitch: str, seed: int):
    random.seed(seed)
    return {
        "match_id": f"lista_{pitch}_{seed}",
        "created_by": "pytest_lista",
        "team_home": "HOM_pytest",
        "team_away": "AWY_pytest",
        "stadium": "Pytest Ground",
        "pitch": pitch,
        "toss": "Heads",
        "toss_winner": "HOM",
        "toss_decision": "Bat",
        "simulation_mode": "auto",
        "match_format": "ListA",
        "playing_xi": {
            "home": _build_team_players("H"),
            "away": _build_team_players("A"),
        },
        "substitutes": {"home": [], "away": []},
        "is_day_night": False,
    }


def _overs_to_balls(overs):
    value = str(overs)
    if "." in value:
        whole, balls = value.split(".", 1)
        return int(whole) * 6 + int(balls)
    return int(value) * 6


def _simulate_first_innings(pitch: str, seed: int):
    match = match_module.Match(_build_match_data(pitch, seed))
    for _ in range(1000):
        response = match.next_ball()
        if response.get("innings_end") and response.get("innings_number") == 1:
            scorecard = response.get("scorecard_data", {})
            players = scorecard.get("players", [])
            return {
                "runs": match.first_innings_score,
                "wickets": scorecard.get("wickets", 0),
                "balls": _overs_to_balls(scorecard.get("overs", "0.0")),
                "boundaries": sum((p.get("fours") or 0) + (p.get("sixes") or 0) for p in players),
            }
    raise AssertionError("First innings did not complete in expected delivery budget")


@pytest.fixture(autouse=True)
def _mute_match_print(monkeypatch):
    monkeypatch.setattr(match_module, "print", lambda *args, **kwargs: None)


@pytest.mark.parametrize(
    "pitch,run_low,run_high,min_avg_balls",
    [
        ("Dead", 320, 390, 285),
        ("Flat", 295, 355, 270),
        ("Hard", 265, 315, 260),
        ("Green", 180, 255, 220),
        ("Dry", 180, 255, 230),
    ],
)
def test_lista_pitch_scoring_bands_and_innings_depth(pitch, run_low, run_high, min_avg_balls):
    innings = [_simulate_first_innings(pitch, seed) for seed in SEEDS]
    avg_runs = statistics.mean(i["runs"] for i in innings)
    avg_balls = statistics.mean(i["balls"] for i in innings)

    assert run_low <= avg_runs <= run_high, (
        f"{pitch} ListA average runs out of band: got {avg_runs:.1f}, "
        f"expected [{run_low}, {run_high}]"
    )
    assert avg_balls >= min_avg_balls, (
        f"{pitch} innings ending too early on average: {avg_balls:.1f} balls"
    )


def test_lista_pitch_boundary_profile_relative_order():
    grouped = {
        pitch: [_simulate_first_innings(pitch, seed) for seed in SEEDS]
        for pitch in ("Dead", "Flat", "Hard", "Green", "Dry")
    }
    avg_boundaries = {
        pitch: statistics.mean(i["boundaries"] for i in innings)
        for pitch, innings in grouped.items()
    }

    assert avg_boundaries["Dead"] > avg_boundaries["Hard"]
    assert avg_boundaries["Flat"] > avg_boundaries["Hard"]
    assert avg_boundaries["Green"] < avg_boundaries["Hard"]
    assert avg_boundaries["Dry"] < avg_boundaries["Hard"]


def test_lista_manual_bowler_decision_uses_ten_over_quota():
    match_data = _build_match_data("Hard", seed=9999)
    match_data["simulation_mode"] = "manual"
    match = match_module.Match(match_data)

    bowling_names = [p["name"] for p in match.bowling_team if p.get("will_bowl")]
    assert len(bowling_names) >= 3

    match.bowler_history[bowling_names[0]] = 7
    match.bowler_history[bowling_names[1]] = 10
    match.bowler_history[bowling_names[2]] = 2

    decision = match._create_next_bowler_decision()
    options_by_name = {opt["name"]: opt for opt in decision["options"]}

    assert decision["type"] == "next_bowler"
    assert options_by_name[bowling_names[0]]["overs_remaining"] == 3
    assert options_by_name[bowling_names[2]]["overs_remaining"] == 8
    assert bowling_names[1] not in options_by_name


def test_lista_chasing_advantage_not_batting_favored():
    engine = PressureEngine(format_config=get_format("ListA"))
    effects = engine.get_chasing_advantage(
        {
            "innings": 2,
            "current_over": 34,
            "wickets": 3,
        }
    )

    assert effects is not None
    assert effects["boundary_boost"] <= 1.0
    assert effects["wicket_reduction"] >= 1.0
