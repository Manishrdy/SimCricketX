"""
First-Class (FC) match resume resilience.

Before this, an FC match lived only in MATCH_INSTANCES: a process restart
or 24h instance eviction mid-match stranded it — _get_or_restore_match_instance
would rebuild a fresh Match(match_data) with no snapshot to restore, silently
restarting the match from fc_innings=1. Now:

1. Match.serialize_fc_snapshot() / restore_fc_snapshot() round-trip the live
   FC match state through JSON, following the exact discipline already
   established by serialize_super_over_snapshot()/restore_super_over_snapshot()
   (team identity as home/away tags, players re-resolved by name).
2. The routes persist the snapshot after every over/innings/day boundary and
   rebuild+restore instances from it on demand (next-ball and live-state
   both), so a restart resumes exactly where it stopped instead of restarting.
"""
import copy
import json
import os
import sys
import uuid

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.match as match_module
from tests.test_fc_format import _squad


HOME = _squad("HOM")
AWAY = _squad("AWY")


def _fc_match_data(user_id, days=5, pitch="Hard"):
    return {
        "match_id": str(uuid.uuid4()), "created_by": user_id,
        "timestamp": "2026-08-09T12:00:00",
        "team_home": "HOM_1", "team_away": "AWY_1",
        "stadium": "Test Ground", "pitch": pitch,
        "toss": "Heads", "toss_winner": "HOM", "toss_decision": "Bat",
        "match_format": "FC", "days": days, "simulation_mode": "auto",
        "rain_probability": 0.0,
        "playing_xi": {"home": copy.deepcopy(HOME), "away": copy.deepcopy(AWAY)},
        "substitutes": {"home": [], "away": []},
        "weather_forecast": "clear",
    }


def _json_roundtrip(obj):
    return json.loads(json.dumps(obj))


def _fresh_copy(m):
    """A new, independent Match built from a disk-faithful copy of the same
    match JSON — simulates the post-restart rebuild."""
    return match_module.Match(_json_roundtrip(m.match_data))


# ── 1. Engine round-trip ─────────────────────────────────────────────────────

def test_fc_snapshot_roundtrip_mid_match(app, regular_user):
    with app.app_context():
        m = match_module.Match(_fc_match_data(regular_user.id))
        for _ in range(300):
            r = m.next_ball()
            assert "error" not in r
            if r.get("match_over"):
                break
            if m.fc_innings >= 2:
                break

        snap = _json_roundtrip(m.serialize_fc_snapshot())
        assert snap["v"] == 1

        m2 = _fresh_copy(m)
        m2.restore_fc_snapshot(snap)

        assert m2.fc_innings == m.fc_innings
        assert m2.score == m.score and m2.wickets == m.wickets
        assert m2.current_over == m.current_over and m2.current_ball == m.current_ball
        assert m2.fc_day == m.fc_day
        assert m2.fc_day_overs_bowled_today == m.fc_day_overs_bowled_today
        assert m2.fc_ball_overs_bowled == m.fc_ball_overs_bowled
        assert m2.match_balls_bowled == m.match_balls_bowled
        assert m2.fc_innings_totals == m.fc_innings_totals
        assert all(isinstance(k, int) for k in m2.fc_innings_totals.keys())
        assert m2.fc_innings_stats == m.fc_innings_stats
        assert m2.batsman_stats == m.batsman_stats
        assert m2.bowler_stats == m.bowler_stats
        assert m2.bowler_manager._overs_this_innings == m.bowler_manager._overs_this_innings
        assert m2.bowler_manager._last_bowler == m.bowler_manager._last_bowler

        # Identity restored against the NEW instance's XIs.
        assert m2.batting_team is (m2.home_xi if m.batting_team is m.home_xi else m2.away_xi)
        assert m2.bowling_team is (m2.home_xi if m.bowling_team is m.home_xi else m2.away_xi)
        assert m2.current_striker["name"] == m.current_striker["name"]
        assert m2.current_non_striker["name"] == m.current_non_striker["name"]
        if m.current_bowler:
            assert m2.current_bowler["name"] == m.current_bowler["name"]


