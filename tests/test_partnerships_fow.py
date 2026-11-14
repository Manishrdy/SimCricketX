"""Partnerships and fall of wickets, checked against the scorecard they sit on.

Every innings of a full simulated match must satisfy the bookkeeping that a
real scorecard does: the stands add up to the total, their boundaries add up
to the batters' boundaries, there is one broken stand per wicket in order, and
the fall-of-wickets scores climb to (at most) the total.
"""
import copy
import random
import uuid

import pytest

import engine.match as match_module
from tests.test_scoring_calibration import _match_data, _squad
from utils.partnership_format import fall_of_wickets, fow_text, ordinal


@pytest.fixture(autouse=True)
def _quiet_and_no_archive(monkeypatch):
    monkeypatch.setattr(match_module.Match, "_create_match_archive", lambda self: None)
    monkeypatch.setattr(match_module.Match, "_fc_create_match_archive", lambda self: None)
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)


def _fc_data(pitch, seed):
    random.seed(seed)
    return {
        "match_id": str(uuid.uuid4()), "created_by": "test",
        "timestamp": "2026-08-09T12:00:00",
        "team_home": "HOM_t", "team_away": "AWY_t", "stadium": "Test Ground",
        "pitch": pitch, "toss": "Heads", "toss_winner": "HOM", "toss_decision": "Bat",
        "match_format": "FC", "days": 5, "simulation_mode": "auto",
        "is_day_night": False,
        "playing_xi": {"home": _squad("H"), "away": _squad("A")},
        "substitutes": {"home": [], "away": []},
    }


def _play(data, limit=60000):
    """Play to the end; return (match, [innings scorecards], final response)."""
    match = match_module.Match(data)
    cards = {}
    for _ in range(limit):
        resp = match.next_ball()
        assert "error" not in resp, resp
        sc = resp.get("scorecard_data")
        if sc and (resp.get("innings_end") or resp.get("match_over")):
            cards[sc["innings_number"]] = copy.deepcopy(sc)
        if resp.get("match_over"):
            first = getattr(match, "first_innings_scorecard", None)
            if first and 1 not in cards:
                cards[1] = copy.deepcopy(first)
            return match, [cards[k] for k in sorted(cards)], resp
    raise AssertionError("match did not finish")


def _num(v):
    return v if isinstance(v, int) else 0


def _check_innings(sc):
    stands = sc["partnerships"]
    label = f"{sc.get('match_format')} inns {sc['innings_number']}"
    if sc["total_score"] == 0 and sc["wickets"] == 0:
        return
    assert stands, label
    assert sum(p["runs"] for p in stands) == sc["total_score"], label
    assert sum(p["balls"] for p in stands) == sc["legal_balls"], label
    players = sc["players"]
    assert sum(p["fours"] for p in stands) == sum(_num(p["fours"]) for p in players), label
    assert sum(p["sixes"] for p in stands) == sum(_num(p["sixes"]) for p in players), label
    assert (sum(p["batsman1_contribution"] + p["batsman2_contribution"] for p in stands)
            == sum(_num(p["runs"]) for p in players)), label

    assert [p["wicket_number"] for p in stands] == list(range(1, len(stands) + 1)), label
    broken = [p for p in stands if not p["is_unbroken"]]
    unbroken = [p for p in stands if p["is_unbroken"]]
    assert len(broken) == sc["wickets"], label
    assert len(unbroken) <= 1 and (not unbroken or stands[-1] is unbroken[0]), label
    if unbroken:
        assert unbroken[0]["wicket_number"] == sc["wickets"] + 1, label

    fow = sc["fall_of_wickets"]
    assert fow == fall_of_wickets(stands)
    assert len(fow) == sc["wickets"], label
    scores = [e["score"] for e in fow]
    assert scores == sorted(scores) and (not scores or scores[-1] <= sc["total_score"]), label

    out = {p["name"]: p for p in players if p.get("wicket_type")}
    for p, e in zip(broken, fow):
        assert p["dismissed_slot"] in (1, 2), label
        assert e["batter"] in out, (label, e)
        assert e["over"], label


