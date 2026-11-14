"""Match Center ball_data carries the batter/bowler credits the scorecard uses.

Summing a six-ball innings' per-ball figures must reproduce the engine's live
batting and bowling stats, so the dashboard cards agree with the score banner.
"""
from collections import defaultdict

import pytest

import engine.match as match_module
from tests.test_scoring_calibration import _match_data


@pytest.fixture(autouse=True)
def _quiet_and_no_archive(monkeypatch):
    monkeypatch.setattr(match_module.Match, "_create_match_archive", lambda self: None)
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)


def _check(fmt, seed, overs):
    match = match_module.Match(_match_data(fmt, "Flat", seed))
    batting, bowling, innings = defaultdict(lambda: defaultdict(int)), defaultdict(lambda: defaultdict(int)), None
    checked = 0
    for _ in range(5000):
        resp = match.next_ball()
        bd = resp.get("ball_data")
        if bd:
            if bd["innings"] != innings:
                innings = bd["innings"]
                batting.clear(); bowling.clear()
            assert bd["innings_over_limit"] == overs
            bat, bowl = batting[bd["striker"]], bowling[bd["bowler"]]
            for key, field in (("runs", "batting_runs"), ("balls", "batter_balls"),
                               ("fours", "batter_fours"), ("sixes", "batter_sixes")):
                bat[key] += bd[field]
            bowl["runs"] += bd["bowler_runs"]
            bowl["balls_bowled"] += bd["bowler_balls"]
            bowl["wickets"] += bd["bowler_wicket"]
            assert not (bd["bowler_wicket"] and bd["wicket_type"] == "Run Out")
            if not (resp.get("innings_end") or resp.get("match_over")):
                for name, totals in batting.items():
                    live = match.batsman_stats[name]
                    assert {k: live[k] for k in totals} == dict(totals), (name, bd)
                for name, totals in bowling.items():
                    live = match.bowler_stats[name]
                    assert {k: live[k] for k in totals} == dict(totals), (name, bd)
                checked += 1
        if resp.get("match_over"):
            break
    assert checked > overs * 6


@pytest.mark.parametrize("fmt,overs", [("T20", 20), ("ListA", 50)])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_ball_figures_sum_to_live_stats(fmt, overs, seed):
    _check(fmt, seed, overs)
