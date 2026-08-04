"""Super-over restart resilience (code review issue 2).

The super over used to live only in MATCH_INSTANCES: a process restart or
24h instance eviction mid-super-over stranded the match — the SO endpoints
404'd and the frontend silently resimulated the whole tied match from the
toss. Now:

1. Match.serialize_super_over_snapshot() / restore_super_over_snapshot()
   round-trip the full super-over state PLUS the tied main match's
   completion payload (archiver/tournament-finalizer inputs) through JSON.
2. The routes persist the snapshot into the match JSON on every super-over
   transition and rebuild+restore instances from it on demand (live-state
   included), so a restart resumes the super over exactly where it stopped.
"""
import json
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.match as match_module


def _build_xi(prefix):
    return [{
        "name": f"{prefix}_P{i+1}",
        "role": "Bowler" if i < 5 else "Batsman",
        "batting_rating": 70, "bowling_rating": 70, "fielding_rating": 65,
        "batting_hand": "Right", "bowling_type": "Medium", "bowling_hand": "Right",
        "will_bowl": i < 5, "is_captain": i == 0,
    } for i in range(11)]


def _make_match(user_id):
    data = {
        "match_id": str(uuid.uuid4()), "created_by": user_id,
        "timestamp": "2026-08-04T12:00:00",
        "team_home": f"TW_{user_id}", "team_away": f"TC_{user_id}",
        "stadium": "Test Ground", "pitch": "Flat",
        "toss": "Heads", "toss_winner": "TW", "toss_decision": "Bat",
        "match_format": "T20", "overs": 20, "simulation_mode": "auto",
        "rain_probability": 0.0,
        "playing_xi": {"home": _build_xi("H"), "away": _build_xi("A")},
        "substitutes": {"home": [], "away": []},
    }
    return match_module.Match(data)


def _tie_match(user_id):
    """A match frozen at the moment a tie created the super over: innings 4,
    awaiting innings-1 selection, with distinctive main-match completion data
    so restore fidelity is assertable."""
    m = _make_match(user_id)
    m.innings = 4
    m.super_over_phase = "awaiting_innings1_selection"
    # Away chased in the (tied) second innings.
    m.batting_team = m.away_xi
    m.bowling_team = m.home_xi
    m.score = 150
    m.wickets = 6
    m.target = 151
    m.first_innings_score = 150
    m.first_batting_team_name = "TW"
    m.first_bowling_team_name = "TC"
    m.batsman_stats = {"A_P1": {"runs": 75, "balls": 50, "fours": 8, "sixes": 2, "wicket_type": ""}}
    m.bowler_stats = {"H_P1": {"runs": 30, "wickets": 2, "balls_bowled": 24}}
    m.first_innings_batting_stats = {"H_P1": {"runs": 80, "balls": 55}}
    m.first_innings_bowling_stats = {"A_P1": {"runs": 25, "wickets": 1}}
    m.first_innings_partnerships = [{"runs": 50, "batters": ["H_P1", "H_P2"]}]
    m.second_innings_partnerships = [{"runs": 30, "batters": ["A_P1", "A_P2"]}]
    m.original_scorecard = {"target_info": "Match Tied"}
    m.commentary = ["19.6 last ball — tied!"]
    m.commentary_replay_log = ["<b>MATCH TIED!</b>"]
    return m


def _json_roundtrip(obj):
    return json.loads(json.dumps(obj))


def _fresh_copy(m):
    """A new Match built from a disk-faithful copy of the same match JSON —
    simulates the post-restart rebuild."""
    return match_module.Match(_json_roundtrip(m.match_data))


# ── 1. Engine round-trip ─────────────────────────────────────────────────────

def test_snapshot_roundtrip_mid_innings(app, regular_user):
    with app.app_context():
        m = _tie_match(regular_user.id)
        assert m.start_super_over("away").get("super_over_started")

        for _ in range(3):
            r = m.next_super_over_ball()
            assert "error" not in r
            if r.get("innings_complete"):
                break

        snap = _json_roundtrip(m.serialize_super_over_snapshot())
        assert snap["super_over"]["phase"] == "innings_in_progress"

        m2 = _fresh_copy(m)
        m2.restore_super_over_snapshot(snap)

        # Super-over state
        assert m2.innings == 4
        assert m2.super_over_phase == "innings_in_progress"
        assert m2.super_over_round == 1
        assert m2.super_over_ball == m.super_over_ball
        assert m2.super_over_scores == m.super_over_scores
        assert m2.super_over_wickets == m.super_over_wickets
        assert m2.super_over_batsman_stats == m.super_over_batsman_stats
        assert m2.super_over_bowler_runs == m.super_over_bowler_runs
        assert m2.super_over_next_batter_idx == m.super_over_next_batter_idx
        assert m2.super_over_current_striker["name"] == m.super_over_current_striker["name"]
        assert m2.super_over_current_non_striker["name"] == m.super_over_current_non_striker["name"]
        assert m2.super_over_bowler["name"] == m.super_over_bowler["name"]
        assert [p["name"] for p in m2.super_over_batsmen] == [p["name"] for p in m.super_over_batsmen]

        # Identity restored against the NEW instance's XIs (engine relies on
        # `is` checks for team keys).
        assert m2.super_over_batting_team is m2.away_xi
        assert m2.super_over_bowling_team is m2.home_xi
        assert m2.batting_team is m2.away_xi

        # Main-match completion payload survives for archive/finalize time.
        assert m2.score == 150 and m2.wickets == 6 and m2.target == 151
        assert m2.batsman_stats == m.batsman_stats
        assert m2.bowler_stats == m.bowler_stats
        assert m2.first_innings_batting_stats == m.first_innings_batting_stats
        assert m2.first_innings_partnerships == m.first_innings_partnerships
        assert m2.first_batting_team_name == "TW"
        assert m2.original_scorecard == {"target_info": "Match Tied"}
        assert m2.commentary_replay_log == ["<b>MATCH TIED!</b>"]


