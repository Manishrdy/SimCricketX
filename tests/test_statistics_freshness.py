"""Statistics are aggregated from persisted scorecards, never from a cache.

The original bug: a per-tournament player cache was rebuilt before the match's
scorecards were archived, so it froze, and the Statistics page served it
("18 matches played, 2 shown"). The cache is retired; these tests pin the
behaviour that replaced it.
"""
import sqlite3
import csv
import io
from datetime import datetime

import pytest
from flask import template_rendered

from database import db
from database.models import (
    Match, MatchScorecard, Player, Team, Tournament, TournamentFixture,
)
from engine.stats_service import StatsService
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

    for number in range(18):
        add_match(number)
    return uid, tournament, player, add_match


def test_every_completed_match_is_counted(statistics_data):
    uid, tournament, player, _ = statistics_data
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
    card = MatchScorecard.query.filter_by(match_id='freshness-0', record_type='batting').one()
    card.runs = 100  # Same match count, different score.
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


def test_players_who_join_late_are_listed(statistics_data):
    uid, tournament, player, _ = statistics_data
    newcomer = Player(team_id=player.team_id, name='Late Player', role='Batsman')
    db.session.add(newcomer)
    db.session.flush()
    db.session.add(MatchScorecard(match_id='freshness-17', player_id=newcomer.id,
                                team_id=player.team_id, record_type='batting',
                                innings_number=1, runs=75, balls=40, is_out=False))
    db.session.commit()
    rows = StatsService().get_tournament_stats(uid, tournament.id, 'T20')['batting']
    assert {row['player_id'] for row in rows} == {player.id, newcomer.id}


def test_format_owner_and_super_over_filters(statistics_data):
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


def _completion_handler(app):
    """The tournament completion handler is a closure inside create_app()."""
    import inspect
    from types import FunctionType

    def find(function, name, seen):
        if id(function) in seen:
            return None
        seen.add(id(function))
        if function.__name__ == name:
            return function
        for value in inspect.getclosurevars(function).nonlocals.values():
            if isinstance(value, FunctionType):
                found = find(value, name, seen)
                if found:
                    return found

    handler = find(app.view_functions['next_ball'], '_handle_tournament_match_completion', set())
    assert handler, 'completion handler not reachable from next_ball'
    return handler


def test_live_completion_is_visible_in_statistics_immediately(app, monkeypatch):
    """Drives the real completion handler: every match must show up in the
    tournament statistics as soon as its scorecards are archived, with no
    intermediate cache to fall behind."""
    from types import SimpleNamespace
    from flask_login import login_user
    import app as app_module
    from database.models import User

    seed(app)
    uid = ACCOUNTS['user1']['email']
    home = Team(user_id=uid, name='Cache Home', short_code='CHH')
    away = Team(user_id=uid, name='Cache Away', short_code='CHA')
    db.session.add_all([home, away])
    db.session.flush()
    batter = Player(team_id=home.id, name='Cache Batter', role='Batsman')
    bowler = Player(team_id=away.id, name='Cache Bowler', role='Bowler')
    # FC skips the limited-overs innings maths, so a stub match is enough.
    tournament = Tournament(user_id=uid, name='Cache Cup', format_type='FC', mode='round_robin')
    db.session.add_all([batter, bowler, tournament])
    db.session.flush()
    fixtures = [
        TournamentFixture(tournament_id=tournament.id, home_team_id=home.id,
                          away_team_id=away.id, stage='league', status='Scheduled')
        for _ in range(3)
    ]
    db.session.add_all(fixtures)
    db.session.commit()
    ids = SimpleNamespace(home=home.id, batter=batter.id, away=away.id, bowler=bowler.id,
                          tournament=tournament.id, fixtures=[f.id for f in fixtures])

    class FakeArchiver:
        """Stands in for MatchArchiver: persists scorecards and commits."""
        def __init__(self, data, match):
            self.match_id = match.match_id

        def _save_to_database(self):
            db.session.add_all([
                MatchScorecard(match_id=self.match_id, player_id=ids.batter, team_id=ids.home,
                               innings_number=1, record_type='batting', runs=40, balls=60, is_out=True),
                MatchScorecard(match_id=self.match_id, player_id=ids.bowler, team_id=ids.away,
                               innings_number=1, record_type='bowling', balls_bowled=60,
                               runs_conceded=40, wickets=1),
            ])
            db.session.commit()

    monkeypatch.setattr(app_module, 'MatchArchiver', FakeArchiver)
    handler = _completion_handler(app)

    for number, fixture_id in enumerate(ids.fixtures, start=1):
        match_id = f'cache-live-{number}'
        live = SimpleNamespace(
            match_id=match_id, winner_is_home=True, match_status='completed',
            data={'tournament_id': ids.tournament, 'fixture_id': fixture_id,
                  'match_format': 'FC', 'current_state': 'live'},
        )
        with app.test_request_context():
            login_user(db.session.get(User, uid))
            handler(live, match_id, {'result': 'Cache Home won', 'scorecard_data': {'x': 1}}, app.logger)
        db.session.rollback()
        assert db.session.get(TournamentFixture, fixture_id).status == 'Completed'
        stats = StatsService().get_tournament_stats(uid, ids.tournament, 'FC')
        assert stats['batting'][0]['matches'] == number, 'statistics lag the completed match'
        assert stats['bowling'][0]['matches'] == number

    assert (stats['batting'][0]['runs'], stats['batting'][0]['innings']) == (120, 3)
    assert stats['bowling'][0]['wickets'] == 3


