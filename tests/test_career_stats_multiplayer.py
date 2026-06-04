"""
Regression: career aggregates must update for EVERY batter/bowler in a match.

Root cause (fixed): `_save_to_database()` aggregated career totals by iterating
`db.session.new`. The session runs with autoflush=True (Flask-SQLAlchemy
default), and each save_stats() call issues a `MatchScorecard.query.filter_by`
lookup that autoflushes previously-added cards out of `db.session.new`. By the
time the aggregate loop ran, only the LAST card added survived in the pending
set — so for every match only one player's totals (total_runs/total_wickets/
matches_played/…) were updated and everyone else got nothing.

The fix aggregates from the match's PERSISTED rows (flush + query by match_id),
which is immune to autoflush timing and covers innings 1, 2 and super-over 3.

These tests assert:
  1. A match with multiple batters AND bowlers updates ALL their aggregates.
  2. Re-archiving the same match does not double-count (reverse symmetry).
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
    """Build a completed two-innings match.

    TW (home, Test Warriors) bats first; TC (away, Test Champions) bats second.
    Player names below match the conftest test_team / test_team_2 fixtures and
    are deliberately spread across both teams and both innings so a single
    surviving-card bug would leave most of them at zero.
    """
    data = {
        "match_id": str(uuid.uuid4()), "created_by": user_id,
        "timestamp": "2026-06-03T12:00:00",
        "team_home": f"TW_{user_id}", "team_away": f"TC_{user_id}",
        "stadium": "Test Ground", "pitch": "Flat",
        "toss": "Heads", "toss_winner": "TW", "toss_decision": "Bat",
        "match_format": "T20", "overs": 20, "simulation_mode": "auto",
        "playing_xi": {"home": _build_xi("H"), "away": _build_xi("A")},
        "substitutes": {"home": [], "away": []},
    }
    m = match_module.Match(data)
    m.result = "TW won by 15 runs"
    m.first_batting_team_name = "TW"
    m.first_innings_score = 65
    m.score = 50
    m.wickets = 3

    # Innings 1: TW bat (two batters), TC bowl (one bowler).
    # No wicket_type / fielder_out -> no fielding cards, keeps assertions clean.
    m.first_innings_batting_stats = {
        "John Doe": {"runs": 40, "balls": 30, "fours": 4, "sixes": 1},
        "Batsman 1": {"runs": 25, "balls": 20, "fours": 2, "sixes": 0},
    }
    m.first_innings_bowling_stats = {
        "Champion 1": {"balls_bowled": 24, "runs": 30, "wickets": 2, "maidens": 0},
    }
    # Innings 2: TC bat (one batter), TW bowl (one bowler).
    m.second_innings_batting_stats = {
        "Champ Bat 1": {"runs": 30, "balls": 22, "fours": 3, "sixes": 0},
    }
    m.second_innings_bowling_stats = {
        "Allrounder 1": {"balls_bowled": 24, "runs": 28, "wickets": 1, "maidens": 0},
    }
    m.first_innings_partnerships = []
    m.second_innings_partnerships = []
    # No super over in this match.
    m.super_over_career_batting = {}
    m.super_over_career_bowling = {}
    return m


def _archive(match):
    arch = MatchArchiver(match.match_data, match)
    arch.filenames = {"json": f"/tmp/{match.match_data['match_id']}.json"}
    ok = arch._save_to_database()
    db.session.commit()
    return ok


def test_all_batters_and_bowlers_get_career_aggregates(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_match(regular_user.id)
        assert _archive(match) is not False

        john = DBPlayer.query.filter_by(name="John Doe").first()
        bats1 = DBPlayer.query.filter_by(name="Batsman 1").first()
        champ1 = DBPlayer.query.filter_by(name="Champion 1").first()
        champbat1 = DBPlayer.query.filter_by(name="Champ Bat 1").first()
        allr1 = DBPlayer.query.filter_by(name="Allrounder 1").first()

        # EVERY batter aggregates — not just the last card added. This is the
        # exact regression: pre-fix, John Doe and Batsman 1 stayed at zero.
        assert john.total_runs == 40, f"John Doe under-counted: {john.total_runs}"
        assert john.total_balls_faced == 30
        assert john.total_fours == 4 and john.total_sixes == 1
        assert john.matches_played == 1

        assert bats1.total_runs == 25, f"Batsman 1 under-counted: {bats1.total_runs}"
        assert bats1.matches_played == 1

        assert champbat1.total_runs == 30
        assert champbat1.matches_played == 1

        # EVERY bowler aggregates across both innings/teams.
        assert champ1.total_wickets == 2, f"Champion 1 under-counted: {champ1.total_wickets}"
        assert champ1.total_balls_bowled == 24
        assert champ1.matches_played == 1

        assert allr1.total_wickets == 1
        assert allr1.total_balls_bowled == 24
        assert allr1.matches_played == 1

        # highest_score / best_bowling high-water marks also land per player.
        assert john.highest_score == 40
        assert champ1.best_bowling_wickets == 2


def test_re_archive_does_not_double_count(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_match(regular_user.id)
        _archive(match)

        john = DBPlayer.query.filter_by(name="John Doe").first()
        bats1 = DBPlayer.query.filter_by(name="Batsman 1").first()
        champ1 = DBPlayer.query.filter_by(name="Champion 1").first()
        assert john.total_runs == 40 and john.matches_played == 1
        assert bats1.total_runs == 25 and bats1.matches_played == 1
        assert champ1.total_wickets == 2 and champ1.matches_played == 1

        # Re-archive the SAME match (re-simulation path): reverse then re-add.
        _archive(match)
        for p in (john, bats1, champ1):
            db.session.refresh(p)

        # Totals unchanged for ALL players — proves the reverse path covers the
        # same rows the forward path now aggregates.
        assert john.total_runs == 40, f"John Doe double-counted: {john.total_runs}"
        assert john.matches_played == 1, f"matches double-counted: {john.matches_played}"
        assert bats1.total_runs == 25, f"Batsman 1 double-counted: {bats1.total_runs}"
        assert bats1.matches_played == 1
        assert champ1.total_wickets == 2, f"Champion 1 double-counted: {champ1.total_wickets}"
        assert champ1.matches_played == 1
