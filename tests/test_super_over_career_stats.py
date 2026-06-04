"""
Super Over career-stat aggregation (issue #5).

Super-over batting/bowling is written as dedicated MatchScorecard rows at
innings_number=3 so the existing forward AND reverse aggregation paths count
(and un-count, on re-simulation) super-over runs/wickets toward career totals
without a separate drift-prone code path. These tests verify:

  1. SO performances become innings_number=3 scorecard rows.
  2. Career aggregates include SO runs/wickets.
  3. Re-archiving the same match does NOT double-count (reverse symmetry).
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
        "timestamp": "2026-06-03T12:00:00",
        "team_home": f"TW_{user_id}", "team_away": f"TC_{user_id}",
        "stadium": "Test Ground", "pitch": "Flat",
        "toss": "Heads", "toss_winner": "TW", "toss_decision": "Bat",
        "match_format": "T20", "overs": 20, "simulation_mode": "auto",
        "playing_xi": {"home": _build_xi("H"), "away": _build_xi("A")},
        "substitutes": {"home": [], "away": []},
    }
    m = match_module.Match(data)
    # Minimal completed-match shell — main innings intentionally empty so the
    # only scorecard rows are the super-over (innings 3) rows we assert on.
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
    # Super-over career stats: TW (home) batter + bowler, TC (away) batter + bowler.
    m.super_over_career_batting = {
        "home": {"John Doe": {"runs": 10, "balls": 5, "fours": 1, "sixes": 1, "wicket_type": ""}},
        "away": {"Champion 1": {"runs": 7, "balls": 6, "fours": 0, "sixes": 0, "wicket_type": "Bowled"}},
    }
    m.super_over_career_bowling = {
        "home": {"Allrounder 1": {"balls_bowled": 6, "runs": 7, "wickets": 1}},
        "away": {"Champion 2": {"balls_bowled": 6, "runs": 10, "wickets": 0}},
    }
    return m


def _archive(match):
    arch = MatchArchiver(match.match_data, match)
    # _save_to_database needs filenames['json']; create_archive normally sets it.
    arch.filenames = {"json": f"/tmp/{match.match_data['match_id']}.json"}
    ok = arch._save_to_database()
    db.session.commit()
    return ok


def test_super_over_stats_written_as_innings3_and_counted(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_match(regular_user.id)
        assert _archive(match) is not False

        mid = match.match_data["match_id"]

        # 1) SO rows exist at innings_number = 3
        so_cards = MatchScorecard.query.filter_by(match_id=mid, innings_number=3).all()
        assert so_cards, "no super-over scorecard rows written"
        bat_card = MatchScorecard.query.filter_by(
            match_id=mid, innings_number=3, record_type="batting"
        ).join(DBPlayer, MatchScorecard.player_id == DBPlayer.id).filter(
            DBPlayer.name == "John Doe"
        ).first()
        assert bat_card is not None and bat_card.runs == 10 and bat_card.sixes == 1

        # No phantom innings 1/2 rows (main innings was empty)
        assert MatchScorecard.query.filter(
            MatchScorecard.match_id == mid, MatchScorecard.innings_number.in_([1, 2])
        ).count() == 0

        # 2) Career aggregates include the SO contribution
        john = DBPlayer.query.filter_by(name="John Doe").first()
        allr = DBPlayer.query.filter_by(name="Allrounder 1").first()
        assert john.total_runs == 10
        assert john.total_sixes == 1
        assert john.matches_played == 1
        assert allr.total_wickets == 1
        assert allr.total_balls_bowled == 6


def test_re_archive_does_not_double_count(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_match(regular_user.id)
        _archive(match)

        john = DBPlayer.query.filter_by(name="John Doe").first()
        assert john.total_runs == 10 and john.matches_played == 1

        # Re-archive the SAME match (re-simulation path): reverse then re-add.
        _archive(match)
        db.session.refresh(john)
        allr = DBPlayer.query.filter_by(name="Allrounder 1").first()

        # Totals must be unchanged — proves innings-3 rows reverse cleanly.
        assert john.total_runs == 10, f"double-counted: {john.total_runs}"
        assert john.matches_played == 1, f"matches double-counted: {john.matches_played}"
        assert allr.total_wickets == 1