LEGACY_CACHE_DDL = '''
    CREATE TABLE tournament_player_stats_cache (
        id INTEGER PRIMARY KEY,
        tournament_id INTEGER NOT NULL REFERENCES tournaments (id),
        player_id INTEGER NOT NULL REFERENCES players (id),
        team_id INTEGER NOT NULL REFERENCES teams (id),
        matches_played INTEGER DEFAULT 0
    );
    CREATE INDEX ix_tournament_player_cache_tournament_id
        ON tournament_player_stats_cache (tournament_id);
'''


def test_cache_model_is_gone():
    import database.models as models
    assert not hasattr(models, 'TournamentPlayerStatsCache')
    assert 'tournament_player_stats_cache' not in db.metadata.tables


def test_boot_step_empties_retired_table_so_deletes_are_not_blocked(app, statistics_data):
    """The table's foreign keys outlive the model. Leftover rows would make
    deleting the tournament fail until the table is dropped, so boot clears them."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from migrations.drop_tournament_player_stats_cache import run_migration

    uid, tournament, player, _ = statistics_data
    tournament_id = tournament.id
    with db.engine.begin() as conn:
        for statement in LEGACY_CACHE_DDL.split(';'):
            if statement.strip():
                conn.execute(text(statement))
        conn.execute(text(
            'INSERT INTO tournament_player_stats_cache (tournament_id, player_id, team_id, matches_played) '
            'VALUES (:t, :p, :team, 2)'), {'t': tournament_id, 'p': player.id, 'team': player.team_id})
    Match.query.filter_by(tournament_id=tournament_id).update({'tournament_id': None})
    db.session.commit()

    db.session.delete(db.session.get(Tournament, tournament_id))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()

    assert run_migration(db, app) == 1
    assert run_migration(db, app) == 0  # safe to repeat on every boot
    db.session.delete(db.session.get(Tournament, tournament_id))
    db.session.commit()
    assert db.session.get(Tournament, tournament_id) is None
    with db.engine.begin() as conn:
        conn.execute(text('DROP TABLE tournament_player_stats_cache'))
    assert run_migration(db, app) == 0  # and a no-op once the table is gone


def test_drop_migration_dry_run_apply_and_repeat(tmp_path):
    from migrations.drop_tournament_player_stats_cache import migrate

    path = tmp_path / 'database.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE tournaments (id INTEGER PRIMARY KEY);
            CREATE TABLE players (id INTEGER PRIMARY KEY);
            CREATE TABLE teams (id INTEGER PRIMARY KEY);
            CREATE TABLE match_scorecards (id INTEGER PRIMARY KEY, runs INTEGER);
            INSERT INTO tournaments VALUES (1);
            INSERT INTO players VALUES (1);
            INSERT INTO teams VALUES (1);
            INSERT INTO match_scorecards VALUES (1, 77);
        ''' + LEGACY_CACHE_DDL + '''
            INSERT INTO tournament_player_stats_cache (tournament_id, player_id, team_id) VALUES (1, 1, 1);
        ''')
    original = path.read_bytes()
    assert migrate(path) == 1
    assert path.read_bytes() == original  # dry run writes nothing
    assert migrate(path, apply=True) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('tournament_player_stats_cache', 'ix_tournament_player_cache_tournament_id')"
        ).fetchall() == []
        assert conn.execute('SELECT runs FROM match_scorecards').fetchall() == [(77,)]
    assert migrate(path, apply=True) == 0


def test_drop_migration_refuses_under_a_stale_server(tmp_path, monkeypatch):
    import migrations.drop_tournament_player_stats_cache as migration
    from utils.runtime_registry import StaleServerError

    path = tmp_path / 'database.db'
    with sqlite3.connect(path) as conn:
        conn.executescript(
            'CREATE TABLE tournaments (id INTEGER PRIMARY KEY);'
            'CREATE TABLE players (id INTEGER PRIMARY KEY);'
            'CREATE TABLE teams (id INTEGER PRIMARY KEY);' + LEGACY_CACHE_DDL)

    def stale(db_path, action):
        raise StaleServerError('old worker still running')

    monkeypatch.setattr(migration, 'assert_no_stale_holders', stale)
    with pytest.raises(StaleServerError):
        migration.migrate(path, apply=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='tournament_player_stats_cache'").fetchone()
