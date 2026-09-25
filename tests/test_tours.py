"""Tour creation, scheduling, statistics and owner route regressions."""
from datetime import datetime
import uuid
import json
import pytest
from database import db
from database.models import Tour, Tournament, TournamentFixture, Match, MatchScorecard, Player, TeamProfile
from engine.tour_engine import (create_tour, ordered_series, fixture_block_reason,
                               reorder_series, refresh_tour, tour_summary, series_result)
from engine.tournament_engine import TournamentEngine
from engine.format_catalog import TOUR_FORMATS


def add_ready_profiles(team):
    legacy = Player.query.filter_by(team_id=team.id, profile_id=None).all()
    for fmt in ('T20', 'ListA', 'FC'):
        profile = TeamProfile(team_id=team.id, format_type=fmt)
        db.session.add(profile)
        db.session.flush()
        players = legacy if fmt == 'T20' else []
        if players:
            for index, player in enumerate(players):
                player.profile_id = profile.id
                player.role = 'Wicketkeeper' if index == 0 else 'All-rounder'
                player.is_captain = index == 0
                player.is_wicketkeeper = index == 0
        else:
            for index in range(11):
                db.session.add(Player(team_id=team.id, profile_id=profile.id, name=f'{fmt} Player {index}',
                                      role='Wicketkeeper' if index == 0 else 'All-rounder',
                                      is_captain=index == 0, is_wicketkeeper=index == 0,
                                      batting_rating=50, bowling_rating=50, fielding_rating=50))
    db.session.commit()
    db.session.expire(team, ['profiles', 'players'])


@pytest.fixture(autouse=True)
def ready_tour_teams(test_team, test_team_2):
    for team in (test_team, test_team_2):
        add_ready_profiles(team)


def confirmed_payload(data):
    data = dict(data)
    data['confirm_schedule'] = 'yes'
    data['schedule_confirmation'] = json.dumps({
        'name': data.get('name', '').strip(), 'host': str(data.get('host_team_id', '')),
        'visitor': str(data.get('visiting_team_id', '')),
        'counts': {f: str(data.get('count_' + f, '0')) for f in ('FC', 'ListA', 'T20')},
        'order': data.get('format_order', []),
    })
    return data


@pytest.fixture
def make_tour(regular_user, test_team, test_team_2):
    def make(counts=None, order=None, token=None):
        return create_tour('Tour test', regular_user.id, test_team.id, test_team_2.id,
                           counts or {'FC': 3, 'ListA': 3, 'T20': 0},
                           [fmt for fmt in (order or ['FC', 'ListA', 'T20'])
                            if str((counts or {'FC': 3, 'ListA': 3}).get(fmt, 0)).isdigit()
                            and int((counts or {'FC': 3, 'ListA': 3}).get(fmt, 0)) > 0], token)
    return make


def test_zero_exclusion_and_host(make_tour, test_team):
    tour = make_tour()
    assert [s.format_type for s in tour.series] == ['FC', 'ListA']
    assert [len(s.fixtures) for s in tour.series] == [3, 3]
    assert TournamentFixture.query.count() == 6
    assert all(f.home_team_id == test_team.id for s in tour.series for f in s.fixtures)


@pytest.mark.parametrize('counts', [{'FC': 0}, {'FC': -1}, {'FC': '1.5'}, {'FC': ''}, {'FC': 'abc'}])
def test_invalid_counts(make_tour, counts):
    with pytest.raises(ValueError):
        make_tour(counts)
    assert Tour.query.count() == Tournament.query.count() == 0


def test_single_format_and_retry(make_tour):
    tour = make_tour({'T20': 8}, token='retry')
    assert len(tour.series) == 1 and len(tour.series[0].fixtures) == 8
    assert make_tour({'T20': 8}, token='retry').id == tour.id
    assert Tour.query.count() == 1 and TournamentFixture.query.count() == 8


