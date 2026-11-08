"""Statistics must reflect persisted scorecards, even with stale caches."""
import csv
import io
from datetime import datetime

import pytest
from flask import template_rendered

from database import db
from database.models import (
    Match, MatchScorecard, Player, Team, Tournament, TournamentFixture,
    TournamentPlayerStatsCache,
)
from engine.stats_service import StatsService
from engine.tournament_engine import TournamentEngine
from scripts.dev_test_accounts import ACCOUNTS, logged_in_client, seed


@pytest.fixture
def statistics_data(app):
    seed(app)  # Shared QA identities, only in pytest's isolated database.
    uid = ACCOUNTS['user1']['email']
    team = Team(user_id=uid, name='Freshness XI', short_code='FRS')
    db.session.add(team)
    db.session.flush()
    player = Player(team_id=team.id, name='Freshness Player', role='All-Rounder')
    tournament = Tournament(user_id=uid, name='Freshness Cup', format_type='T20')
    db.session.add_all([player, tournament])
    db.session.flush()

    def add_match(number, fmt='T20', linked=True):
        mid = f'freshness-{number}'
        match = Match(id=mid, user_id=uid, home_team_id=team.id,
                      away_team_id=team.id, tournament_id=tournament.id,
                      match_format=fmt, date=datetime(2026, 1, 1))
        db.session.add(match)
        db.session.flush()
        if linked:
            db.session.add(TournamentFixture(
                tournament_id=tournament.id, home_team_id=team.id,
                away_team_id=team.id, match_id=mid, status='Completed'))
        db.session.add_all([
            MatchScorecard(match_id=mid, player_id=player.id, team_id=team.id,
                           innings_number=1, record_type='batting',
                           runs=30, balls=20, is_out=True, catches=1),
            MatchScorecard(match_id=mid, player_id=player.id, team_id=team.id,
                           innings_number=2, record_type='bowling',
                           balls_bowled=12, runs_conceded=10, wickets=2),
        ])
        db.session.commit()
        return match

    for number in range(2):
        add_match(number)
    TournamentEngine().rebuild_player_stats_cache(tournament.id, {player.id})
    db.session.commit()
    for number in range(2, 18):
        add_match(number)
    return uid, tournament, player, add_match


def test_stale_cache_does_not_hide_sixteen_matches(statistics_data):
    uid, tournament, player, _ = statistics_data
    cache = TournamentPlayerStatsCache.query.filter_by(player_id=player.id).one()
    assert cache.matches_played == 2
    service = StatsService()
    stats = service.get_tournament_stats(uid, tournament.id, 'T20')
    assert stats == service.get_overall_stats(uid, 'T20')
    assert stats['batting'][0]['matches'] == 18
    assert stats['batting'][0]['runs'] == 540
    assert stats['bowling'][0]['wickets'] == 36
    assert stats['fielding'][0]['catches'] == 18


def test_corrected_and_deleted_scorecards_are_immediately_visible(statistics_data):
    uid, tournament, player, _ = statistics_data
    service = StatsService()
    TournamentEngine().rebuild_player_stats_cache(tournament.id, {player.id})
    db.session.commit()
    card = MatchScorecard.query.filter_by(match_id='freshness-0', record_type='batting').one()
    card.runs = 100  # Same match count, different score: count-only validation fails.
    db.session.commit()
    assert service.get_tournament_stats(uid, tournament.id, 'T20')['batting'][0]['runs'] == 610
    MatchScorecard.query.filter_by(match_id='freshness-1').delete()
    db.session.commit()
    stats = service.get_tournament_stats(uid, tournament.id, 'T20')
    assert stats['batting'][0]['matches'] == 17
    assert stats['batting'][0]['runs'] == 580
    MatchScorecard.query.delete()
    db.session.commit()
    assert service.get_tournament_stats(uid, tournament.id, 'T20') == service._empty_stats()


def test_partial_cache_does_not_hide_players(statistics_data):
    uid, tournament, player, _ = statistics_data
    newcomer = Player(team_id=player.team_id, name='Uncached Player', role='Batsman')
    db.session.add(newcomer)
    db.session.flush()
    db.session.add(MatchScorecard(match_id='freshness-17', player_id=newcomer.id,
                                team_id=player.team_id, record_type='batting',
                                innings_number=1, runs=75, balls=40, is_out=False))
    db.session.commit()
    rows = StatsService().get_tournament_stats(uid, tournament.id, 'T20')['batting']
    assert {row['player_id'] for row in rows} == {player.id, newcomer.id}


def test_filters_and_super_overs_survive_stale_cache(statistics_data):
    uid, tournament, player, add_match = statistics_data
    add_match(18, fmt='ListA')
    db.session.add(MatchScorecard(match_id='freshness-0', player_id=player.id,
                                team_id=player.team_id, record_type='batting',
                                innings_number=3, runs=99, balls=6, is_super_over=True))
    db.session.commit()
    service = StatsService()
    stats = service.get_tournament_stats(uid, tournament.id, 'T20')
    assert stats['batting'][0]['runs'] == 540
    assert service.get_overall_stats(uid, 'T20') == stats
    assert not service.get_tournament_stats(ACCOUNTS['user2']['email'], tournament.id, 'T20')['batting']
    assert not service.get_tournament_stats(uid, tournament.id, 'FC')['batting']


def test_overall_includes_other_tournaments_and_individual_matches(statistics_data):
    uid, tournament, _, add_match = statistics_data
    friendly = add_match(18, linked=False)
    friendly.tournament_id = None
    other = Tournament(user_id=uid, name='Other Cup', format_type='T20')
    db.session.add(other)
    db.session.flush()
    match = add_match(19, linked=False)
    match.tournament_id = other.id
    db.session.commit()
    service = StatsService()
    assert service.get_tournament_stats(uid, tournament.id, 'T20')['batting'][0]['matches'] == 18
    assert service.get_overall_stats(uid, 'T20')['batting'][0]['matches'] == 20
    assert service.get_tournament_stats(uid, other.id, 'T20')['batting'][0]['matches'] == 1


def test_statistics_page_and_export_use_current_totals(app, statistics_data):
    uid, tournament, _, _ = statistics_data
    client = logged_in_client(app, 'user1')
    contexts = []
    def capture(sender, template, context, **kwargs):
        contexts.append(context)
    with template_rendered.connected_to(capture, app):
        response = client.get(f'/statistics?view=tournament&tournament_id={tournament.id}')
    assert response.status_code == 200
    assert contexts[-1]['batting_stats'][0]['matches'] == 18
    for view in ('overall', 'tournament'):
        response = client.get(f'/statistics/export/batting/csv?view={view}&tournament_id={tournament.id}')
        assert response.status_code == 200
        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        assert int(rows[0]['matches']) == 18
        assert int(rows[0]['runs']) == 540
