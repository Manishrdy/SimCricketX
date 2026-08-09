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
        "match_id": f"free_hit_state_{match_format}_{bowling_count}_{simulation_mode}",
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


def test_free_hit_dismissal_does_not_poison_match_state(monkeypatch):
    """A wicket that is invalidated by a free hit must not leak into any
    downstream system (pressure engine, GSME ball history, collapse/wicket
    tracker, batter form) as if the dismissal actually happened."""
    match = match_module.Match(_build_match_data())
    match.current_bowler = match.bowling_team[0]
    match.bowler_selected_for_over = 0
    match.current_over = 0
    match.current_ball = 0
    match.free_hit_active = True

    striker_name = match.current_striker["name"]
    match.batsman_stats[striker_name]["form"] = 1.10

    def bowled_outcome(**_kwargs):
        return {
            "runs": 0,
            "batter_out": True,
            "wicket_type": "Bowled",
            "is_extra": False,
            "description": "Bowled him!",
        }

    monkeypatch.setattr(match_module, "calculate_outcome", bowled_outcome)

    result = match.next_ball()

    # Scorecard: the batter survives, no wicket actually falls.
    assert match.wickets == 0
    assert result["wickets"] == 0

    # Batter form must take the normal "Dot" delta (1.10 - 0.02), not get
    # wiped to 1.0 as though the batter were dismissed.
    assert match.batsman_stats[striker_name]["form"] == pytest.approx(1.08)

    # Pressure engine's momentum window must not record a wicket.
    assert match.pressure_engine.recent_events[-1]["wicket"] is False

    # GSME's rolling ball history must not record a Wicket event.
    assert match.ball_history[-1]["is_wicket"] is False
    assert match.ball_history[-1]["label"] != "Wicket"

    # Collapse/recent-wickets tracker must not count this ball.
    assert getattr(match, "recent_wickets_count", 0) == 0
    assert getattr(match, "recent_wickets_tracker", []) == []


def test_free_hit_run_out_still_counts(monkeypatch):
    """Run outs are the one dismissal type that still stands on a free hit —
    the reordering must not accidentally suppress them."""
    match = match_module.Match(_build_match_data())
    match.current_bowler = match.bowling_team[0]
    match.bowler_selected_for_over = 0
    match.current_over = 0
    match.current_ball = 0
    match.free_hit_active = True

    def run_out_outcome(**_kwargs):
        return {
            "runs": 1,
            "batter_out": True,
            "wicket_type": "Run Out",
            "is_extra": False,
            "description": "Run out!",
            "dismissed_batter": "striker",
        }

    monkeypatch.setattr(match_module, "calculate_outcome", run_out_outcome)

    match.next_ball()

    assert match.wickets == 1
    assert match.pressure_engine.recent_events[-1]["wicket"] is True
    assert match.ball_history[-1]["is_wicket"] is True
