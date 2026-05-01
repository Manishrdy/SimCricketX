import logging

import pytest

import engine.match as match_module
from engine.bowler_manager import BowlerManager
from engine.format_config import get_format


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
        "match_id": f"consecutive_guard_{match_format}_{bowling_count}_{simulation_mode}",
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


def test_legal_extra_on_last_ball_still_selects_new_bowler(monkeypatch):
    match = match_module.Match(_build_match_data())
    opening_bowler = match.bowling_team[0]["name"]
    calls = []

    match.current_bowler = match.bowling_team[0]
    match.bowler_selected_for_over = 0
    match.current_over = 0
    match.current_ball = 5

    def byes_on_last_ball(**_kwargs):
        return {
            "runs": 1,
            "batter_out": False,
            "is_extra": True,
            "extra_type": "Byes",
            "description": "Byes.",
        }

    def dot_next_over(**kwargs):
        calls.append(kwargs["bowler"]["name"])
        return {
            "runs": 0,
            "batter_out": False,
            "is_extra": False,
            "description": "Defended.",
        }

    monkeypatch.setattr(match_module, "calculate_outcome", byes_on_last_ball)
    first = match.next_ball()
    assert first["over"] == 1
    assert first["ball"] == 0
    assert match.prev_delivery_was_extra is True

    monkeypatch.setattr(match_module, "calculate_outcome", dot_next_over)
    second = match.next_ball()

    assert second.get("error") is None
    assert calls
    assert calls[0] != opening_bowler
    assert second["bowler"] != opening_bowler


def test_manual_bowler_options_exclude_previous_even_when_quota_exhausted():
    match = match_module.Match(_build_match_data(bowling_count=2, simulation_mode="manual"))
    previous = match.bowling_team[0]
    alternative = match.bowling_team[1]

    match.current_bowler = previous
    match.bowler_history[alternative["name"]] = match.fmt.max_bowler_overs

    decision = match._create_next_bowler_decision()
    option_names = {opt["name"] for opt in decision["options"]}

    assert previous["name"] not in option_names
    assert alternative["name"] in option_names


def test_submit_pending_bowler_decision_rejects_previous_bowler():
    match = match_module.Match(_build_match_data(simulation_mode="manual"))
    previous = match.bowling_team[0]
    match.current_bowler = previous
    match.pending_decision = {
        "type": "next_bowler",
        "options": [{"index": 0, "name": previous["name"]}],
    }

    result, status = match.submit_pending_decision(0)

    assert status == 400
    assert "consecutive" in result["error"]


def test_bowler_manager_violates_quota_before_consecutive():
    bowlers = _build_team("A", bowling_count=2)[:2]
    manager = BowlerManager(bowlers, get_format("ListA"))
    previous, alternative = bowlers

    manager.record_over_completion(previous["name"], 0)
    manager._quota[alternative["name"]] = get_format("ListA").max_bowler_overs

    eligible = manager.get_eligible_bowlers(current_over=49, overs_remaining_in_innings=1)

    assert [b["name"] for b in eligible] == [alternative["name"]]


def test_auto_mode_aborts_when_only_previous_bowler_exists(monkeypatch):
    match = match_module.Match(_build_match_data(bowling_count=1))
    previous = match.bowling_team[0]

    match.current_bowler = previous
    match.bowler_manager.record_over_completion(previous["name"], 0)
    match.current_over = 1
    match.current_ball = 0
    match.bowler_selected_for_over = -1

    response = match.next_ball()

    assert response["match_over"] is True
    assert "no non-consecutive bowler" in response["result"].lower()


def test_t20_death_plan_starts_at_over_17_and_solves_four_over_finish():
    match = match_module.Match(_build_match_data(bowling_count=5))
    bowler_a, bowler_b, bowler_c, bowler_d, bowler_e = match.bowling_team[:5]

    match.current_over = match.fmt.death_phase.start
    match.current_ball = 0
    match.current_bowler = bowler_a
    match.bowler_history[bowler_a["name"]] = 2
    match.bowler_history[bowler_b["name"]] = 3
    match.bowler_history[bowler_c["name"]] = 3
    match.bowler_history[bowler_d["name"]] = 4
    match.bowler_history[bowler_e["name"]] = 4

    selected = match._pick_death_overs_bowler()

    assert selected["name"] != bowler_a["name"]
    assert len(match.death_overs_plan) == 4
    assert match.death_overs_plan_start == match.fmt.death_phase.start
    assert match.death_overs_plan.count(bowler_a["name"]) == 2
    assert match.death_overs_plan.count(bowler_b["name"]) == 1
    assert match.death_overs_plan.count(bowler_c["name"]) == 1
    assert all(
        left != right
        for left, right in zip(
            [bowler_a["name"]] + match.death_overs_plan,
            match.death_overs_plan,
        )
    )


def test_death_plan_uses_exhausted_non_consecutive_fallback_before_throwing():
    match = match_module.Match(_build_match_data(bowling_count=5))
    previous = match.bowling_team[0]

    match.current_over = match.fmt.death_phase.start
    match.current_ball = 0
    match.current_bowler = previous
    for bowler in match.bowling_team[:5]:
        match.bowler_history[bowler["name"]] = match.fmt.max_bowler_overs
    match.bowler_history[previous["name"]] = match.fmt.max_bowler_overs - 1

    selected = match._pick_death_overs_bowler()

    assert selected["name"] != previous["name"]
    assert len(match.death_overs_plan) == 4
    assert all(
        left != right
        for left, right in zip(
            [previous["name"]] + match.death_overs_plan,
            match.death_overs_plan,
        )
    )
