"""Forty-over creation, fixture authority, and archive metadata integration."""
import json
from pathlib import Path
import pytest
from database import db
from database.models import Tournament, TournamentFixture
from engine.tour_engine import create_tour
from tests.test_tours import add_ready_profiles


@pytest.fixture
def lista_teams(test_team, test_team_2):
    for team in (test_team, test_team_2):
        add_ready_profiles(team)
    return test_team, test_team_2


def payload(teams, **extra):
    return dict(team_home=teams[0].id, team_away=teams[1].id,
                match_format='ListA', simulation_mode='auto', scheduled_overs=40, **extra)


def test_standalone_persists_and_labels_length(authenticated_client, lista_teams):
    response = authenticated_client.post('/match/setup', json=payload(lista_teams))
    assert response.status_code == 200, response.json
    match_id = response.json['match_id']
    # The persisted JSON is the restart source of truth.
    paths = list(Path('data/matches').glob(f'*{match_id}*.json'))
    if not paths:
        paths = [p for p in Path('data/matches').glob('*.json') if json.loads(p.read_text()).get('match_id') == match_id]
    assert paths
    saved = json.loads(paths[0].read_text())
    assert saved['match_format'] == 'ListA' and saved['scheduled_overs'] == saved['overs'] == 40
    assert saved['weather_script']['events'] == []


@pytest.mark.parametrize('value', [20,41,True,'oops'])
def test_invalid_length_rejected(authenticated_client, lista_teams, value):
    data = payload(lista_teams); data['scheduled_overs'] = value
    response = authenticated_client.post('/match/setup', json=data)
    assert response.status_code == 400
    assert 'scheduled overs' in response.json['error']


def test_fixture_uses_locked_length(authenticated_client, lista_teams, regular_user):
    tournament = Tournament(name='Forty cup', user_id=regular_user.id, format_type='ListA', scheduled_overs=40)
    db.session.add(tournament); db.session.flush()
    fixture = TournamentFixture(tournament_id=tournament.id, home_team_id=lista_teams[0].id,
                                away_team_id=lista_teams[1].id, round_number=1, stage='league', status='Scheduled')
    db.session.add(fixture); db.session.commit()
    page = authenticated_client.get(f'/match/setup?fixture_id={fixture.id}')
    assert page.status_code == 200
    assert 'List A · 40 overs' in page.get_data(as_text=True)
    data = payload(lista_teams, fixture_id=fixture.id, tournament_id=tournament.id)
    data['scheduled_overs'] = 50
    response = authenticated_client.post('/match/setup', json=data)
    assert response.status_code == 200, response.json
    match_id = response.json['match_id']
    # Setup writes JSON before the engine is created.
    records = [json.loads(p.read_text()) for p in Path('data/matches').glob('*.json')]
    assert next(r for r in records if r.get('match_id') == match_id)['scheduled_overs'] == 40


def test_tour_series_length_and_confirmation(authenticated_client, lista_teams, regular_user):
    tour = create_tour('Short tour', regular_user.id, *(t.id for t in lista_teams),
                       {'ListA':2}, ['ListA'], scheduled_overs=40)
    assert len(tour.series) == 1 and tour.series[0].scheduled_overs == 40
    data = dict(name='Confirmed short tour', host_team_id=str(lista_teams[0].id),
                visiting_team_id=str(lista_teams[1].id), count_ListA='2', count_FC='0', count_T20='0',
                format_order=['ListA'], scheduled_overs='40', confirm_schedule='yes')
    snapshot = dict(name=data['name'], host=data['host_team_id'], visitor=data['visiting_team_id'],
                    counts={'FC':'0','ListA':'2','T20':'0'}, order=['ListA'], scheduled_overs='50')
    data['schedule_confirmation'] = json.dumps(snapshot)
    before = Tournament.query.count()
    authenticated_client.post('/tournaments/tours/create', data=data)
    assert Tournament.query.count() == before
    snapshot['scheduled_overs'] = '40'; data['schedule_confirmation'] = json.dumps(snapshot)
    response = authenticated_client.post('/tournaments/tours/create', data=data)
    assert response.status_code == 302
    assert Tournament.query.count() == before + 1


def test_archive_and_csv_label(tmp_path):
    from tests.test_lista_40 import make_match
    from match_archiver import MatchArchiver
    m = make_match(); m.data['timestamp'] = '2026-09-20'
    archiver = MatchArchiver(m.data, m)
    archiver.archive_path = tmp_path
    assert 'List A · 40 overs' in archiver._generate_text_header()
    archiver._create_bowling_csv('bowling.csv', {'Bowler':{'balls_bowled':48,'runs':32,'wickets':2}}, 'Team')
    text = (tmp_path/'bowling.csv').read_text()
    assert 'Scheduled Overs' in text and 'List A · 40 overs,40' in text


def test_setup_inline_javascript_parses(authenticated_client, lista_teams, tmp_path):
    import re
    import shutil
    import subprocess
    if not shutil.which('node'):
        pytest.skip('Node.js unavailable')
    for index, url in enumerate(('/match/setup', '/tournaments/create', '/tournaments/tours/create',
                                 '/statistics?match_format=ListA&scheduled_overs=40')):
        response = authenticated_client.get(url)
        assert response.status_code == 200
        inline = []
        for attributes, body in re.findall(r'<script\b([^>]*)>(.*?)</script>', response.get_data(as_text=True), re.S):
            if 'src=' not in attributes and ('type=' not in attributes or 'javascript' in attributes):
                inline.append(body)
        path = tmp_path / f'page-{index}.js'
        path.write_text('\n'.join(inline))
        result = subprocess.run(['node', '--check', str(path)], capture_output=True, text=True)
        assert result.returncode == 0, (url, result.stderr)
