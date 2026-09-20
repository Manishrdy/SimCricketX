"""Public T10 integration: squads, setup, competition identity and rendering."""
import json
import os
import re
import subprocess
from pathlib import Path
from datetime import datetime

import pytest
from database import db
from database.models import TeamProfile, Player, Match, MatchScorecard, Tournament, TournamentFixture
from engine.tour_engine import create_tour
from engine.stats_service import StatsService
from engine.tournament_engine import TournamentEngine
from tests.test_tours import add_ready_profiles


@pytest.fixture
def teams(test_team, test_team_2):
    for team in (test_team, test_team_2):
        add_ready_profiles(team)
    return test_team, test_team_2


def seed(client, teams):
    for team in teams:
        response = client.post(f'/api/team/{team.id}/squad/T10/copy-t20')
        assert response.status_code == 201, response.json
    db.session.expire_all()


def test_copy_isolated_zero_stats_no_overwrite(authenticated_client, teams):
    from database.models import MasterPlayer
    source = TeamProfile.query.filter_by(team_id=teams[0].id, format_type='T20').one()
    pool_player = MasterPlayer(name='T10 source identity')
    db.session.add(pool_player); db.session.flush()
    source.players[0].master_player_id = pool_player.id
    source.players[0].technique_rating = 62
    source.players[0].total_runs = 800
    source.players[0].batting_rating = 0
    db.session.commit()
    page = authenticated_client.get(f'/team/{teams[0].id}/squad/T10')
    assert page.status_code == 200 and b'Copy T20 squad' in page.data
    assert TeamProfile.query.filter_by(team_id=teams[0].id, format_type='T10').count() == 0
    seed(authenticated_client, teams)
    target = TeamProfile.query.filter_by(team_id=teams[0].id, format_type='T10').one()
    copied = next(p for p in target.players if p.name == source.players[0].name)
    assert copied.id != source.players[0].id
    assert copied.total_runs == copied.matches_played == copied.batting_rating == 0
    assert copied.master_player_id == pool_player.id and copied.technique_rating == 62
    copied.batting_rating = 99; db.session.commit()
    assert source.players[0].batting_rating == 0
    response = authenticated_client.post(f'/api/team/{teams[0].id}/squad/T10/copy-t20')
    assert response.status_code == 409
    assert copied.batting_rating == 99


def test_setup_and_fixture_identity(authenticated_client, teams, regular_user):
    seed(authenticated_client, teams)
    data = dict(team_home=teams[0].id, team_away=teams[1].id, match_format='T10', pitch='Hard', stadium='Test Ground', toss='Heads', toss_winner=teams[0].short_code, toss_decision='Bat')
    bad = authenticated_client.post('/match/setup', json=data | {'scheduled_overs':20})
    assert bad.status_code == 400
    response = authenticated_client.post('/match/setup', json=data)
    assert response.status_code == 200, response.json
    mid = response.json['match_id']
    records = [json.loads(p.read_text()) for p in Path('data/matches').glob('*.json')]
    saved = next(r for r in records if r.get('match_id') == mid)
    assert saved['match_format'] == 'T10' and saved['scheduled_overs'] == saved['overs'] == 10
    from engine.match import Match as EngineMatch
    match = EngineMatch(saved)
    assert match.fmt.overs == 10
    assert match.next_ball().get('error') is None
    live = authenticated_client.get(f'/match/{mid}/live-state?delivery=1')
    assert live.status_code == 200, live.json
    live = authenticated_client.get(f'/match/{mid}/live-state')
    assert live.status_code == 200, live.json
    assert live.json['total_overs'] == live.json['scheduled_overs'] == 10
    assert live.json['phase_name'] == 'Powerplay'
    assert live.json['bowling_eligibility']['selectable']
    assert len(live.json['bowling_eligibility']['options']) >= 5
    tournament = Tournament(name='Ten Cup', user_id=regular_user.id, format_type='T10', scheduled_overs=10)
    db.session.add(tournament); db.session.flush()
    fixture = TournamentFixture(tournament_id=tournament.id, home_team_id=teams[0].id,
        away_team_id=teams[1].id, round_number=1, stage='league', status='Scheduled')
    db.session.add(fixture); db.session.commit()
    response = authenticated_client.post('/match/setup', json=data | {
        'match_format':'T20', 'scheduled_overs':20, 'tournament_id':tournament.id, 'fixture_id':fixture.id})
    assert response.status_code == 200, response.json
    records = [json.loads(p.read_text()) for p in Path('data/matches').glob('*.json')]
    saved = next(r for r in records if r.get('match_id') == response.json['match_id'])
    assert saved['match_format'] == 'T10' and saved['scheduled_overs'] == 10
    tour = create_tour('Mixed tour', regular_user.id, *(t.id for t in teams),
                       {'T20':1, 'T10':2}, ['T20','T10'])
    assert [(s.format_type,s.scheduled_overs) for s in tour.series] == [('T20',20),('T10',10)]


