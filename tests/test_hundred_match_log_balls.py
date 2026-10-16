"""Hundred match log shows legal balls, not six-ball overs, under its "Balls" heading."""
import uuid
from datetime import datetime

import pytest
from database import db
from database.models import Match, MatchScorecard, Player
from engine.stats_service import StatsService


@pytest.mark.parametrize('fmt, expected', [('Hundred', '20'), ('T20', '3.2')])
def test_match_log_bowling_volume(app, regular_user, test_team, test_team_2, fmt, expected):
    bowler = Player.query.filter_by(team_id=test_team.id).first()
    bowler.profile_id = None
    match = Match(id=str(uuid.uuid4()), user_id=regular_user.id,
                  home_team_id=test_team.id, away_team_id=test_team_2.id,
                  match_format=fmt, date=datetime.utcnow())
    db.session.add(match)
    db.session.flush()
    db.session.add(MatchScorecard(match_id=match.id, player_id=bowler.id, team_id=test_team.id,
                                  innings_number=1, record_type='bowling', balls_bowled=20,
                                  runs_conceded=25, wickets=1))
    db.session.commit()

    profile = StatsService().get_player_profile(bowler.id, regular_user.id, match_format=fmt)
    entry = profile['match_log'][0]
    assert entry['bowl_overs'] == expected
    assert entry['bowling_innings'][0]['overs'] == expected
