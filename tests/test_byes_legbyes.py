"""
Regression: byes/leg-byes are tracked live by the engine per bowler
(self.bowler_stats[...]["byes"/"legbyes"]) and must persist to
MatchScorecard.byes/.leg_byes, instead of being dropped at archive time
and later guessed by view_scoreboard as `score - batting_runs - wides - noballs`
(a remainder that silently absorbs any unrelated accounting bug).
"""
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db
from database.models import MatchScorecard, Player as DBPlayer
import engine.match as match_module
from match_archiver import MatchArchiver
from engine.stats_service import StatsService


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
    m.result = "TW won by 15 runs"
    m.first_batting_team_name = "TW"
    m.first_innings_score = 65
    m.first_innings_batting_stats = {"John Doe": {"runs": 40, "balls": 30, "fours": 4, "sixes": 1}}
    m.first_innings_bowling_stats = {
        "Champion 1": {"balls_bowled": 23, "runs": 30, "wickets": 2, "maidens": 0, "byes": 3, "legbyes": 2},
    }
    m.second_innings_batting_stats = {}
    m.second_innings_bowling_stats = {}
    m.first_innings_partnerships = []
    m.second_innings_partnerships = []
    m.super_over_career_batting = {}
    m.super_over_career_bowling = {}
    return m


def _archive(match):
    arch = MatchArchiver(match.match_data, match)
    arch.filenames = {"json": f"/tmp/{match.match_data['match_id']}.json"}
    ok = arch._save_to_database()
    db.session.commit()
    return ok


def test_byes_and_legbyes_persist_to_scorecard(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_match(regular_user.id)
        assert _archive(match) is not False

        champ1 = DBPlayer.query.filter_by(name="Champion 1").first()
        card = MatchScorecard.query.filter_by(
            match_id=match.match_data["match_id"], player_id=champ1.id, record_type="bowling"
        ).first()
        assert card.byes == 3
        assert card.leg_byes == 2
        # 23 balls -> 3 overs, 5 balls -- and derived from balls_bowled, not
        # the (unset) whole-overs counter.
        assert card.overs == "3.5"
        assert card.balls_bowled == 23


def test_byes_and_legbyes_flow_into_aggregate_bowling_stats(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_match(regular_user.id)
        _archive(match)

        service = StatsService()
        stats = service.get_overall_stats(regular_user.id)
        champ1_row = next(r for r in stats["bowling"] if r["player"] == "Champion 1")
        assert champ1_row["byes"] == 3
        assert champ1_row["leg_byes"] == 2
