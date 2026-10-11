"""Profile transactions and server-owned snapshots with shared QA accounts."""
import copy
import json

import pytest
from database import db
from database.models import Team, TeamProfile, Player, Stadium
from engine.captaincy import default_config
from scripts.dev_test_accounts import seed, logged_in_client, ACCOUNTS
from services.stadiums import seed_catalogue
from tests.test_stadiums import populated_team, read_created


@pytest.fixture
def qa(app):
    seed(app)
    seed_catalogue()
    return logged_in_client(app, 'user1')


def profile_payload(profile, config=None):
    payload = {'captain': next(p.name for p in profile.players if p.is_captain),
        'wicketkeeper': next(p.name for p in profile.players if p.is_wicketkeeper),
        'players': [dict(name=p.name, role=p.role, batting_rating=p.batting_rating,
            bowling_rating=p.bowling_rating, fielding_rating=p.fielding_rating,
            batting_hand=p.batting_hand, bowling_type=p.bowling_type, bowling_hand=p.bowling_hand)
            for p in profile.players]}
    if config is not None:
        payload['captaincy_config'] = config
    return payload


def test_edit_squad_and_clone_preserve_profile_config(app, qa):
    team = populated_team('CAP')
    prof = next(p for p in team.profiles if p.format_type == 'T20')
    cfg = default_config(); cfg['composure'] = 0; cfg['bowling_management'] = 92
    prof.captaincy_config = cfg
    db.session.commit()
    # Omitted captaincy in an older squad payload must preserve its settings.
    p = profile_payload(prof)
    p['captain'] = prof.players[-1].name
    response = qa.post(f'/api/team/{team.id}/squad/T20/bulk-publish', json={
        'players': p['players'], 'captain_name': p['captain'], 'wk_name': p['wicketkeeper'], 'is_draft': True})
    assert response.status_code == 200, response.json
    db.session.refresh(prof)
    assert prof.captaincy_config == cfg
    response = qa.post('/team/CAP/clone')
    assert response.status_code == 302
    clone = Team.query.filter(Team.short_code != 'CAP').filter(Team.name.contains('(Copy)')).first()
    assert next(p for p in clone.profiles if p.format_type == 'T20').captaincy_config == cfg
    for url in ('/team/CAP/edit', f'/team/{team.id}/squad/T20', '/teams/manage', '/match/setup'):
        response = qa.get(url)
        assert response.status_code == 200, url
        assert b'captaincy' in response.data.lower()


@pytest.mark.parametrize('invalid', [101, -1, True, '50', None])
def test_invalid_config_does_not_mutate_squad(app, qa, invalid):
    team = populated_team('CAP')
    profile = team.profiles[0]
    original = [(p.id, p.name) for p in profile.players]
    response = qa.post(f'/api/team/{team.id}/squad/{profile.format_type}/bulk-publish', json={
        'players': [], 'captaincy_config': {'match_awareness': invalid}})
    assert response.status_code == 400
    assert 'Match awareness' in response.json['error']
    assert [(p.id, p.name) for p in profile.players] == original


@pytest.mark.parametrize('fmt', ['T10', 'T20', 'ListA', 'FC', 'Hundred'])
def test_match_snapshot_is_authoritative_and_immutable(app, qa, fmt):
    home = populated_team('CAP')
    away = populated_team('OPP')
    source_fmt = 'T20' if fmt == 'Hundred' else fmt
    profile = next(p for p in home.profiles if p.format_type == source_fmt)
    config = default_config(); config['batting_aggression'] = 0; config['match_awareness'] = 88
    profile.captaincy_config = config
    db.session.commit()
    data, path = read_created(qa.post('/match/setup', json={
        'team_home': home.id, 'team_away': away.id, 'match_format': fmt,
        'pitch': 'Hard', 'stadium': 'Original ground', 'toss': 'Heads',
        'captaincy_model_version': 999, 'captaincy': {'home': {'config': {'batting_aggression': 100}}}}))
    try:
        assert data['captaincy_model_version'] == 1
        assert data['captaincy']['home']['config'] == config
        assert data['captaincy']['home']['captain']['name'] == next(p.name for p in profile.players if p.is_captain)
        page = qa.get(f"/match/{data['match_id']}")
        assert page.status_code == 200
        # The Captain tactics panel was removed from the match page (UX_TODO, Oct 2026).
        assert b'id="captaincy-match"' not in page.data
        profile.captaincy_config = default_config()
        db.session.commit()
        assert json.loads(path.read_text())['captaincy']['home']['config'] == config
    finally:
        path.unlink(missing_ok=True)


def test_migration_is_idempotent_and_preserves_assigned_values(tmp_path):
    import sqlite3
    from flask import Flask
    from flask_sqlalchemy import SQLAlchemy
    from migrations.add_captaincy import run_migration
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{tmp_path / "migration.db"}'
    store = SQLAlchemy(app)
    with app.app_context(), store.engine.begin() as conn:
        from sqlalchemy import text
        conn.execute(text('CREATE TABLE team_profiles (id INTEGER PRIMARY KEY)'))
        conn.execute(text('INSERT INTO team_profiles (id) VALUES (1)'))
    run_migration(store, app)
    with app.app_context(), store.engine.begin() as conn:
        from sqlalchemy import text
        cfg = default_config(); cfg['composure'] = 0
        conn.execute(text('UPDATE team_profiles SET captaincy_config=:config'), {'config': json.dumps(cfg)})
    run_migration(store, app)
    with app.app_context(), store.engine.connect() as conn:
        assert json.loads(conn.execute(text('SELECT captaincy_config FROM team_profiles')).scalar()) == cfg


def test_squad_saves_supplied_config_without_changing_other_formats(app, qa):
    team = populated_team('CAP')
    profile = next(p for p in team.profiles if p.format_type == 'T20')
    payload = profile_payload(profile)
    config = default_config(); config['batting_aggression'] = 0
    response = qa.post(f'/api/team/{team.id}/squad/T20/bulk-publish', json={
        'players': payload['players'], 'captain_name': payload['captain'],
        'wk_name': payload['wicketkeeper'], 'is_draft': True, 'captaincy_config': config})
    assert response.status_code == 200, response.json
    db.session.refresh(profile)
    assert profile.captaincy_config == config
    assert all(p.captaincy_config == default_config() for p in team.profiles if p.format_type != 'T20')


def test_create_and_edit_profile_configs_use_shared_validation(app, qa):
    from tests.test_team_routes import _valid_team_form
    form = _valid_team_form(short_code='NWCAP')
    form.update(stadium_id='__custom__', custom_stadium_name='Captaincy QA ground')
    template = populated_team('TEMPLATE')
    config = default_config(); config['match_awareness'] = 0; config['batting_aggression'] = 75
    profiles = {p.format_type: profile_payload(p, config if p.format_type == 'T20' else default_config()) for p in template.profiles}
    form['profiles_payload'] = json.dumps(profiles)
    response = qa.post('/team/create', data=form)
    assert response.status_code == 302, response.status_code
    created = Team.query.filter_by(short_code='NWCAP').one()
    profile = next(p for p in created.profiles if p.format_type == 'T20')
    assert profile.captaincy_config == config
    config['bowling_management'] = 95
    profiles['T20']['captaincy_config'] = config
    profiles['T20']['captain'] = profiles['T20']['players'][-1]['name']
    form['profiles_payload'] = json.dumps(profiles)
    form.pop('stadium_id'); form.pop('custom_stadium_name')
    response = qa.post('/team/NWCAP/edit', data=form)
    assert response.status_code == 302, response.status_code
    db.session.refresh(profile)
    assert profile.captaincy_config == config