def test_snapshot_at_selection_phase(app, regular_user):
    with app.app_context():
        m = _tie_match(regular_user.id)
        snap = _json_roundtrip(m.serialize_super_over_snapshot())
        assert snap["super_over"]["phase"] == "awaiting_innings1_selection"

        m2 = _fresh_copy(m)
        m2.restore_super_over_snapshot(snap)

        state = m2.get_super_over_resume_state()
        assert state["phase"] == "awaiting_innings1_selection"
        assert state["display_round"] == 1
        assert len(state["home_players"]) == 11
        assert len(state["away_players"]) == 11

        # The normal ball loop must not run on the restored instance.
        nb = m2.next_ball()
        assert nb.get("super_over_in_progress") is True
        assert nb.get("match_over") is False


def test_restore_rejects_corrupt_snapshot(app, regular_user):
    import pytest
    with app.app_context():
        m2 = _fresh_copy(_tie_match(regular_user.id))
        with pytest.raises(ValueError):
            m2.restore_super_over_snapshot({"v": 99})
        with pytest.raises(ValueError):
            m2.restore_super_over_snapshot({
                "v": 1,
                "main_match": {"second_batting_side": "away"},
                "super_over": {"phase": "innings_in_progress", "in_progress": {}},
            })


# ── 2. Restored instance plays out to a decision ─────────────────────────────

def test_restored_match_completes(app, regular_user):
    with app.app_context():
        m = _tie_match(regular_user.id)
        assert m.start_super_over("home").get("super_over_started")
        m.next_super_over_ball()
        m.next_super_over_ball()

        snap = _json_roundtrip(m.serialize_super_over_snapshot())
        m2 = _fresh_copy(m)
        m2.restore_super_over_snapshot(snap)

        guard = 0
        while m2.innings != 5:
            guard += 1
            assert guard < 500, "super over did not converge to a result"
            phase = m2.super_over_phase
            if phase == "awaiting_innings1_selection":
                forced = getattr(m2, "_super_over_next_first_batting", None) or "home"
                r = m2.start_super_over(forced)
                assert r.get("super_over_started"), r
            elif phase == "awaiting_innings2_selection":
                r = m2.start_super_over_innings2()
                assert r.get("super_over_innings2_started"), r
            else:
                r = m2.next_super_over_ball()
                assert "error" not in r, r

        assert m2.super_over_phase == "complete"
        assert m2.result
        assert m2.super_over_history  # at least one completed round recorded
        # Completion consumed the restored main-innings stats.
        assert m2.second_innings_batting_stats == snap["main_match"]["batsman_stats"]
        # Cumulative career stores were fed by every completed innings.
        so_batters = (set(m2.super_over_career_batting["home"])
                      | set(m2.super_over_career_batting["away"]))
        assert so_batters


# ── 3. Route glue: live-state rebuilds from the persisted snapshot ───────────

def test_live_state_rebuilds_from_snapshot(app, authenticated_client, regular_user):
    import app as app_module

    m = _tie_match(regular_user.id)
    assert m.start_super_over("home").get("super_over_started")
    m.next_super_over_ball()
    snap = m.serialize_super_over_snapshot()
    match_id = m.match_data["match_id"]

    match_dir = os.path.join(app_module.PROJECT_ROOT, "data", "matches")
    os.makedirs(match_dir, exist_ok=True)
    path = os.path.join(match_dir, f"match_{match_id}.json")
    data = _json_roundtrip(m.match_data)
    data["super_over_snapshot"] = snap
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    try:
        # Simulate restart: nothing in memory for this match.
        assert match_id not in app_module.MATCH_INSTANCES

        resp = authenticated_client.get(f"/match/{match_id}/live-state")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "in_progress"
        assert body["super_over"]["phase"] == "innings_in_progress"
        assert body["super_over"]["round"] == 1
        assert body["commentary_log"] == ["<b>MATCH TIED!</b>"]

        restored = app_module.MATCH_INSTANCES.get(match_id)
        assert restored is not None
        assert restored.innings == 4
        assert restored.super_over_ball == m.super_over_ball
    finally:
        app_module.MATCH_INSTANCES.pop(match_id, None)
        try:
            os.remove(path)
        except OSError:
            pass