def test_stats_nrr_export_identity(authenticated_client, teams, regular_user):
    seed(authenticated_client, teams)
    player = TeamProfile.query.filter_by(team_id=teams[0].id, format_type='T10').one().players[0]
    for fmt,runs in [('T20',200),('T10',80)]:
        match = Match(id='identity-'+fmt,user_id=regular_user.id,match_format=fmt,
            scheduled_overs=10 if fmt=='T10' else 20,overs_per_side=10 if fmt=='T10' else 20,
            home_team_id=teams[0].id,away_team_id=teams[1].id,home_team_score=runs,
            home_team_wickets=10,home_team_overs='8.2',date=datetime(2026,9,20))
        db.session.add(match);db.session.flush()
        db.session.add(MatchScorecard(match_id=match.id,player_id=player.id,team_id=teams[0].id,
            record_type='batting',runs=runs,balls=30,is_out=True))
    db.session.add(MatchScorecard(match_id='identity-T10',player_id=player.id,team_id=teams[0].id,
        record_type='batting',runs=999,balls=6,is_super_over=True))
    db.session.commit()
    records = StatsService().get_overall_stats(regular_user.id, 'T10')['batting']
    assert records and sum(r['runs'] for r in records) == 80
    match = db.session.get(Match,'identity-T10')
    assert TournamentEngine._get_nrr_overs('8.2',10,match) == '10.0'
    assert TournamentEngine._get_nrr_overs('8.2',4,match) == '8.2'


