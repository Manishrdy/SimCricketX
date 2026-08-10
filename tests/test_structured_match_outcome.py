"""
Regression: winner_team_id / margin_type / margin_value / match_status must
be read directly from the structured fields the engine sets at decision time
(Match._set_outcome), not re-parsed from the result_description prose.

Root cause (fixed): match_archiver.py used to regex-match "X won by N runs"
out of self.match.result and startswith()-match a short code against the
same prose to find the winner. Any phrasing drift silently produced no
winner/margin. These tests hand-set the structured fields the same way
Match._set_outcome does (see engine/match.py) and assert the archiver
consumes them directly, without touching result_description parsing.
"""
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db
from database.models import Match as DBMatch
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
    m.first_batting_team_name = "TW"
    m.first_innings_score = 65
    m.first_innings_batting_stats = {"John Doe": {"runs": 40, "balls": 30, "fours": 4, "sixes": 1}}
    m.first_innings_bowling_stats = {"Champion 1": {"balls_bowled": 24, "runs": 30, "wickets": 2, "maidens": 0}}
    m.second_innings_batting_stats = {"Champ Bat 1": {"runs": 30, "balls": 22, "fours": 3, "sixes": 0}}
    m.second_innings_bowling_stats = {"Allrounder 1": {"balls_bowled": 24, "runs": 28, "wickets": 1, "maidens": 0}}
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


def test_home_win_by_runs_resolves_from_structured_fields(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_match(regular_user.id)
        match._set_outcome(
            result_text="TW won by 15 run(s).",
            winner_is_home=True, match_status='completed',
            margin_type='runs', margin_value=15,
        )
        assert _archive(match) is not False

        db_match = DBMatch.query.get(match.match_data["match_id"])
        assert db_match.winner_team_id == db_match.home_team_id
        assert db_match.match_status == 'completed'
        assert db_match.margin_type == 'runs'
        assert db_match.margin_value == 15


def test_away_win_by_wickets_resolves_from_structured_fields(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_match(regular_user.id)
        match._set_outcome(
            result_text="TC won by 4 wicket(s) with 2.3 overs remaining.",
            winner_is_home=False, match_status='completed',
            margin_type='wickets', margin_value=4,
        )
        assert _archive(match) is not False

        db_match = DBMatch.query.get(match.match_data["match_id"])
        assert db_match.winner_team_id == db_match.away_team_id
        assert db_match.match_status == 'completed'
        assert db_match.margin_type == 'wickets'
        assert db_match.margin_value == 4


def test_tie_has_no_winner_and_tied_status(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_match(regular_user.id)
        match._set_outcome(
            result_text="Match Drawn after 5 Super Overs — scores and boundaries level",
            winner_is_home=None, match_status='tied',
            margin_type='tie', margin_value=None,
        )
        assert _archive(match) is not False

        db_match = DBMatch.query.get(match.match_data["match_id"])
        assert db_match.winner_team_id is None
        assert db_match.match_status == 'tied'
        assert db_match.margin_type == 'tie'
        assert db_match.margin_value is None
