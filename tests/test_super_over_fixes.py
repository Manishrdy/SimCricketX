"""
Super-over hardening fixes (code review follow-ups).

1. Phase guards: a duplicate/retried call to next_super_over_ball /
   start_super_over / start_super_over_innings2 outside its valid phase is
   rejected instead of re-running _end_super_over_innings on stale state
   (which double-counted stats under the swapped team key and declared a
   winner before innings 2 was played).

2. is_super_over discriminator: super-over scorecard rows carry an explicit
   flag, count toward career TOTALS only, and never set innings-shaped stats
   (highest score, fifties, not-outs, best bowling, five-fors).
"""
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db
from database.models import MatchScorecard, Player as DBPlayer
import engine.match as match_module
from match_archiver import MatchArchiver


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
        "timestamp": "2026-06-09T12:00:00",
        "team_home": f"TW_{user_id}", "team_away": f"TC_{user_id}",
        "stadium": "Test Ground", "pitch": "Flat",
        "toss": "Heads", "toss_winner": "TW", "toss_decision": "Bat",
        "match_format": "T20", "overs": 20, "simulation_mode": "auto",
        "playing_xi": {"home": _build_xi("H"), "away": _build_xi("A")},
        "substitutes": {"home": [], "away": []},
    }
    return match_module.Match(data)


def _make_completed_so_match(user_id):
    m = _make_match(user_id)
    m.result = "TW won by Super Over"
    m.first_batting_team_name = "TW"
    m.first_innings_score = 120
    m.score = 120
    m.wickets = 5
    m.first_innings_batting_stats = {}
    m.first_innings_bowling_stats = {}
    m.second_innings_batting_stats = {}
    m.second_innings_bowling_stats = {}
    m.first_innings_partnerships = []
    m.second_innings_partnerships = []
    # A multi-round super-over haul big enough to trip every innings-shaped
    # stat if it were (wrongly) treated as a real innings: a not-out fifty
    # and a 2-wicket spell.
    m.super_over_career_batting = {
        "home": {"John Doe": {"runs": 60, "balls": 20, "fours": 6, "sixes": 4, "wicket_type": ""}},
        "away": {},
    }
    m.super_over_career_bowling = {
        "home": {"Allrounder 1": {"balls_bowled": 12, "runs": 5, "wickets": 2}},
        "away": {},
    }
    return m


def _archive(match):
    arch = MatchArchiver(match.match_data, match)
    arch.filenames = {"json": f"/tmp/{match.match_data['match_id']}.json"}
    ok = arch._save_to_database()
    db.session.commit()
    return ok


# ── 1. Phase guards ──────────────────────────────────────────────────────────

def test_next_super_over_ball_rejected_outside_innings(app, regular_user):
    with app.app_context():
        m = _make_match(regular_user.id)

        # No super over at all
        res = m.next_super_over_ball()
        assert res.get("error") == "super_over_not_in_progress"

        # Between innings (the stale-state window: ball counter still >= 6,
        # teams already swapped) — a retried POST must NOT re-end the innings.
        m.super_over_phase = "awaiting_innings2_selection"
        res = m.next_super_over_ball()
        assert res.get("error") == "super_over_not_in_progress"
        assert res.get("phase") == "awaiting_innings2_selection"

        # After the match is decided
        m.super_over_phase = "complete"
        res = m.next_super_over_ball()
        assert res.get("error") == "super_over_not_in_progress"


def test_start_super_over_rejected_outside_selection_phase(app, regular_user):
    with app.app_context():
        m = _make_match(regular_user.id)

        # Tie not reached yet — no selection pending
        res = m.start_super_over("home")
        assert res.get("error") == "super_over_not_awaiting_selection"

        # Duplicate submit while an innings is running must not bump the
        # round counter or reset scores.
        m.super_over_phase = "innings_in_progress"
        round_before = m.super_over_round
        res = m.start_super_over("home")
        assert res.get("error") == "super_over_not_awaiting_selection"
        assert m.super_over_round == round_before


def test_start_innings2_rejected_after_innings_started(app, regular_user):
    with app.app_context():
        m = _make_match(regular_user.id)
        # Once innings 2 is running, a duplicate submit would wipe its state
        # via _init_super_over_innings_state.
        m.super_over_phase = "innings_in_progress"
        res = m.start_super_over_innings2()
        assert res.get("error") == "super_over_not_awaiting_selection"


def test_super_over_guard_to_end_flow(app, regular_user):
    """Drive a real super over through the engine and assert the guards
    don't get in the way of the legitimate flow."""
    with app.app_context():
        m = _make_match(regular_user.id)
        m.innings = 4
        setup = m._setup_super_over()
        assert setup["super_over_required"]

        started = m.start_super_over("home")
        assert started.get("super_over_started"), started

        # Innings 1: legitimate calls flow until the innings ends.
        for _ in range(40):
            res = m.next_super_over_ball()
            assert res.get("error") is None, res
            if res.get("super_over_innings_end"):
                break
        else:
            raise AssertionError("innings 1 never ended")

        # The stale-state window: a duplicate POST here used to re-run
        # _end_super_over_innings and decide the match. Now it's rejected
        # and the accumulated boundary counts are untouched.
        boundaries_before = dict(m.super_over_team_boundaries)
        dup = m.next_super_over_ball()
        assert dup.get("error") == "super_over_not_in_progress"
        assert m.super_over_team_boundaries == boundaries_before
        assert m.super_over_phase == "awaiting_innings2_selection"

        started2 = m.start_super_over_innings2()
        assert started2.get("super_over_innings2_started"), started2


# ── 2. is_super_over discriminator ───────────────────────────────────────────

def test_super_over_rows_flagged_and_totals_only(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_completed_so_match(regular_user.id)
        assert _archive(match) is not False

        mid = match.match_data["match_id"]

        # Rows carry the explicit discriminator
        so_cards = MatchScorecard.query.filter_by(match_id=mid, is_super_over=True).all()
        assert so_cards, "no flagged super-over rows written"
        assert all(c.innings_number == 3 for c in so_cards)

        john = DBPlayer.query.filter_by(name="John Doe").first()
        allr = DBPlayer.query.filter_by(name="Allrounder 1").first()

        # Career TOTALS include the super over (designed behavior)
        assert john.total_runs == 60
        assert john.total_sixes == 4
        assert allr.total_wickets == 2

        # Innings-shaped stats must NOT be minted from a super over
        assert john.total_fifties == 0, "super-over knock minted a fifty"
        assert john.highest_score == 0, "super-over knock set career highest score"
        assert john.not_outs == 0, "super-over not-out counted as an innings not-out"
        assert allr.best_bowling_wickets == 0, "super-over spell set career best bowling"


def test_super_over_rows_reverse_cleanly_on_re_archive(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_completed_so_match(regular_user.id)
        _archive(match)
        _archive(match)  # re-simulation path: reverse then re-add

        john = DBPlayer.query.filter_by(name="John Doe").first()
        allr = DBPlayer.query.filter_by(name="Allrounder 1").first()
        assert john.total_runs == 60, f"double-counted: {john.total_runs}"
        assert john.total_fifties == 0
        assert john.not_outs == 0
        assert john.matches_played == 1
        assert allr.total_wickets == 2
        assert allr.best_bowling_wickets == 0