def test_fc_snapshot_none_when_not_fc(app, regular_user):
    with app.app_context():
        data = _fc_match_data(regular_user.id)
        data["match_format"] = "T20"
        data["overs"] = 20
        m = match_module.Match(data)
        assert m.serialize_fc_snapshot() is None


def test_fc_restore_rejects_corrupt_snapshot(app, regular_user):
    with app.app_context():
        m2 = _fresh_copy(match_module.Match(_fc_match_data(regular_user.id)))
        with pytest.raises(ValueError):
            m2.restore_fc_snapshot({"v": 99})
        with pytest.raises(ValueError):
            m2.restore_fc_snapshot({"v": 1, "batting_side": "home", "bowling_side": "away"})


# ── 2. Restored instance keeps playing correctly ─────────────────────────────

def test_fc_restored_match_continues_playing(app, regular_user):
    with app.app_context():
        m = match_module.Match(_fc_match_data(regular_user.id))
        for _ in range(200):
            r = m.next_ball()
            if r.get("match_over") or m.fc_innings >= 2:
                break

        snap = _json_roundtrip(m.serialize_fc_snapshot())
        m2 = _fresh_copy(m)
        m2.restore_fc_snapshot(snap)

        for _ in range(50):
            r2 = m2.next_ball()
            assert "error" not in r2, r2
        # No crash, and the match kept legally advancing.
        assert m2.match_balls_bowled >= m.match_balls_bowled


# ── 3. Route glue: live-state and next-ball rebuild from the persisted
#      snapshot after a simulated restart ───────────────────────────────────

def test_fc_live_state_rebuilds_from_snapshot(app, authenticated_client, regular_user):
    import app as app_module

    m = match_module.Match(_fc_match_data(regular_user.id))
    for _ in range(200):
        r = m.next_ball()
        if r.get("match_over") or m.fc_innings >= 2 or m.current_over >= 2:
            break
    snap = m.serialize_fc_snapshot()
    assert snap is not None
    match_id = m.match_data["match_id"]

    match_dir = os.path.join(app_module.PROJECT_ROOT, "data", "matches")
    os.makedirs(match_dir, exist_ok=True)
    path = os.path.join(match_dir, f"match_{match_id}.json")
    data = _json_roundtrip(m.match_data)
    data["fc_snapshot"] = snap
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    try:
        # Simulate restart: nothing in memory for this match.
        assert match_id not in app_module.MATCH_INSTANCES

        resp = authenticated_client.get(f"/match/{match_id}/live-state")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "in_progress"
        assert body["innings"] == m.fc_innings
        assert body["score"] == m.score
        assert body["wickets"] == m.wickets

        restored = app_module.MATCH_INSTANCES.get(match_id)
        assert restored is not None
        assert restored.is_fc is True
        assert restored.fc_innings == m.fc_innings
        assert restored.fc_day == m.fc_day
        assert restored.current_over == m.current_over
    finally:
        app_module.MATCH_INSTANCES.pop(match_id, None)
        try:
            os.remove(path)
        except OSError:
            pass


def test_fc_next_ball_persists_snapshot_at_over_boundary(app, authenticated_client, regular_user):
    """After an over completes via the real /next-ball route, the match JSON
    on disk must carry an fc_snapshot — not just the in-memory instance."""
    import app as app_module

    m = match_module.Match(_fc_match_data(regular_user.id))
    match_id = m.match_data["match_id"]
    match_dir = os.path.join(app_module.PROJECT_ROOT, "data", "matches")
    os.makedirs(match_dir, exist_ok=True)
    path = os.path.join(match_dir, f"match_{match_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_json_roundtrip(m.match_data), f)

    try:
        with app_module.MATCH_INSTANCES_LOCK:
            app_module.MATCH_INSTANCES[match_id] = m

        for _ in range(60):
            resp = authenticated_client.post(f"/match/{match_id}/next-ball")
            assert resp.status_code == 200
            body = resp.get_json()
            if body.get("match_over"):
                break
            if m.current_over >= 1 and m.current_ball == 0:
                break

        with open(path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk.get("fc_snapshot") is not None
        assert on_disk["fc_snapshot"]["v"] == 1
    finally:
        app_module.MATCH_INSTANCES.pop(match_id, None)
        try:
            os.remove(path)
        except OSError:
            pass