def test_atomic_rollback(make_tour, monkeypatch):
    original = TournamentEngine.create_tournament
    calls = []
    def fail_second(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('injected failure')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(TournamentEngine, 'create_tournament', fail_second)
    with pytest.raises(RuntimeError):
        make_tour()
    assert Tour.query.count() == Tournament.query.count() == TournamentFixture.query.count() == 0


def test_order_completion_and_reopen(make_tour):
    tour = make_tour()
    first, second = ordered_series(tour)
    assert fixture_block_reason(second.fixtures[0])
    first.tour_started_at = datetime.utcnow()
    with pytest.raises(ValueError):
        reorder_series(tour, [second.id, first.id])
    for fixture in first.fixtures:
        fixture.status = 'Completed'
    refresh_tour(tour)
    assert not fixture_block_reason(second.fixtures[0])
    for fixture in second.fixtures:
        fixture.status = 'Completed'
    refresh_tour(tour)
    assert tour.status == 'Completed'
    first.fixtures[0].status = 'Scheduled'
    second.fixtures[0].status = 'Scheduled'
    refresh_tour(tour)
    assert tour.status == 'Active'
    assert fixture_block_reason(second.fixtures[0])


def test_reorder_unstarted(make_tour):
    tour = make_tour()
    first, second = ordered_series(tour)
    reorder_series(tour, [second.id, first.id])
    assert ordered_series(tour) == [second, first]
    assert fixture_block_reason(first.fixtures[0])
    with pytest.raises(ValueError):
        reorder_series(tour, [first.id, first.id])


def test_clinch_does_not_skip_and_drawn_series(make_tour, test_team):
    tour = make_tour()
    first, second = tour.series
    for fixture in first.fixtures[:2]:
        fixture.status = 'Completed'
        fixture.winner_team_id = test_team.id
    refresh_tour(tour)
    assert first.status == 'Active'
    assert fixture_block_reason(second.fixtures[0])
    for fixture in first.fixtures:
        fixture.status = 'Completed'
        fixture.winner_team_id = None
    assert series_result(first, tour)['result'] == 'Series drawn'


def test_aggregate_fc_innings_and_super_over(make_tour, test_team, regular_user):
    tour = make_tour({'FC': 1})
    fixture = tour.series[0].fixtures[0]
    player = Player.query.filter_by(team_id=test_team.id).first()
    match = Match(id=str(uuid.uuid4()), user_id=regular_user.id, tournament_id=tour.series[0].id,
                  home_team_id=tour.host_team_id, away_team_id=tour.visiting_team_id,
                  home_team_score=100, away_team_score=90, home_team_score_innings2=50,
                  away_team_score_innings2=40, home_team_wickets=10, away_team_wickets=10,
                  home_team_wickets_innings2=2, away_team_wickets_innings2=10,
                  match_format='FC', motm_player_id=player.id)
    db.session.add(match)
    fixture.match = match
    fixture.status = 'Completed'
    for innings, runs, super_over in [(1, 20, False), (3, 30, False), (3, 99, True)]:
        db.session.add(MatchScorecard(match=match, player_id=player.id, team_id=test_team.id,
                                     innings_number=innings, runs=runs, record_type='batting', is_super_over=super_over))
    db.session.commit()
    summary = tour_summary(tour)
    assert summary['runs'] == 280 and summary['wickets'] == 32
    assert summary['batters'][0][1] == 50
    assert summary['awards'][0][1] == 1
    assert tour_summary(tour, 'T20')['played'] == 0


def test_create_pages_and_zero_section(authenticated_client, test_team, test_team_2):
    page = authenticated_client.get('/tournaments/tours/create')
    assert page.status_code == 200 and page.data.count(b'value="0"') == len(TOUR_FORMATS)
    response = authenticated_client.post('/tournaments/tours/create', data=confirmed_payload({
        'name': 'India tour', 'host_team_id': test_team.id, 'visiting_team_id': test_team_2.id,
        'count_FC': '3', 'count_ListA': '3', 'count_T20': '0',
        'format_order': ['ListA', 'FC'], 'creation_token': 'route-retry'}))
    assert response.status_code == 302
    page = authenticated_client.get(response.location)
    assert page.status_code == 200 and b'T20' not in page.data
    assert 'List A · 50 overs series' in page.get_data(as_text=True)
    assert authenticated_client.get(response.location + '?format=T20').status_code == 404
    assert b'India tour' in authenticated_client.get('/tournaments').data


def test_direct_start_blocked(authenticated_client, make_tour, test_team, test_team_2):
    tour = make_tour()
    fixture = tour.series[1].fixtures[0]
    page = authenticated_client.get(f'/match/setup?fixture_id={fixture.id}')
    assert page.status_code == 302 and '/tournaments/tours/' in page.location
    response = authenticated_client.post('/match/setup', json={
        'team_home': test_team.id, 'team_away': test_team_2.id,
        'fixture_id': fixture.id, 'tournament_id': fixture.tournament_id})
    assert response.status_code == 409
    response = authenticated_client.post('/match/setup', json={
        'team_home': test_team.id, 'team_away': test_team_2.id,
        'tournament_id': fixture.tournament_id})
    assert response.status_code == 400
    child = authenticated_client.get(f'/tournaments/{fixture.tournament_id}')
    assert b'Awaiting Previous Results' in child.data


def test_owner_and_delete(authenticated_client, make_tour):
    tour = make_tour()
    tour_id = tour.id
    series_id = tour.series[0].id
    authenticated_client.post(f'/tournaments/{series_id}/delete')
    assert db.session.get(Tournament, series_id)
    authenticated_client.post(f'/tournaments/tours/{tour_id}/rename', data={'name': 'Renamed'})
    assert tour.name == 'Renamed'
    authenticated_client.post(f'/tournaments/tours/{tour_id}/delete')
    assert db.session.get(Tour, tour_id) is None
    assert Tournament.query.count() == TournamentFixture.query.count() == 0


def test_unauthorized(authenticated_client, make_tour, admin_user):
    tour = make_tour()
    tour.user_id = admin_user.id
    db.session.commit()
    for suffix in ['', '/rename', '/reorder', '/delete']:
        path = f'/tournaments/tours/{tour.id}{suffix}'
        response = authenticated_client.post(path) if suffix else authenticated_client.get(path)
        assert response.status_code == 404


@pytest.mark.parametrize('action', ['resimulate', 'delete'])
def test_match_cleanup_reverses_once(authenticated_client, make_tour, test_team, regular_user, action):
    tour = make_tour({'FC': 1})
    series = tour.series[0]
    fixture = series.fixtures[0]
    player = Player.query.filter_by(team_id=test_team.id).first()
    player.matches_played = 2
    player.total_runs = 80
    match = Match(id=str(uuid.uuid4()), user_id=regular_user.id, tournament_id=series.id,
                  home_team_id=tour.host_team_id, away_team_id=tour.visiting_team_id,
                  winner_team_id=tour.host_team_id, match_format='FC')
    db.session.add(match)
    fixture.match = match
    fixture.status = 'Completed'
    db.session.add(MatchScorecard(match=match, player_id=player.id, team_id=player.team_id,
                                 record_type='batting', runs=60, balls=40, is_out=True))
    refresh_tour(tour)
    db.session.commit()
    assert tour.status == 'Completed'
    # Viewing aggregates must never modify the career totals.
    tour_summary(tour)
    tour_summary(tour)
    assert player.total_runs == 80
    if action == 'resimulate':
        response = authenticated_client.post(f'/fixture/{fixture.id}/resimulate')
        assert response.status_code == 302
        assert tour.status == 'Active'
        assert tour_summary(tour)['played'] == 0
        assert series.tour_started_at is not None
    else:
        authenticated_client.post(f'/tournaments/tours/{tour.id}/delete')
        assert Tour.query.count() == 0
    assert Match.query.count() == MatchScorecard.query.count() == 0
    assert player.total_runs == 20


def test_completion_hook(make_tour):
    tour = make_tour({'T20': 1})
    series = tour.series[0]
    series.fixtures[0].status = 'Completed'
    TournamentEngine()._check_tournament_completion(series.id)
    db.session.commit()
    assert tour.status == 'Completed'
    assert series.tour_started_at


def test_route_reorder_and_validation(authenticated_client, make_tour, test_team, test_team_2):
    tour = make_tour()
    first, second = ordered_series(tour)
    response = authenticated_client.post(f'/tournaments/tours/{tour.id}/reorder',
                                          data={'series_ids': [second.id, first.id]}, follow_redirects=True)
    assert response.status_code == 200
    assert ordered_series(tour) == [second, first]
    response = authenticated_client.post('/tournaments/tours/create', data=confirmed_payload({
        'name': 'Empty tour', 'host_team_id': test_team.id, 'visiting_team_id': test_team_2.id,
        'count_FC': '0', 'count_ListA': '0', 'count_T20': '0', 'format_order': []}))
    assert b'positive match count' in response.data
    assert Tour.query.count() == 1


def test_successful_start_fixes_series_position(authenticated_client, make_tour, test_team, test_team_2):
    tour = make_tour({'T20': 1, 'ListA': 1}, order=['T20', 'ListA', 'FC'])
    for team in (test_team, test_team_2):
        players = Player.query.filter_by(team_id=team.id).all()
        for index in range(len(players), 11):
            db.session.add(Player(team_id=team.id, name=f'Extra {team.id} {index}', role='Bowler',
                                  batting_rating=50, bowling_rating=50, fielding_rating=50))
    db.session.commit()
    first, second = ordered_series(tour)
    response = authenticated_client.post('/match/setup', json={
        'team_home': test_team.id, 'team_away': test_team_2.id,
        'fixture_id': first.fixtures[0].id, 'match_format': 'T20', 'simulation_mode': 'auto'})
    assert response.status_code == 200, response.get_json()
    assert first.tour_started_at
    with pytest.raises(ValueError):
        reorder_series(tour, [second.id, first.id])


def test_missing_format_rejected_without_partial_tour(make_tour, test_team_2):
    for profile in list(test_team_2.profiles):
        if profile.format_type != 'FC':
            db.session.delete(profile)
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(ValueError, match='List A.*Squad not created'):
        make_tour({'FC': 5, 'ListA': 5, 'T20': 5})
    assert Tour.query.count() == Tournament.query.count() == 0
    tour = make_tour({'FC': 5})
    assert len(tour.series) == 1


@pytest.mark.parametrize('problem', ['empty', 'short', 'captain', 'keeper', 'bowlers', 'draft'])
def test_incomplete_profile_rejected(make_tour, test_team, problem):
    profile = next(p for p in test_team.profiles if p.format_type == 'FC')
    if problem == 'empty':
        for player in list(profile.players):
            db.session.delete(player)
    elif problem == 'short':
        db.session.delete(profile.players[-1])
    elif problem == 'captain':
        for player in profile.players:
            player.is_captain = False
    elif problem == 'keeper':
        for player in profile.players:
            player.is_wicketkeeper = False
    elif problem == 'bowlers':
        for player in profile.players[1:]:
            player.role = 'Batsman'
    else:
        test_team.is_draft = True
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(ValueError):
        make_tour({'FC': 2})
    assert Tour.query.count() == 0


def test_profiles_rechecked_at_match_start(authenticated_client, make_tour, test_team, test_team_2):
    tour = make_tour({'FC': 1})
    profile = next(p for p in test_team_2.profiles if p.format_type == 'FC')
    db.session.delete(profile)
    db.session.commit()
    db.session.expire_all()
    fixture = tour.series[0].fixtures[0]
    response = authenticated_client.post('/match/setup', json={
        'team_home': test_team.id, 'team_away': test_team_2.id,
        'fixture_id': fixture.id, 'match_format': 'FC'})
    assert response.status_code == 409
    assert 'Squad not created' in response.get_json()['error']


def test_confirmation_required_and_stale_rejected(authenticated_client, test_team, test_team_2):
    payload = {'name': 'Confirmation test', 'host_team_id': test_team.id,
               'visiting_team_id': test_team_2.id, 'count_FC': '2', 'format_order': ['FC']}
    response = authenticated_client.post('/tournaments/tours/create', data=payload)
    assert b'Review and confirm' in response.data
    confirmed = confirmed_payload(payload)
    confirmed['count_FC'] = '3'
    response = authenticated_client.post('/tournaments/tours/create', data=confirmed)
    assert b'Review and confirm' in response.data
    assert Tour.query.count() == 0
    response = authenticated_client.post('/tournaments/tours/create', data=confirmed_payload(payload))
    assert response.status_code == 302
    assert Tour.query.count() == 1


def test_forged_confirmation_cannot_bypass_squad_validation(authenticated_client, test_team, test_team_2):
    profile = next(p for p in test_team_2.profiles if p.format_type == 'ListA')
    db.session.delete(profile)
    db.session.commit()
    response = authenticated_client.post('/tournaments/tours/create', data=confirmed_payload({
        'name': 'Invalid tour', 'host_team_id': test_team.id, 'visiting_team_id': test_team_2.id,
        'count_FC': '5', 'count_ListA': '5', 'format_order': ['FC', 'ListA']}))
    assert b'Squad not created' in response.data
    assert Tour.query.count() == Tournament.query.count() == 0


def test_order_must_match_included_formats(regular_user, test_team, test_team_2):
    for order in ([], ['FC', 'FC'], ['FC', 'T20']):
        with pytest.raises(ValueError, match='included format'):
            create_tour('Invalid order', regular_user.id, test_team.id, test_team_2.id, {'FC': 2}, order)


def test_legacy_players_do_not_replace_a_missing_format(make_tour, test_team):
    profile = next(p for p in test_team.profiles if p.format_type == 'FC')
    db.session.delete(profile)
    db.session.commit()
    for index in range(11):
        db.session.add(Player(team_id=test_team.id, profile_id=None, name=f'Legacy {index}',
                              role='Wicketkeeper' if index == 0 else 'All-rounder',
                              is_captain=index == 0, is_wicketkeeper=index == 0))
    db.session.commit()
    db.session.expire_all()
    assert Player.query.filter_by(team_id=test_team.id, profile_id=None).count() == 11
    with pytest.raises(ValueError, match='Squad not created'):
        make_tour({'FC': 1})


def test_same_team_cannot_create_tour(authenticated_client, test_team):
    response = authenticated_client.post('/tournaments/tours/create', data=confirmed_payload({
        'name': 'Invalid same-team tour', 'host_team_id': test_team.id,
        'visiting_team_id': test_team.id, 'count_T20': '3', 'format_order': ['T20']}))
    assert b'Choose two different teams' in response.data
    assert Tour.query.count() == Tournament.query.count() == 0
