"""Regressions for the Nov 2026 engine review.

Each test pins one defect found by simulating matches and checking scorecard
invariants:

* a run-out that completes the winning run used to leave the chase running;
* an innings ending on the last ball of an over lost that over for the bowler;
* bowler dot balls were never tallied;
* only the Hundred's Super Five honoured the free hit after a no-ball;
* a five-bowler T20 attack could be steered into a fifth over for one bowler.
"""
import logging
import random

import pytest

import engine.match as match_module
from tests.test_free_hit_state_ordering import _build_match_data


@pytest.fixture(autouse=True)
def _quiet_match(monkeypatch):
    monkeypatch.setattr(match_module, "print", lambda *args, **kwargs: None)
    monkeypatch.setattr(match_module.Match, "_create_match_archive", lambda self: True)
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def _outcome(runs=0, wicket=None, extra=None):
    return {
        "runs": runs,
        "batter_out": bool(wicket),
        "wicket_type": wicket,
        "is_extra": bool(extra),
        "extra_type": extra,
        "type": "wicket" if wicket else "extra" if extra else "run",
        "description": "Out!" if wicket else "Delivery",
    }


def _play_scoreless_first_innings(monkeypatch, match):
    monkeypatch.setattr(match_module, "calculate_outcome", lambda **kw: _outcome())
    for _ in range(200):
        if match.next_ball().get("innings_end"):
            break
    assert match.innings == 2 and match.target == 1


def test_run_out_on_the_winning_run_ends_the_chase(monkeypatch):
    match = match_module.Match(_build_match_data())
    _play_scoreless_first_innings(monkeypatch, match)

    # One to win; the engine credits the completed run on every run-out.
    monkeypatch.setattr(match_module, "calculate_outcome",
                        lambda **kw: _outcome(runs=1, wicket="Run Out"))
    result = match.next_ball()

    assert result["match_over"] is True
    assert match.score == 1 and match.wickets == 0
    assert "won by 10 wicket(s)" in match.result
    assert not any(s["wicket_type"] for s in match.second_innings_batting_stats.values())


def test_innings_ending_on_last_ball_of_over_counts_the_over_and_dots(monkeypatch):
    match = match_module.Match(_build_match_data())
    balls = iter([None, None] + ["Bowled"] * 10)   # 0 0 W W W W | W W W W W W
    monkeypatch.setattr(match_module, "calculate_outcome",
                        lambda **kw: _outcome(wicket=next(balls)))

    for _ in range(20):
        if match.next_ball().get("innings_end"):
            break

    figures = sorted(match.first_innings_bowling_stats.values(),
                     key=lambda s: s["wickets"])
    figures = [s for s in figures if s["balls_bowled"]]
    assert [s["wickets"] for s in figures] == [4, 6]
    for stats in figures:
        assert stats["balls_bowled"] == 6
        assert stats["overs"] == 1          # the closing over used to stay 0
        assert stats["maidens"] == 1        # ... and its wicket maiden was lost
        assert stats["dots"] == 6
    assert match._innings1_overs_bowled == 2


def test_bowler_dots_exclude_wides_no_balls_and_scoring_shots(monkeypatch):
    match = match_module.Match(_build_match_data())
    script = iter([
        _outcome(),                      # dot
        _outcome(runs=1, extra="Wide"),  # not a legal ball
        _outcome(runs=4),                # scoring shot
        _outcome(runs=1, extra="Byes"),  # legal, nothing against the bowler
        _outcome(runs=1, wicket="Run Out"),  # one run charged
        _outcome(wicket="Bowled"),       # dot
    ])
    monkeypatch.setattr(match_module, "calculate_outcome", lambda **kw: dict(next(script)))

    for _ in range(6):
        match.next_ball()

    stats = match.bowler_stats[match.current_bowler["name"]]
    assert stats["balls_bowled"] == 5
    assert stats["dots"] == 3


def test_t20_super_over_honours_free_hit_after_no_ball(monkeypatch):
    match = match_module.Match(_build_match_data())
    _play_scoreless_first_innings(monkeypatch, match)
    tied = {}
    for _ in range(200):
        tied = match.next_ball()
        if tied.get("super_over_required"):
            break
    assert tied.get("super_over_required")
    assert not match.start_super_over("home").get("error")

    def deliver(**kwargs):
        monkeypatch.setattr(match_module, "calculate_super_over_outcome",
                            lambda **kw: _outcome(**kwargs))
        return match.next_super_over_ball()

    assert deliver(runs=1, extra="No Ball")["free_hit"] is False
    assert match.serialize_super_over_snapshot()["super_over"]["free_hit_active"] is True

    protected = deliver(wicket="Bowled")
    assert protected["free_hit"] is True
    assert protected["wicket"] is False and protected["wickets"] == 0

    # The free hit is spent by that legal ball; the next dismissal stands.
    assert deliver(wicket="Bowled")["wickets"] == 1


