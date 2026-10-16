"""Zero-legal-ball spells must retain contributions and undefined rates."""
import uuid
from datetime import datetime

import pytest
from database import db
from database.models import Match, MatchScorecard, Player, Tournament, TournamentFixture, TournamentPlayerStatsCache
from engine.format_catalog import SUPPORTED_FORMATS
from engine.stats_service import StatsService
from engine.tournament_engine import TournamentEngine
from engine import player_comparison


@pytest.mark.parametrize('fmt', SUPPORTED_FORMATS)
@pytest.mark.parametrize('legal_balls', [0, None])
@pytest.mark.parametrize('spell', ['wide', 'wide_wicket', 'noball', 'unused'])
def test_zero_ball_spells_across_statistics(app, regular_user, test_team, test_team_2, fmt, spell, legal_balls):
    bowler = Player.query.filter_by(team_id=test_team.id).first()
    other = Player.query.filter_by(team_id=test_team_2.id).first()
    # Profile-less fixture players permit isolating every match format.
    bowler.profile_id = None
    other.profile_id = None
    tournament = Tournament(name='Zero-ball confirmation', user_id=regular_user.id, mode='round_robin', format_type=fmt)
    db.session.add(tournament)
    db.session.flush()
    match = Match(id=str(uuid.uuid4()), user_id=regular_user.id,
                  home_team_id=test_team.id, away_team_id=test_team_2.id,
                  match_format=fmt, date=datetime.utcnow(), tournament_id=tournament.id)
    db.session.add(match)
    db.session.flush()
    fixture = TournamentFixture(tournament_id=tournament.id, match_id=match.id,
                               home_team_id=test_team.id, away_team_id=test_team_2.id,
                               round_number=1, stage='League', status='Completed')
    wickets = int(spell == 'wide_wicket')
    runs = int(spell != 'unused')
    card = MatchScorecard(match_id=match.id, player_id=bowler.id, team_id=test_team.id,
                         innings_number=2, record_type='bowling', balls_bowled=0, overs='0.0',
                         runs_conceded=runs, wickets=wickets,
                         wides=int(spell.startswith('wide')), noballs=int(spell == 'noball'))
    db.session.add_all([fixture, card])
    db.session.commit()
    # Legacy scorecards can have NULL legal-ball counters. UPDATE avoids
    # the column default replacing None during INSERT.
    card.balls_bowled = legal_balls
    db.session.commit()
    service = StatsService()
    engine = TournamentEngine()
    comparison = player_comparison
    overall = service.get_overall_stats(regular_user.id, match_format=fmt)['bowling']
    profile = service.get_player_profile(bowler.id, regular_user.id, match_format=fmt)['bowling']
    detailed = service._get_player_detailed_stats(bowler.id, regular_user.id, match_format=fmt)['bowling']
    ids = [next(p['id'] for p in comparison.identities(regular_user.id) if pid in p['player_ids']) for pid in (bowler.id, other.id)]
    cross = comparison.compare(service, regular_user.id, ids)['players'][0]['formats'][fmt]['bowling']
    engine.rebuild_player_stats_cache(tournament.id, {bowler.id})
    cache = TournamentPlayerStatsCache.query.filter_by(tournament_id=tournament.id, player_id=bowler.id).one()
    active = spell != 'unused'
    if active:
        assert len(overall) == 1
        for row in (overall[0], profile, detailed, cross):
            assert (row['innings'], row['balls'], row['runs'], row['wickets']) == (1, 0, runs, wickets)
            assert row['economy'] is None
            assert row['average'] == (1.0 if wickets else None)
            assert row['strike_rate'] == (0.0 if wickets else None)
        assert (cache.innings_bowled, cache.runs_conceded, cache.wickets_taken) == (1, runs, wickets)
        assert cache.bowling_economy is None
        assert cache.bowling_average == (1.0 if wickets else None)
        assert cache.bowling_strike_rate == (0.0 if wickets else None)
        cached = service._try_cache_tournament_stats(tournament.id, regular_user.id, match_format=fmt)
        if fmt == 'Hundred':
            assert cached is None  # Hundred deliberately bypasses this cache reader.
        else:
            row = cached['bowling'][0]
            assert (row['innings'], row['runs'], row['wickets']) == (1, runs, wickets)
            assert row['economy'] is None
            assert row['average'] == (1.0 if wickets else None)
            assert row['strike_rate'] == (0.0 if wickets else None)
        # Verify UPDATE/rebuild as well as the initial INSERT above.
        engine.rebuild_player_stats_cache(tournament.id, {bowler.id})
        db.session.flush()
        db.session.expire(cache)
        assert cache.bowling_economy is None
        assert cache.bowling_average == (1.0 if wickets else None)
        assert cache.bowling_strike_rate == (0.0 if wickets else None)
    else:
        assert overall == []
        assert profile == detailed == cross == {}
        assert cache.innings_bowled == 0
