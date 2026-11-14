"""
Regression: player identity should resolve by DB id (carried through the
engine's player dicts from DBPlayer.id) when available, falling back to the
existing name-based lookup for older matches / hand-built fixtures that
never had an id attached. A total miss must be a loud failure, not a
silent `continue`.

Root cause (fixed): the dict handed to the match engine never carried
DBPlayer.id, so match_archiver.py could only resolve players by name at
archive time. A name mismatch (rename, duplicate, whitespace/unicode drift)
silently dropped that player's entire scorecard row while the match still
committed successfully.
"""
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db
from database.models import Match as DBMatch, MatchScorecard, Player as DBPlayer, ExceptionLog
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
    m.result = "TW won by 15 runs"
    m.first_batting_team_name = "TW"
    m.first_innings_score = 65
    m.first_innings_bowling_stats = {}
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


def test_id_resolves_even_with_mismatched_name(app, regular_user, test_team, test_team_2):
    """A stats-dict entry carrying the real DBPlayer.id must resolve to that
    player even if the dict key (name) doesn't match anything in the DB —
    proves id-based lookup is actually preferred, not just tolerated."""
    with app.app_context():
        john = DBPlayer.query.filter_by(name="John Doe").first()
        match = _make_match(regular_user.id)
        # Deliberately wrong/garbage name, but the real id.
        match.first_innings_batting_stats = {
            "Totally Different Name": {"runs": 40, "balls": 30, "fours": 4, "sixes": 1, "id": john.id},
        }
        assert _archive(match) is not False

        card = MatchScorecard.query.filter_by(match_id=match.match_data["match_id"], player_id=john.id).first()
        assert card is not None
        assert card.runs == 40

        db_match = DBMatch.query.get(match.match_data["match_id"])
        assert db_match.stats_incomplete is False


def test_missing_id_falls_back_to_name_lookup(app, regular_user, test_team, test_team_2):
    """Stats dicts without an id (legacy JSON, hand-built fixtures) must keep
    resolving by name exactly as before."""
    with app.app_context():
        match = _make_match(regular_user.id)
        match.first_innings_batting_stats = {
            "John Doe": {"runs": 22, "balls": 18, "fours": 1, "sixes": 0},
        }
        assert _archive(match) is not False

        john = DBPlayer.query.filter_by(name="John Doe").first()
        card = MatchScorecard.query.filter_by(match_id=match.match_data["match_id"], player_id=john.id).first()
        assert card is not None
        assert card.runs == 22


def test_unresolvable_player_sets_loud_failure(app, regular_user, test_team, test_team_2):
    """A player that can't be resolved by id or name must: skip only that
    row (not abort the match), flag stats_incomplete, and log a loud
    (non-silent) anomaly instead of just a debug-level warning."""
    with app.app_context():
        before_anomalies = ExceptionLog.query.filter_by(exception_type="ScorecardPlayerLookupMiss").count()

        match = _make_match(regular_user.id)
        match.first_innings_batting_stats = {
            "Nobody Real": {"runs": 99, "balls": 50, "fours": 0, "sixes": 0, "id": 999999},
        }
        assert _archive(match) is not False

        db_match = DBMatch.query.get(match.match_data["match_id"])
        assert db_match.stats_incomplete is True

        card = MatchScorecard.query.filter_by(
            match_id=match.match_data["match_id"], runs=99
        ).first()
        assert card is None  # the row was skipped, not fabricated with a bad FK

        after_anomalies = ExceptionLog.query.filter_by(exception_type="ScorecardPlayerLookupMiss").count()
        assert after_anomalies == before_anomalies + 1


def _stand(b1, b2, **over):
    stand = {
        "innings_number": 1, "wicket_number": 1,
        "batsman1_name": b1, "batsman2_name": b2, "batsman1_id": None, "batsman2_id": None,
        "runs": 34, "balls": 25, "fours": 4, "sixes": 1,
        "batsman1_contribution": 20, "batsman1_balls": 14,
        "batsman2_contribution": 12, "batsman2_balls": 11,
        "start_over": 0.0, "end_over": 4.1667, "is_unbroken": False,
        "score_at_fall": 34, "fall_over": "4.1", "wicket_type": "Caught", "dismissed_slot": 2,
    }
    stand.update(over)
    return stand


def test_partnership_persists_detail_and_resolves_by_id(app, regular_user, test_team, test_team_2):
    """Partnerships go through the same id-preferred resolver as the
    scorecard and keep the 4s/6s, batter balls and fall-of-wicket fields."""
    from database.models import MatchPartnership
    with app.app_context():
        john = DBPlayer.query.filter_by(name="John Doe").first()
        bob = DBPlayer.query.filter_by(name="Bob Keeper").first()
        match = _make_match(regular_user.id)
        match.first_innings_batting_stats = {}
        match.first_innings_partnerships = [
            _stand("Renamed John", "Bob Keeper", batsman1_id=john.id),
            _stand("John Doe", "Bob Keeper", wicket_number=2, runs=9, balls=6, is_unbroken=True,
                   score_at_fall=None, fall_over=None, wicket_type=None, dismissed_slot=None),
        ]
        assert _archive(match) is not False

        rows = MatchPartnership.query.filter_by(match_id=match.match_data["match_id"]).order_by(
            MatchPartnership.wicket_number).all()
        assert [(r.batsman1_id, r.batsman2_id) for r in rows] == [(john.id, bob.id), (john.id, bob.id)]
        first, second = rows
        assert (first.fours, first.sixes, first.batsman1_balls, first.batsman2_balls) == (4, 1, 14, 11)
        assert (first.score_at_fall, first.fall_over, first.wicket_type, first.dismissed_slot) == (34, "4.1", "Caught", 2)
        assert first.is_unbroken is False and second.is_unbroken is True
        assert DBMatch.query.get(match.match_data["match_id"]).stats_incomplete is False


def test_unresolvable_partnership_is_loud(app, regular_user, test_team, test_team_2):
    from database.models import MatchPartnership
    with app.app_context():
        before = ExceptionLog.query.filter_by(exception_type="PartnershipPlayerLookupMiss").count()
        match = _make_match(regular_user.id)
        match.first_innings_batting_stats = {}
        match.first_innings_partnerships = [_stand("Nobody Real", "John Doe")]
        assert _archive(match) is not False

        assert MatchPartnership.query.filter_by(match_id=match.match_data["match_id"]).count() == 0
        assert DBMatch.query.get(match.match_data["match_id"]).stats_incomplete is True
        assert ExceptionLog.query.filter_by(exception_type="PartnershipPlayerLookupMiss").count() == before + 1