def test_pages_and_javascript(authenticated_client, teams, tmp_path):
    seed(authenticated_client, teams)
    urls = ['/match/setup','/team/create','/tournaments/create','/tournaments/tours/create',
            '/statistics?match_format=T10','/ground-conditions?format=T10',
            f'/team/{teams[0].id}/squad/T10']
    for i,url in enumerate(urls):
        page = authenticated_client.get(url)
        assert page.status_code == 200, url
        text = page.get_data(as_text=True)
        (tmp_path / f'page{i}.html').write_text(text)
        assert 'T10' in text
        if url.startswith('/ground-conditions'):
            assert 'id="gcPitchGrid"' in text and 'id="pp-boundary"' in text
            assert 'id="gcModesRow"' in text and 'id="death-wicket"' in text
        scripts = [body for attrs,body in re.findall(r'<script\b([^>]*)>(.*?)</script>',text,re.S)
                   if 'src=' not in attrs and ('type=' not in attrs or 'javascript' in attrs)]
        path = tmp_path / f'page{i}.js';path.write_text('\n'.join(scripts))
        result = subprocess.run(['node','--check',str(path)],capture_output=True,text=True)
        assert result.returncode == 0, (url,result.stderr)
    if os.environ.get('T10_BROWSER_CHECK') == '1':
        result = subprocess.run(['node', 'scripts/check_t10_ui.cjs', str(tmp_path)],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr


def test_pool_seed_uses_override_and_zero(authenticated_client, regular_user):
    from database.models import MasterPlayer, UserPlayer
    master = MasterPlayer(name='T10 Pool Seed', batting_rating=90, bowling_rating=80)
    db.session.add(master); db.session.flush()
    override = UserPlayer(user_id=regular_user.id, master_player_id=master.id,
                          name=master.name, batting_rating=0, bowling_rating=75)
    db.session.add(override); db.session.commit()
    response = authenticated_client.get('/api/player-pool/search?q=T10%20Pool%20Seed')
    row = response.json['players'][0]
    assert row['format_ratings']['T10'] == row['format_ratings']['T20']
    assert row['format_ratings']['T10']['batting'] == 0
    assert row['format_ratings']['T10']['bowling'] == 75


def test_archive_is_format_isolated_and_idempotent(authenticated_client, teams, monkeypatch, tmp_path):
    import csv
    import engine.match as match_module
    from match_archiver import MatchArchiver
    seed(authenticated_client, teams)
    response = authenticated_client.post('/match/setup', json=dict(
        team_home=teams[0].id, team_away=teams[1].id, match_format='T10',
        pitch='Hard', stadium='Test Ground', toss='Heads',
        toss_winner=teams[0].short_code, toss_decision='Bat'))
    assert response.status_code == 200, response.json
    mid = response.json['match_id']
    data = next(json.loads(p.read_text()) for p in Path('data/matches').glob('*.json')
                if json.loads(p.read_text()).get('match_id') == mid)
    m = match_module.Match(data)
    monkeypatch.setattr(m, '_create_match_archive', lambda: None)
    monkeypatch.setattr(match_module, 'print', lambda *a, **kw: None)
    monkeypatch.setattr(match_module, 'calculate_outcome', lambda **kw: {
        'runs': 0 if m.innings == 1 else 1, 'batter_out': False,
        'is_extra': False, 'description': 'Single'})
    for _ in range(65):
        result = m.next_ball()
        if result.get('match_over'): break
    assert result.get('match_over')
    # A stale T20 id must not divert a T10 card into the T20 career row.
    t20 = TeamProfile.query.filter_by(team_id=teams[0].id, format_type='T20').one()
    for stats in m.first_innings_batting_stats.values():
        stats['id'] = t20.players[0].id
    archiver = MatchArchiver(data, m)
    archiver.archive_path = tmp_path
    assert archiver._save_to_database()
    archived = db.session.get(Match, mid)
    assert archived.match_format == 'T10' and archived.scheduled_overs == 10
    cards = MatchScorecard.query.filter_by(match_id=mid).all()
    assert cards and all(card.player_ref.profile.format_type == 'T10' for card in cards)
    counts = {p.id: (p.matches_played, p.total_runs, p.total_balls_bowled)
              for p in Player.query.all()}
    assert archiver._save_to_database()
    assert len(MatchScorecard.query.filter_by(match_id=mid).all()) == len(cards)
    assert counts == {p.id: (p.matches_played, p.total_runs, p.total_balls_bowled)
                      for p in Player.query.all()}
    assert all(p.matches_played == 0 for p in t20.players)
    archiver._create_batting_csv('t10.csv', m.first_innings_batting_stats,
                                teams[0].name, m.home_xi)
    rows = list(csv.DictReader((tmp_path / 't10.csv').open()))
    assert rows and all(r['Match Format'] == 'T10' and r['Scheduled Overs'] == '10' for r in rows)


@pytest.mark.parametrize('mode', ['auto', 'manual'])
def test_t10_http_socket_replay_and_live_refresh(app, authenticated_client, regular_user, monkeypatch, mode):
    import app as app_module
    import engine.match as match_module
    from tests.test_t10 import make_match
    m = make_match(simulation_mode=mode, created_by=regular_user.id)
    mid = m.data['match_id']
    monkeypatch.setattr(match_module, 'print', lambda *a, **kw: None)
    monkeypatch.setattr(match_module, 'calculate_outcome', lambda **kw: {
        'runs': 0, 'batter_out': False, 'is_extra': False, 'description': 'Dot'})
    app_module.MATCH_INSTANCES[mid] = m
    socket = app_module.socketio.test_client(app, flask_test_client=authenticated_client)
    try:
        url = f'/match/{mid}'
        invalid_xi = [dict(p) for p in m.home_xi]
        invalid_xi[-1] = invalid_xi[0]
        rejected = authenticated_client.post(url + '/update-final-lineups', json={'home_final_xi': invalid_xi})
        assert rejected.status_code == 400
        original_ratings = {p['name']: p['bowling_rating'] for p in m.home_xi}
        reordered = [dict(p, bowling_rating=0, will_bowl=False) for p in reversed(m.home_xi)]
        accepted = authenticated_client.post(url + '/update-final-lineups', json={'home_final_xi': reordered})
        assert accepted.status_code == 200, accepted.json
        assert {p['name']: p['bowling_rating'] for p in m.home_xi} == original_ratings
        assert sum(bool(p['will_bowl']) for p in m.home_xi) == 5
        token = authenticated_client.get(url + '/live-state?delivery=1').json['delivery_token']
        response = authenticated_client.post(url + '/next-ball', json={'delivery_token': token})
        assert response.status_code == 200 and not response.json.get('error'), response.json
        socket.emit('next_ball', {'match_id': mid, 'delivery_token': token})
        replay = next(e['args'][0] for e in socket.get_received() if e['name'] == 'ball_result')
        assert replay == response.json
        if mode == 'manual':
            assert m.current_ball == 0 and m.pending_decision['type'] == 'next_bowler'
            selection = authenticated_client.post(url + '/submit-decision', json={
                'type': 'next_bowler', 'selected_index': m.pending_decision['options'][0]['index']})
            assert selection.status_code == 200, selection.json
            token = authenticated_client.get(url + '/live-state?delivery=1').json['delivery_token']
            assert authenticated_client.post(url + '/next-ball', json={'delivery_token': token}).status_code == 200
        assert m.current_ball == 1
        refreshed = authenticated_client.get(url + '/live-state').json
        assert refreshed['match_format'] == 'T10' and refreshed['current_ball'] == 1
        assert refreshed['total_overs'] == refreshed['scheduled_overs'] == 10
        assert not refreshed['bowling_eligibility']['selectable']
    finally:
        socket.disconnect()
        app_module.MATCH_INSTANCES.pop(mid, None)
