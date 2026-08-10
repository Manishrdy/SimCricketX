"""
Regression: legacy/legacy-shaped MatchScorecard.overs values (a string
column) must never be compared numerically or passed to round() directly —
only balls_bowled (exact, Integer) should drive overs math.

Root cause (fixed): MatchScorecard.overs is a String(10) column. Several
read sites did `round(card.overs, 1)` or `(card.overs or 0) > 0`, which
raises TypeError once the value round-trips through the DB as a str. In
get_bowling_figures_leaderboard this was caught by a blanket
`except Exception` that silently returned an empty list for the whole
leaderboard — not just an understated economy figure, a fully broken
feature.

This test seeds MatchScorecard rows DIRECTLY (bypassing the now-fixed
archiver) with balls_bowled=0/None, so it genuinely exercises the
legacy-string-column code path regardless of how new rows are written.
"""
import os
import sys
import uuid
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db
from database.models import Match as DBMatch, MatchScorecard, Player as DBPlayer
from engine.stats_service import StatsService


def _seed_legacy_bowling_card(user_id, home_team, away_team, bowler):
    match_id = str(uuid.uuid4())
    db_match = DBMatch(
        id=match_id, user_id=user_id,
        home_team_id=home_team.id, away_team_id=away_team.id,
        result_description="TW won by 4 wickets", date=datetime.utcnow(),
        match_format="T20", overs_per_side=20,
    )
    db.session.add(db_match)
    db.session.flush()

    card = MatchScorecard(
        match_id=match_id, player_id=bowler.id, team_id=home_team.id,
        innings_number=2, record_type="bowling",
        overs="4.0", balls_bowled=0,  # legacy shape: string overs, no balls_bowled
        runs_conceded=28, wickets=1, maidens=0, wides=0, noballs=0,
    )
    db.session.add(card)
    db.session.commit()
    return match_id


def test_bowling_figures_leaderboard_survives_legacy_overs_column(app, regular_user, test_team, test_team_2):
    with app.app_context():
        bowler = DBPlayer.query.filter_by(name="Champion 1").first()
        _seed_legacy_bowling_card(regular_user.id, test_team, test_team_2, bowler)

        service = StatsService()
        figures = service.get_bowling_figures_leaderboard(regular_user.id, limit=10)

        assert figures != []
        row = figures[0]
        assert row["player"] == "Champion 1"
        assert row["overs"] == 0.0  # derived from balls_bowled=0, not the string column


def test_get_insights_survives_legacy_overs_column(app, regular_user, test_team, test_team_2):
    with app.app_context():
        bowler = DBPlayer.query.filter_by(name="Champion 1").first()
        _seed_legacy_bowling_card(regular_user.id, test_team, test_team_2, bowler)

        service = StatsService()
        insights = service.get_insights(regular_user.id)  # must not raise TypeError
        assert insights is not None


def test_get_overall_stats_survives_legacy_overs_column(app, regular_user, test_team, test_team_2):
    with app.app_context():
        bowler = DBPlayer.query.filter_by(name="Champion 1").first()
        _seed_legacy_bowling_card(regular_user.id, test_team, test_team_2, bowler)

        service = StatsService()
        stats = service.get_overall_stats(regular_user.id)  # must not raise TypeError
        assert stats is not None
        assert stats["bowling"] != []