def _five_bowler_xi(prefix):
    styles = ["Fast", "Fast-medium", "Medium-fast", "Off spin", "Leg spin", "Medium"]
    return [{
        "name": f"{prefix}_P{i + 1}",
        "role": "Bowler" if i >= 6 else "Batsman",
        "batting_rating": 80 - 4 * i,
        "bowling_rating": 60 + 2 * i,
        "fielding_rating": 70,
        "batting_hand": "Right",
        "bowling_type": styles[i % 6],
        "bowling_hand": "Right",
        "will_bowl": i >= 6,
        "is_captain": i == 0,
    } for i in range(11)]


@pytest.mark.parametrize("seed", [8, 9])
def test_five_bowler_t20_attack_never_bowls_a_fifth_over(seed):
    random.seed(seed)
    data = _build_match_data()
    data["match_id"] = f"five_bowler_quota_{seed}"
    data["pitch"] = ["Green", "Dry", "Hard", "Flat", "Dead"][seed % 5]
    data["playing_xi"] = {"home": _five_bowler_xi("H"), "away": _five_bowler_xi("A")}
    match = match_module.Match(data)

    for _ in range(400):
        if match.next_ball().get("innings_end"):
            break

    overs = {name: stats["balls_bowled"] / 6
             for name, stats in match.first_innings_bowling_stats.items()}
    assert max(overs.values()) <= 4, overs


def test_last_wicket_run_out_on_the_winning_run_is_a_one_wicket_win(monkeypatch):
    match = match_module.Match(_build_match_data())
    _play_scoreless_first_innings(monkeypatch, match)
    script = iter([_outcome(wicket="Bowled")] * 9 + [_outcome(runs=1, wicket="Run Out")])
    monkeypatch.setattr(match_module, "calculate_outcome", lambda **kw: dict(next(script)))

    result = {}
    for _ in range(10):
        result = match.next_ball()

    assert result["match_over"] is True and match.wickets == 9
    assert "won by 1 wicket(s)" in match.result
    assert match.margin_value == 1


def test_extras_before_first_legal_ball_stay_in_the_over_total(monkeypatch):
    match = match_module.Match(_build_match_data())
    script = iter([_outcome(runs=1, extra="Wide"), _outcome(runs=1, extra="Wide")]
                  + [_outcome()] * 6)
    monkeypatch.setattr(match_module, "calculate_outcome", lambda **kw: dict(next(script)))

    match.next_ball()
    match.next_ball()
    assert match.current_ball == 0 and match.current_over_runs == 2
    bowler = match.current_bowler["name"]
    for _ in range(6):
        match.next_ball()

    assert match.bowler_manager.prev_over_runs(bowler) == 2
    assert match.current_over_runs == 0     # fresh tally for the next over


@pytest.mark.parametrize("end", ["striker", "non_striker"])
def test_run_out_resets_form_of_the_batter_who_is_out(monkeypatch, end):
    match = match_module.Match(_build_match_data())
    striker = match.current_striker["name"]
    non_striker = match.current_non_striker["name"]
    monkeypatch.setattr(match_module, "calculate_outcome", lambda **kw: _outcome())
    match.next_ball()                       # seat a bowler and stats rows
    match.batsman_stats[striker]["form"] = 1.10
    match.batsman_stats[non_striker]["form"] = 1.07

    monkeypatch.setattr(
        match_module, "calculate_outcome",
        lambda **kw: dict(_outcome(runs=1, wicket="Run Out"), dismissed_end=end))
    match.next_ball()

    out, survivor = (striker, non_striker) if end == "striker" else (non_striker, striker)
    assert match.batsman_stats[out]["wicket_type"] == "Run Out"
    assert match.batsman_stats[out]["form"] == 1.0
    assert match.batsman_stats[survivor]["form"] != 1.0
    if end == "non_striker":
        # The striker is credited with the single he completed.
        assert match.batsman_stats[striker]["form"] > 1.10
