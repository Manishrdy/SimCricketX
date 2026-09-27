"""Deterministic Super Five free-hit scoring and recovery regressions."""
import json

import pytest

import engine.match as module
from engine.hundred_snapshot import restore, serialize
from tests.test_hundred import make, dot, quiet


@pytest.fixture
def super_five(monkeypatch):
    monkeypatch.setattr(module, "calculate_outcome", dot)
    match = make(is_knockout=True)
    for _ in range(210):
        if match.next_ball().get("super_over_required"):
            break
    result = match.start_super_over(match._super_over_next_first_batting)
    assert not result.get("error"), result
    return match


def deliver(monkeypatch, match, *, runs=0, extra=None, wicket=None):
    outcome = dict(runs=runs, batter_out=bool(wicket), is_extra=bool(extra),
                   extra_type=extra, wicket_type=wicket,
                   type="wicket" if wicket else "extra" if extra else "run",
                   description="Out!" if wicket else "Delivery")
    monkeypatch.setattr(module, "calculate_super_over_outcome", lambda **kw: dict(outcome))
    return match.next_super_over_ball()


@pytest.mark.parametrize("dismissal", ["Bowled", "Caught", "LBW", "Stumped"])
def test_prohibited_dismissals_do_not_reach_stats_or_history(super_five, monkeypatch, dismissal):
    m = super_five
    name = m.super_over_current_striker["name"]
    first = deliver(monkeypatch, m, runs=1, extra="No Ball")
    assert first["free_hit"] is False
    result = deliver(monkeypatch, m, wicket=dismissal)
    assert result["free_hit"] and result["ball_data"]["free_hit"]
    assert "Free hit" in result["commentary"]
    assert not result["wicket"] and result["wickets"] == 0
    assert result["score"] == 1 and result["ball"] == 1
    assert m.super_over_bowler_wickets == 0
    assert not m.super_over_batsman_stats[name]["out"]
    assert m.super_over_batsman_stats[name]["balls"] == 2
    assert m.super_over_ball_history[-1]["label"] == "Dot"
    assert not m.super_over_ball_history[-1]["is_wicket"]
    assert not m.super_five_free_hit_active
    assert deliver(monkeypatch, m, wicket="Bowled")["wickets"] == 1


def test_run_out_counts_on_free_hit(super_five, monkeypatch):
    m = super_five
    deliver(monkeypatch, m, runs=1, extra="No Ball")
    result = deliver(monkeypatch, m, runs=1, wicket="Run Out")
    assert result["free_hit"] and result["wicket"]
    assert result["score"] == 2 and result["wickets"] == 1 and result["ball"] == 1
    assert m.super_over_bowler_wickets == 0
    assert sum(s["out"] for s in m.super_over_batsman_stats.values()) == 1
    assert m.super_over_ball_history[-1]["is_wicket"]
    assert not m.super_five_free_hit_active


@pytest.mark.parametrize("extra,runs", [(None, 4), ("Byes", 2), ("Leg Bye", 1)])
def test_extra_chain_and_legal_delivery_consumption(super_five, monkeypatch, extra, runs):
    m = super_five
    for index, illegal in enumerate(["No Ball", "Wide", "No Ball", "Wide"]):
        result = deliver(monkeypatch, m, runs=1, extra=illegal)
        assert result["free_hit"] is (index > 0)
        assert result["ball"] == 0 and m.super_five_free_hit_active
    result = deliver(monkeypatch, m, runs=runs, extra=extra)
    assert result["free_hit"] and result["score"] == 4 + runs
    assert result["ball"] == 1 and not m.super_five_free_hit_active
    assert m.super_over_bowler_runs == (4 + runs if extra is None else 4)
    assert sum(s["runs"] for s in m.super_over_batsman_stats.values()) == (runs if extra is None else 0)
    result = deliver(monkeypatch, m, wicket="Bowled")
    assert not result["free_hit"] and result["wickets"] == 1


def test_protection_preserves_extra_penalty(super_five, monkeypatch):
    m = super_five
    deliver(monkeypatch, m, runs=1, extra="No Ball")
    result = deliver(monkeypatch, m, runs=1, extra="Wide", wicket="Stumped")
    assert not result["wicket"] and result["score"] == 2 and result["ball"] == 0
    assert m.super_over_bowler_runs == 2 and m.super_five_free_hit_active
    assert m.super_over_ball_history[-1]["label"] == "Wide"


def test_checkpoint_preserves_pending_free_hit(super_five, monkeypatch):
    m = super_five
    deliver(monkeypatch, m, runs=1, extra="No Ball")
    restored = make()
    restore(restored, json.loads(json.dumps(serialize(m))))
    assert restored.super_five_free_hit_active
    result = deliver(monkeypatch, restored, wicket="Caught")
    assert result["free_hit"] and not result["wicket"]
    assert result["score"] == 1 and result["ball"] == 1
    # Old snapshots must overwrite even an already-active destination flag.
    del m.super_five_free_hit_active
    restored.super_five_free_hit_active = True
    restore(restored, json.loads(json.dumps(serialize(m))))
    assert not restored.super_five_free_hit_active


def test_new_innings_and_round_reset_free_hit(super_five, monkeypatch):
    m = super_five
    for innings in (1, 2):
        # Exercise the pending flag at a boundary without relying on an illegal
        # delivery ending an innings (which cannot normally happen).
        for _ in range(5):
            deliver(monkeypatch, m)
        m.super_five_free_hit_active = True
        m.next_super_over_ball()
        if innings == 1:
            result = m.start_super_over_innings2()
        else:
            result = m.start_super_over(m._super_over_next_first_batting)
        assert not result.get("error"), result
        assert not m.super_five_free_hit_active
