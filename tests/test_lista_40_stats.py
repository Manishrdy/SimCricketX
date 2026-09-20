"""Scheduled length filters are shared across all List A statistics views."""
import json
from datetime import datetime

import pytest
from database import db
from database.models import Match, MatchScorecard, Player, Tournament, MatchPartnership
from engine.stats_service import StatsService
from engine.tournament_engine import TournamentEngine


@pytest.fixture
def length_records(regular_user, test_team, test_team_2):
    players = Player.query.filter_by(team_id=test_team.id).limit(2).all()
    assert len(players) == 2
    for ident, length, runs in [('forty',40,40), ('fifty',50,50), ('legacy',None,60)]:
        # Fifty overs reduced by rain must never appear in the forty-over view.
        match = Match(id=ident, user_id=regular_user.id, match_format='ListA',
                      scheduled_overs=length, overs_per_side=40,
                      home_team_id=test_team.id, away_team_id=test_team_2.id,
                      home_team_score=runs, home_team_wickets=10, home_team_overs='15.0',
                      away_team_score=20, away_team_wickets=10, away_team_overs='12.0',
                      result_description=f'{test_team.name} won by {runs-20} runs', date=datetime(2026,9,20))
        db.session.add(match); db.session.flush()
        for p in players:
            db.session.add(MatchScorecard(match_id=ident, player_id=p.id, team_id=test_team.id,
                           record_type='batting', runs=runs, balls=30, is_out=True))
            db.session.add(MatchScorecard(match_id=ident, player_id=p.id, team_id=test_team.id,
                           record_type='bowling', balls_bowled=48, runs_conceded=20, wickets=2))
        db.session.add(MatchPartnership(match_id=ident, innings_number=1, wicket_number=1,
                       batsman1_id=players[0].id, batsman2_id=players[1].id, runs=runs, balls=30))
    db.session.add(MatchScorecard(match_id='forty', player_id=players[0].id, team_id=test_team.id,
                                  record_type='batting', runs=999, is_super_over=True))
    db.session.commit()
    return players


def test_combined_and_length_totals(length_records, regular_user, test_team, test_team_2):
    service = StatsService()
    totals = []
    for length in (None,40,50):
        stats = service.get_overall_stats(regular_user.id, 'ListA', scheduled_overs=length)
        totals.append(sum(row['runs'] for row in stats['batting']))
        scoped = StatsService(scheduled_overs=length)
        assert scoped.get_overall_stats(regular_user.id, 'ListA') == stats
        log = scoped.get_player_profile(length_records[0].id, regular_user.id, 'ListA')
        assert 'error' not in log
        assert log['batting']['runs'] == totals[-1] // 2
        h2h = scoped.get_head_to_head(regular_user.id, test_team.id, test_team_2.id, 'ListA')
        assert 'error' not in h2h
        compared = scoped.compare_players_cross_format(regular_user.id, [f'player:{p.id}' for p in length_records])
        assert compared['players'][0]['formats']['ListA']['batting']['runs'] == totals[-1] // 2
    assert totals == [300,80,220]
    assert totals[0] == totals[1] + totals[2]


def test_routes_and_exports_share_filter(authenticated_client, length_records, regular_user, test_team):
    for path in ['/statistics?match_format=ListA', '/api/partnerships?match_format=ListA',
                 f'/player/{length_records[0].id}?match_format=ListA',
                 f'/team-stats/{test_team.id}?match_format=ListA', '/my-matches?format=ListA']:
        response = authenticated_client.get(path + '&scheduled_overs=40')
        assert response.status_code == 200, (path, response.status_code)
    response = authenticated_client.get('/api/partnerships?match_format=ListA&scheduled_overs=40')
    assert [r['match_id'] for r in response.json['data']] == ['forty']
    assert authenticated_client.get('/statistics?match_format=ListA&scheduled_overs=41').status_code == 400
    import csv, io
    export = authenticated_client.get('/statistics/export/batting/csv?match_format=ListA&scheduled_overs=40')
    assert export.status_code == 200
    assert 'ListA_40' in export.headers['Content-Disposition']
    exported = list(csv.DictReader(io.StringIO(export.get_data(as_text=True))))
    assert sum(int(row["runs"]) for row in exported) == 80


def test_tournament_cache_mismatch_and_nrr(length_records, regular_user, test_team):
    tournament = Tournament(name='Forty', user_id=regular_user.id, format_type='ListA', scheduled_overs=40)
    db.session.add(tournament); db.session.flush()
    m = db.session.get(Match, 'forty'); m.tournament_id = tournament.id
    db.session.commit()
    service = StatsService()
    assert not service.get_tournament_stats(regular_user.id, tournament.id, 'ListA', scheduled_overs=50)['batting']
    assert service.get_tournament_stats(regular_user.id, tournament.id, 'ListA', scheduled_overs=40)['batting']
    assert TournamentEngine._get_nrr_overs('15.0', 10, m) == '40.0'