LIMITED = [("T20", s) for s in range(4)] + [("ListA", 1), ("T10", 2), ("T10", 3),
                                           ("Hundred", 4), ("Hundred", 5)]


@pytest.mark.parametrize("fmt,seed", LIMITED)
def test_limited_overs_partnerships_and_fow_balance(fmt, seed):
    match, cards, resp = _play(_match_data(fmt, "Hard", seed))
    assert len(cards) == 2
    for sc in cards:
        _check_innings(sc)
    # Archive lists hold the same stands as the scorecards.
    assert match.first_innings_partnerships == cards[0]["partnerships"]
    assert match.second_innings_partnerships == cards[1]["partnerships"]


def test_chase_won_keeps_the_winning_stand():
    for seed in range(30):
        match, cards, resp = _play(_match_data("T20", "Flat", seed))
        if (match.margin_type == "wickets") and cards[1]["wickets"] < 10:
            last = cards[1]["partnerships"][-1]
            assert last["is_unbroken"]
            assert last["wicket_number"] == cards[1]["wickets"] + 1
            return
    pytest.skip("no chase won in 30 seeds")


def test_final_commentary_lists_fow_and_partnerships():
    match, cards, resp = _play(_match_data("T20", "Hard", 0))
    commentary = resp["commentary"]
    assert "Partnerships" in commentary
    if cards[0]["fall_of_wickets"]:
        assert "Fall of wickets:" in commentary
        assert fow_text(cards[0]["fall_of_wickets"][0]) in commentary


def test_run_out_names_the_batter_actually_out(monkeypatch):
    seen = 0
    for seed in range(12):
        match, cards, _ = _play(_match_data("T20", "Hard", seed))
        for sc in cards:
            players = {p["name"]: p for p in sc["players"]}
            for e in sc["fall_of_wickets"]:
                if e["wicket_type"] == "Run Out":
                    assert players[e["batter"]]["wicket_type"] == "Run Out"
                    seen += 1
    if not seen:
        pytest.skip("no run-outs in sample")


@pytest.mark.parametrize("seed", [1, 2])
def test_first_class_every_innings_balances(seed):
    match, cards, resp = _play(_fc_data("Hard", seed))
    assert cards
    for sc in cards:
        _check_innings(sc)
    for sc in cards:
        assert match.fc_innings_partnerships[sc["innings_number"]] == sc["partnerships"]
    assert "Partnerships" in resp["commentary"]


def test_fc_interval_card_shows_the_stand_in_progress():
    match = match_module.Match(_fc_data("Hard", 3))
    for _ in range(5000):
        resp = match.next_ball()
        if resp.get("fc_interval") or resp.get("day_break"):
            sc = resp["scorecard_data"]
            if sc["wickets"] < 10 and match.current_partnership_balls:
                assert sc["partnerships"][-1]["is_unbroken"]
                assert sc["partnerships"][-1]["runs"] == match.current_partnership_runs
                return
    pytest.fail("no interval card")


def test_old_fc_snapshot_without_boundary_counts_restores():
    match = match_module.Match(_fc_data("Hard", 1))
    for _ in range(40):
        match.next_ball()
    snap = match.serialize_fc_snapshot()
    snap.pop("current_partnership_fours", None)
    snap.pop("current_partnership_sixes", None)
    fresh = match_module.Match(_fc_data("Hard", 1))
    fresh.restore_fc_snapshot(snap)
    assert fresh.current_partnership_fours == 0
    assert fresh.current_partnership_sixes == 0


def test_ordinal_and_fow_text():
    assert [ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd"]
    assert fow_text({"wicket": 3, "score": 112, "batter": "S Iyer", "over": "14.2"}) == "3-112 (S Iyer, 14.2 ov)"
    assert fow_text({"wicket": 3, "score": 112, "batter": None, "over": None}) == "3-112"
