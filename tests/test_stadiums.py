"""Stadium compatibility, ownership, conditions and migration contracts."""
from copy import deepcopy
import json
import random

import pytest
from sqlalchemy import create_engine, text, inspect

from database import db
from database.models import Team, Stadium, StadiumRevision, StadiumRevisionV3
from engine.format_catalog import SUPPORTED_FORMATS
from engine.format_config import get_any_format
from engine.ground_config import get_defaults
from engine.stadium_conditions import resolve_conditions, apply_venue_weights
from services.stadiums import neutral_profile, validate_profile, create_custom, seed_catalogue, current_profile
from scripts.dev_test_accounts import seed, logged_in_client, ACCOUNTS


def conditions(fmt='T20', seed_value=42, profile=None, overrides=None, night=False):
    return resolve_conditions(profile or neutral_profile(), 'test-stadium', 1,
        get_any_format(fmt), get_defaults(fmt, mutable=True), night,
        overrides or {}, seed_value)


@pytest.fixture
def qa(app):
    seed(app)
    seed_catalogue()
    return logged_in_client(app, 'user1')


def make_team(mode='legacy', stadium_id=None, code='OLD'):
    team = Team(user_id=ACCOUNTS['user1']['email'], name='Existing team ' + code,
                short_code=code, home_ground='Original ground', pitch_preference='Green',
                team_color='#123456', venue_mode=mode, stadium_id=stadium_id, is_draft=True)
    db.session.add(team)
    db.session.commit()
    return team


@pytest.mark.parametrize('fmt', SUPPORTED_FORMATS)
def test_conditions_deterministic_and_neutral(fmt):
    cfg = get_defaults(fmt, mutable=True)
    before = deepcopy(cfg)
    a = conditions(fmt)
    assert a == conditions(fmt)
    assert cfg == before
    weights = {'Dot': .3, 'Single': .2, 'Double': .1, 'Three': .03, 'Four': .15, 'Six': .12, 'Wicket': .08, 'Extras': .02}
    assert apply_venue_weights(weights, a, 'Fast') == weights
    assert a['ground_config']['blending'] == cfg['blending']


def test_geometry_direction_and_caps():
    weights = {k: 1.0 for k in ('Four', 'Six', 'Double', 'Three', 'Wicket', 'Extras')}
    small = apply_venue_weights(weights, conditions(profile={**neutral_profile(), 'dimensions_m': {key: 45 for key in neutral_profile()['dimensions_m']}}), 'Fast')
    large = apply_venue_weights(weights, conditions(profile={**neutral_profile(), 'dimensions_m': {key: 95 for key in neutral_profile()['dimensions_m']}}), 'Fast')
    assert small['Six'] > large['Six']
    assert small['Four'] > large['Four']
    assert small['Double'] < large['Double']
    assert all(.8 <= value <= 1.2 for value in [*small.values(), *large.values()])


def test_v1_dew_is_preserved_and_v2_has_no_stadium_dew():
    snapshot = conditions(night=True)
    weights = {'Four': 1, 'Six': 1, 'Wicket': 1, 'Extras': 1}
    assert apply_venue_weights(weights, snapshot, 'Fast', 2, 120) == weights
    assert 'dew' not in snapshot and 'surface' not in snapshot
    snapshot.update(conditions_model_version='stadium_v1',
        surface={'seam_residual': 1, 'spin_residual': 1, 'wear_rate_multiplier': 1},
        dew={'intensity': 1, 'start_ball': 150, 'peak_ball': 210})
    assert apply_venue_weights(weights, snapshot, 'Fast', 1, 100) == weights
    late = apply_venue_weights(weights, snapshot, 'Fast', 2, 120)
    assert late['Wicket'] == .925
    assert late['Extras'] == 1.2
    assert apply_venue_weights(weights, snapshot, 'Fast', super_over=True) == late
    with pytest.raises(ValueError):
        conditions(night=True, overrides={'dew_intensity': 1})


@pytest.mark.parametrize('mutate', [
    lambda p: p['dimensions_m'].update(straight_a=101),
    lambda p: p.update(outfield_speed=float('nan')),
    lambda p: p['pitch_defaults'].update(T20='Unknown'),
    lambda p: p['pitch_defaults'].pop('FC'),
    lambda p: p.update(timezone='UTC'),
    lambda p: p.update(average_temperature_c=True),
    lambda p: p.update(average_temperature_c=float('inf')),
    lambda p: p.update(name=' '),
])
def test_profile_validation(mutate):
    profile = neutral_profile()
    mutate(profile)
    with pytest.raises(ValueError):
        validate_profile(profile)


def test_catalogue_seed_idempotent_and_pages(app, qa):
    assert Stadium.query.filter_by(kind='official').count() == 16
    seed_catalogue()
    assert Stadium.query.filter_by(kind='official').count() == 16
    assert qa.get('/stadiums').status_code == 200
    assert qa.get('/team/create').status_code == 200
    assert qa.get('/match/setup').status_code == 200
    stadium = Stadium.query.first()
    assert qa.get('/stadiums/' + stadium.id).status_code == 200
    assert qa.get('/stadiums/new').status_code == 200


def test_new_team_requires_existing_new_format_stadium(app, qa):
    form = dict(team_name='New team', short_code='NEW', home_ground='Text ground', pitch_preference='Hard')
    assert qa.post('/team/create', data=form).status_code == 400
    assert Team.query.count() == 0
    form.update(stadium_id='__custom__', custom_stadium_name='My new ground')
    before = Stadium.query.count()
    response = qa.post('/team/create', data=form)
    assert response.status_code == 400
    assert b'Create your stadium on the Stadiums page' in response.data
    assert Stadium.query.count() == before
    assert Team.query.count() == 0
    from services.stadium_tuning import create_personal
    stadium = create_personal(ACCOUNTS['user1']['email'], neutral_profile('My new ground'))
    db.session.commit()
    form['stadium_id'] = stadium.id
    assert qa.post('/team/create', data=form).status_code == 302
    team = Team.query.one()
    assert team.venue_mode == 'stadium' and team.stadium_id
    assert team.home_ground == 'My new ground'


def test_clone_legacy_is_incomplete_stadium_draft(app, qa):
    old = make_team()
    assert qa.post('/team/OLD/clone').status_code == 302
    clone = Team.query.filter(Team.id != old.id).one()
    assert clone.venue_mode == 'stadium'
    assert clone.stadium_id is None and clone.is_draft
    from services.stadiums import team_venue_error
    assert team_venue_error(old, old.user_id) is None
    assert team_venue_error(clone, clone.user_id)
    assert qa.post(f'/api/team/{clone.id}/squad/T20/bulk-publish', json={'players': []}).status_code == 400


def test_revision_permissions_retirement_and_preview(app, qa):
    response = qa.post('/api/stadiums', json=neutral_profile('Private ground'))
    assert response.status_code == 201
    row = response.json
    other = logged_in_client(app, 'user2')
    assert other.get('/api/stadiums/' + row['id']).status_code == 404
    assert other.patch('/api/stadiums/' + row['id'], json={'revision': 1, 'profile': {'name':'Stolen'}}).status_code == 400
    preview = qa.post('/api/stadiums/' + row['id'] + '/preview', json={'match_format': 'T20'}).json
    assert not {'weather_script', 'conditions_seed', 'events'} & preview.keys()
    revision = db.session.get(StadiumRevisionV3, (row['id'], 1))
    snap = resolve_conditions(current_profile(db.session.get(Stadium, row['id'])), row['id'], 1,
        get_any_format('T20'), get_defaults('T20', mutable=True), False, {}, 42,
        baseline_configs=revision.baseline_configs, baseline_provenance=revision.baseline_provenance)
    old = deepcopy(snap)
    updated = qa.patch('/api/stadiums/' + row['id'], json={'revision': 1, 'profile': {'name': 'Changed'}})
    assert updated.status_code == 200 and updated.json['revision'] == 2
    assert db.session.get(StadiumRevisionV3, (row['id'], 1)).name == 'Private ground'
    assert snap == old
    assert qa.patch('/api/stadiums/' + row['id'], json={'revision': 1, 'profile': {'name': 'Stale'}}).status_code == 400
    assert qa.post('/api/stadiums/' + row['id'] + '/retire').status_code == 200
    from services.stadiums import accessible_stadium
    with pytest.raises(ValueError):
        accessible_stadium(row['id'], ACCOUNTS['user1']['email'])


def test_migration_preserves_preexisting_teams(tmp_path, monkeypatch):
    from flask import Flask
    from types import SimpleNamespace
    from migrations.add_stadiums import run_migration
    engine = create_engine('sqlite:///' + str(tmp_path / 'legacy.db'))
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE users (id VARCHAR(120) PRIMARY KEY)'))
        conn.execute(text('CREATE TABLE teams (id INTEGER PRIMARY KEY, home_ground TEXT, pitch_preference TEXT)'))
        conn.execute(text("INSERT INTO teams VALUES (1, 'My original ground', 'Dry')"))
    monkeypatch.setattr('services.stadiums.seed_catalogue', lambda: None)
    app = Flask('migration-test')
    run_migration(SimpleNamespace(engine=engine), app)
    run_migration(SimpleNamespace(engine=engine), app)
    with engine.connect() as conn:
        row = conn.execute(text('SELECT home_ground, pitch_preference, venue_mode, stadium_id FROM teams')).one()
        assert tuple(row) == ('My original ground', 'Dry', 'legacy', None)


def populated_team(code, mode='legacy', stadium_id=None):
    from database.models import Player, TeamProfile
    team = make_team(mode, stadium_id, code)
    team.is_draft = False
    for fmt in ('T20', 'T10', 'ListA', 'FC'):
        profile = TeamProfile(team_id=team.id, format_type=fmt)
        db.session.add(profile)
        db.session.flush()
        for i in range(12):
            role = 'Wicketkeeper' if i == 0 else 'All-rounder' if i < 7 else 'Batsman'
            db.session.add(Player(team_id=team.id, profile_id=profile.id, name=f'{code} Player {i}',
                role=role, batting_rating=70, bowling_rating=70, fielding_rating=70,
                batting_hand='Right', bowling_hand='Right', bowling_type='Fast' if i < 4 else 'Off spin',
                is_captain=i == 1, is_wicketkeeper=i == 0))
    db.session.commit()
    return team


def read_created(response):
    from pathlib import Path
    from app import PROJECT_ROOT
    assert response.status_code == 200, response.json
    path = Path(PROJECT_ROOT) / 'data/matches' / f'match_{response.json["match_id"]}.json'
    data = json.loads(path.read_text())
    return data, path


@pytest.mark.parametrize('fmt', SUPPORTED_FORMATS)
def test_full_stadium_match_setup_and_engine(app, qa, fmt):
    from engine.match import Match
    stadium = Stadium.query.first()
    home = populated_team('NEW', 'stadium', stadium.id)
    away = populated_team('OLD')
    payload = {'team_home': home.id, 'team_away': away.id, 'match_format': fmt,
        'pitch': 'Injected', 'stadium': 'Injected', 'toss': 'Heads',
        'conditions_model_version': 'legacy', 'conditions_snapshot': {'injected': True},
        'weather_forecast': 'clear', 'condition_overrides': {}}
    data, path = read_created(qa.post('/match/setup', json=payload))
    try:
        assert data['conditions_model_version'] == 'stadium_v2'
        assert data['stadium_id'] == stadium.id
        assert data['pitch'] != 'Injected' and data['stadium'] != 'Injected'
        assert data['conditions_snapshot']['ground_config']['blending'] == get_defaults(fmt)['blending']
        original = deepcopy(data)
        # Neither mutable stadium/team metadata nor client config is consulted on resume.
        stadium.current_revision = 999
        home.stadium_id = None
        db.session.commit()
        data['ground_config'] = {'invalid': True}
        match = Match(data)
        assert match.pitch == original['pitch']
        assert match.ground_config['blending'] == original['ground_config']['blending']
        assert match._compute_pitch_wear() == 0
        response = qa.get('/match/' + data['match_id'])
        assert response.status_code == 200
        # The Stadium conditions summary was removed from the match page (UX_TODO, Oct 2026).
        assert b'Stadium conditions' not in response.data
        assert b'conditions_seed' not in response.data
    finally:
        path.unlink(missing_ok=True)


def test_legacy_home_neutral_selection_and_adoption(app, qa):
    home = populated_team('OLD')
    away = populated_team('AWY')
    stadium = Stadium.query.first()
    adopted = qa.post('/api/stadiums/' + stadium.id + '/adopt', json={'revision': stadium.current_revision})
    assert adopted.status_code == 200
    stadium = db.session.get(Stadium, adopted.json['id'])
    payload = {'team_home': home.id, 'team_away': away.id, 'pitch': 'Green', 'toss': 'Heads'}
    legacy, path = read_created(qa.post('/match/setup', json=payload))
    try:
        assert legacy['conditions_model_version'] == 'legacy'
        assert legacy['stadium'] == 'Original ground'
        assert 'conditions_snapshot' not in legacy
        neutral, neutral_path = read_created(qa.post('/match/setup', json={**payload, 'stadium_id': stadium.id}))
        neutral_path.unlink()
        assert neutral['conditions_model_version'] == 'stadium_v3'
        assert home.venue_mode == 'legacy' and home.stadium_id is None
        assert qa.post(f'/api/team/{home.id}/stadium', json={'stadium_id': stadium.id}).status_code == 200
        db.session.refresh(home)
        assert home.venue_mode == 'stadium' and home.home_ground == 'Original ground'
        assert home.pitch_preference == 'Green'
        assert json.loads(path.read_text()) == legacy
        assert qa.post(f'/api/team/{home.id}/stadium', json={'stadium_id': None}).status_code == 400
    finally:
        path.unlink(missing_ok=True)


def test_flag_disables_new_stadium_matches_only(app, qa):
    home = populated_team('OLD')
    away = populated_team('AWY')
    app.config['STADIUM_MATCHES_ENABLED'] = False
    payload = {'team_home': home.id, 'team_away': away.id, 'pitch': 'Green', 'toss': 'Heads'}
    _, path = read_created(qa.post('/match/setup', json=payload))
    path.unlink()
    payload['stadium_id'] = Stadium.query.first().id
    assert qa.post('/match/setup', json=payload).status_code == 503


@pytest.mark.parametrize('fmt', SUPPORTED_FORMATS)
@pytest.mark.parametrize('version', ('stadium_v1', 'stadium_v2'))
def test_stadium_delivery_and_resume_state(fmt, version, monkeypatch):
    from engine.match import Match
    from tests.test_bowler_consecutive_guards import _build_match_data
    import engine.match as match_module
    monkeypatch.setattr(match_module, 'print', lambda *args, **kwargs: None)
    monkeypatch.setattr(Match, '_create_match_archive', lambda self: True)
    data = _build_match_data(fmt)
    snapshot = conditions(fmt)
    if version == 'stadium_v1':
        snapshot.update(schema_version=1, conditions_model_version=version,
            surface={'seam_residual': 1.04, 'spin_residual': .96, 'wear_rate_multiplier': 1.08},
            dew={'intensity': .4 if fmt != 'FC' else 0, 'start_ball': 150, 'peak_ball': 210})
    data.update(pitch=snapshot['pitch'], conditions_model_version=version, conditions_snapshot=snapshot)
    random.seed(654)
    match = Match(data)
    for _ in range(25):
        result = match.next_ball()
        assert 'error' not in result, result
    assert match._compute_pitch_wear() > 0
    if fmt == 'FC':
        state = json.loads(json.dumps(match.serialize_fc_snapshot()))
        resumed = Match(deepcopy(data))
        resumed.restore_fc_snapshot(state)
        assert resumed._compute_pitch_wear() == match._compute_pitch_wear()
    elif fmt == 'Hundred':
        from engine.hundred_snapshot import serialize, restore
        state = json.loads(json.dumps(serialize(match)))
        resumed = Match(deepcopy(data))
        restore(resumed, state)
        assert resumed._compute_pitch_wear() == match._compute_pitch_wear()


def test_legacy_explicit_model_has_identical_delivery_sequence(monkeypatch):
    from engine.match import Match
    from tests.test_bowler_consecutive_guards import _build_match_data
    import engine.match as match_module
    monkeypatch.setattr(match_module, 'print', lambda *args, **kwargs: None)
    data = _build_match_data('T20')
    data['created_at'] = 1
    results = []
    for explicit in (False, True):
        payload = deepcopy(data)
        if explicit:
            payload['conditions_model_version'] = 'legacy'
        random.seed(908)
        match = Match(payload)
        deliveries = [match.next_ball() for _ in range(30)]
        results.append((match.score, match.wickets, match.current_over, match.current_ball,
                        [(d.get('runs'), d.get('wicket'), d.get('outcome')) for d in deliveries]))
    assert results[0] == results[1]


def test_weather_reduction_does_not_accelerate_stadium_wear(monkeypatch):
    from engine.match import Match
    from tests.test_bowler_consecutive_guards import _build_match_data
    match = Match({**_build_match_data('T20'), 'conditions_model_version': 'stadium_v2',
                   'conditions_snapshot': conditions()})
    match.innings_balls_bowled = 60
    before = match._compute_pitch_wear()
    match.overs = 10
    assert match._compute_pitch_wear() == before
    match.stadium_completed_balls = 60
    match.innings_balls_bowled = 0
    match.innings = 2
    assert match._compute_pitch_wear() == before


def test_edit_unrelated_fields_keeps_legacy_and_linked_cannot_downgrade(app, qa):
    from tests.test_team_routes import _valid_team_form
    team = populated_team('OLD')
    form = _valid_team_form('Renamed team', 'OLD')
    form.update(home_ground=team.home_ground, pitch_preference=team.pitch_preference)
    response = qa.post('/team/OLD/edit', data=form)
    assert response.status_code == 302
    db.session.refresh(team)
    assert team.name == 'Renamed team' and team.venue_mode == 'legacy'
    assert team.stadium_id is None and team.home_ground == 'Original ground'
    stadium = Stadium.query.first()
    adoption = qa.post('/api/stadiums/' + stadium.id + '/adopt', json={'revision':stadium.current_revision})
    assert adoption.status_code == 200
    stadium = db.session.get(Stadium, adoption.json['id'])
    qa.post(f'/api/team/{team.id}/stadium', json={'stadium_id': stadium.id})
    form.update(venue_mode='legacy', stadium_id='')
    assert qa.post('/team/OLD/edit', data=form).status_code == 302
    db.session.refresh(team)
    assert team.venue_mode == 'stadium' and team.stadium_id == stadium.id


def test_private_stadium_cannot_be_used_for_new_team(app, qa):
    row = create_custom(ACCOUNTS['user2']['email'], neutral_profile())
    db.session.commit()
    response = qa.post('/team/create', data={'team_name':'Forbidden', 'short_code':'FOR',
        'stadium_id':row.id, 'venue_mode':'legacy', 'home_ground':'Fake', 'pitch_preference':'Hard'})
    assert response.status_code == 400
    assert Team.query.count() == 0


def test_official_template_can_only_be_edited_by_admin(app, qa):
    row = Stadium.query.filter_by(kind='official').first()
    body = {'revision':1, 'profile':{'average_temperature_c':30}}
    assert qa.patch('/api/stadiums/'+row.id, json=body).status_code == 400
    admin = logged_in_client(app, 'admin')
    response = admin.patch('/api/stadiums/'+row.id, json=body)
    assert response.status_code == 200
    assert response.json['average_temperature_c'] == 30
    assert response.json['schema_version'] == 3
    assert qa.get('/api/stadiums/'+row.id).json['average_temperature_c'] == 30
    assert db.session.get(StadiumRevision, (row.id, 1)) is not None


def test_t10_does_not_compress_t20_wear():
    short = conditions('T10')
    t20 = conditions('T20')
    assert short['wear_reference_balls'] == t20['wear_reference_balls'] == 240


def test_dashboard_stadium_management_and_team_selection(app, qa):
    from bs4 import BeautifulSoup

    home = BeautifulSoup(qa.get('/').data, 'html.parser')
    card = home.select_one('.home-option[href="/stadiums"]')
    assert card is not None and card.h2.get_text() == 'Stadiums'

    form = dict(name='QA managed stadium', **{f'pitch_{fmt}': 'Hard' for fmt in SUPPORTED_FORMATS},
                straight_a=70, straight_b=70, square_a=65, square_b=65,
                outfield_speed=1, average_temperature_c=22)
    created = qa.post('/stadiums/new', data=form)
    assert created.status_code == 302
    row = Stadium.query.filter_by(kind='custom').one()
    ident = row.id
    catalogue = BeautifulSoup(qa.get('/stadiums?kind=custom').data, 'html.parser')
    assert catalogue.find('a', string='Edit stadium')
    select_link = catalogue.find('a', string='Use for new team')['href']
    team_form = BeautifulSoup(qa.get(select_link).data, 'html.parser')
    assert team_form.select_one('#tc-stadium option[selected]')['value'] == ident
    other = logged_in_client(app, 'user2')
    other_form = BeautifulSoup(other.get(select_link).data, 'html.parser')
    assert not other_form.select_one(f'#tc-stadium option[value="{ident}"]')

    edited = qa.post('/stadiums/' + ident, data={**form, 'revision': 1, 'name': 'QA renamed stadium'})
    assert edited.status_code == 302
    db.session.expire_all()
    assert current_profile(db.session.get(Stadium, ident))['name'] == 'QA renamed stadium'
    assert qa.post('/api/stadiums/' + ident + '/retire').status_code == 200
    retired_form = BeautifulSoup(qa.get(select_link).data, 'html.parser')
    assert not retired_form.select_one(f'#tc-stadium option[value="{ident}"]')
    assert b'This stadium is retired' in qa.get('/stadiums/' + ident).data


@pytest.mark.parametrize('fmt', SUPPORTED_FORMATS)
def test_pitch_defaults_and_only_pitch_overrides(fmt):
    profile = neutral_profile()
    profile['pitch_defaults'][fmt] = 'Dry'
    assert conditions(fmt, profile=profile)['pitch'] == 'Dry'
    assert conditions(fmt, profile=profile, overrides={'pitch': 'Green'})['pitch'] == 'Green'
    for removed in ('weather', 'boundary_m', 'outfield_speed', 'dew_intensity'):
        with pytest.raises(ValueError, match='Only a pitch'):
            conditions(fmt, overrides={removed: 1})


@pytest.mark.parametrize('fmt', SUPPORTED_FORMATS)
def test_temperature_controls_wear_without_direct_outcome_weights(fmt):
    from engine.match import Match
    from tests.test_bowler_consecutive_guards import _build_match_data
    wear = []
    for temp in (-20, 12, 22, 32, 55):
        snapshot = conditions(fmt, profile={**neutral_profile(), 'average_temperature_c': temp})
        match = Match({**_build_match_data(fmt), 'conditions_model_version': 'stadium_v2',
                       'conditions_snapshot': snapshot})
        if fmt == 'FC':
            # Exercise multiplication independently of FC's tested day-aware base progression.
            match._legacy_pitch_wear = lambda: .5
        else:
            match.innings_balls_bowled = snapshot['wear_reference_balls'] / 2
        wear.append(match._compute_pitch_wear())
        weights = {'Four': 1, 'Six': 1, 'Wicket': 1, 'Extras': 1}
        assert apply_venue_weights(weights, snapshot, 'Off spin') == weights
        assert 'expected_dew' not in snapshot['public_forecast']
    assert wear == pytest.approx([.425, .45, .5, .55, .575])


def test_outfield_direction_and_public_projection():
    from engine.stadium_conditions import public_match_data
    slow = conditions(profile={**neutral_profile(), 'outfield_speed': .9})
    fast = conditions(profile={**neutral_profile(), 'outfield_speed': 1.1})
    assert slow['coefficients']['Four'] == pytest.approx(.9)
    assert fast['coefficients']['Four'] == pytest.approx(1.1)
    public = public_match_data({'conditions_snapshot': fast, 'weather_script': fast['weather_script']})
    assert 'weather_script' not in public
    assert public['conditions_snapshot']['average_temperature_c'] == 22
    assert not {'conditions_seed', 'ground_config', 'weather_script', 'surface', 'dew', 'environment'} & public['conditions_snapshot'].keys()


@pytest.mark.parametrize('fmt', SUPPORTED_FORMATS)
def test_weather_has_no_stadium_recovery_adjustment(fmt):
    from engine.weather import generate_weather_script
    from engine import fc_weather
    seed_value = 314
    config = get_any_format(fmt)
    snapshot = resolve_conditions(neutral_profile(), 's', 1, config, get_defaults(fmt),
                                  True, {}, seed_value, forecast='rain_around')
    rng = random.Random(seed_value)
    if fmt == 'FC':
        expected = fc_weather.generate_weather_script('rain_around', config.days, config.overs_per_day,
            min_overs_last_hour=config.min_overs_last_hour, rng=rng, is_day_night=True)
        assert snapshot['fc_weather_script'] == expected
    else:
        assert snapshot['weather_script'] == generate_weather_script('rain_around', config.overs, fmt, rng=rng)


@pytest.mark.parametrize('fmt,helper', [('ListA', '_apply_dew_factor'), ('Hundred', '_apply_hundred_dew')])
def test_format_dew_runs_once_for_v2_and_is_bypassed_for_v1(fmt, helper, monkeypatch):
    import engine.ball_outcome as outcome
    from engine.match import Match
    from tests.test_bowler_consecutive_guards import _build_match_data
    calls = []
    original = getattr(outcome, helper)
    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(outcome, helper, spy)
    for version, expected in [('stadium_v2', 1), ('stadium_v1', 0)]:
        calls.clear()
        snapshot = conditions(fmt, night=True)
        if version == 'stadium_v1':
            snapshot.update(conditions_model_version=version,
                surface={'seam_residual': 1, 'spin_residual': 1, 'wear_rate_multiplier': 1},
                dew={'intensity': 0, 'start_ball': 0, 'peak_ball': 1})
        match = Match({**_build_match_data(fmt), 'is_day_night': True,
                       'conditions_model_version': version, 'conditions_snapshot': snapshot})
        match.next_ball()
        assert len(calls) == expected


def test_simple_form_and_obsolete_api_fields(app, qa):
    from bs4 import BeautifulSoup
    page = BeautifulSoup(qa.get('/stadiums/new').data, 'html.parser')
    fields = {node.get('name') for node in page.select('#stadium-editor input, #stadium-editor select, #stadium-editor textarea') if node.get('name')}
    assert fields == {'csrf_token', 'revision', 'format_settings', 'name', 'outfield_speed', 'average_temperature_c',
                      'straight_a', 'straight_b', 'square_a', 'square_b',
                      *(f'pitch_{fmt}' for fmt in SUPPORTED_FORMATS)}
    row = Stadium.query.first()
    for field in ('country_code', 'timezone', 'advanced_profile', 'climate_profile', 'description', 'city'):
        response = qa.patch(f'/api/stadiums/{row.id}', json={'revision': row.current_revision, 'profile': {field: 'obsolete'}})
        assert response.status_code == 400 and 'Unsupported' in response.json['error']
    assert qa.post(f'/api/stadiums/{row.id}/preview', json={'match_date': '2026-07-12'}).status_code == 400
    preview = qa.post(f'/api/stadiums/{row.id}/preview', json={'match_format': 'FC'})
    assert preview.status_code == 200
    assert preview.json['pitch'] == current_profile(row)['pitch_defaults']['FC']
    assert 'temperature_wear_multiplier' in preview.json
    bad = {node['name']: node.get('value', '') for node in page.select('#stadium-editor input[name]')}
    bad.update({f'pitch_{fmt}': 'Dry' for fmt in SUPPORTED_FORMATS}, name='Keep my inputs', outfield_speed='1.2')
    response = qa.post('/stadiums/new', data=bad)
    assert response.status_code == 400
    assert b'Keep my inputs' in response.data and b'value="1.2"' in response.data


def test_v1_migration_preserves_history_links_and_is_idempotent(tmp_path):
    from flask import Flask
    from types import SimpleNamespace
    from migrations.simplify_stadiums import run_migration, convert_v1_profile
    from engine.ground_config import get_pitch_options
    engine = create_engine('sqlite:///' + str(tmp_path / 'v1.db'))
    old = {
        'name': 'Old ground',
        'pitch_profile': {'preparation_weights': {fmt: {p['key']: 1 for p in get_pitch_options(fmt)} for fmt in SUPPORTED_FORMATS}},
        'geometry_profile': {key: {'min': 60, 'typical': 68+i, 'max': 90} for i,key in enumerate(neutral_profile()['dimensions_m'])},
        'outfield_profile': {'speed_multiplier': 1.04, 'recovery_multiplier': 1.2},
        'climate_profile': {'months': {str(i): {'temperature_c': {'typical': 15+i}} for i in range(1,13)}},
    }
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE stadiums (id TEXT PRIMARY KEY, current_revision INTEGER)'))
        conn.execute(text("INSERT INTO stadiums VALUES ('s', 3)"))
        conn.execute(text('CREATE TABLE stadium_revisions (stadium_id TEXT, revision INTEGER, name TEXT, pitch_profile JSON, geometry_profile JSON, outfield_profile JSON, climate_profile JSON)'))
        conn.execute(text('INSERT INTO stadium_revisions VALUES (:id, :revision, :name, :pitch, :geometry, :outfield, :climate)'),
                     {'id':'s', 'revision':3, 'name':old['name'], 'pitch':json.dumps(old['pitch_profile']),
                      'geometry':json.dumps(old['geometry_profile']), 'outfield':json.dumps(old['outfield_profile']),
                      'climate':json.dumps(old['climate_profile'])})
        for table in ('teams', 'tournament_fixtures', 'matches'):
            conn.execute(text(f'CREATE TABLE {table} (stadium_id TEXT, conditions_snapshot TEXT)'))
            conn.execute(text(f'INSERT INTO {table} VALUES (:id, :snapshot)'), {'id':'s', 'snapshot':'unchanged v1 snapshot'})
        history = tuple(conn.execute(text('SELECT * FROM stadium_revisions')).one())
    db_stub = SimpleNamespace(engine=engine)
    app = Flask('v1-conversion-test')
    run_migration(db_stub, app)
    run_migration(db_stub, app)
    expected = convert_v1_profile(old)
    assert expected['average_temperature_c'] == 21.5
    for fmt in SUPPORTED_FORMATS:
        assert expected['pitch_defaults'][fmt] == get_pitch_options(fmt)[0]['key']
    with engine.connect() as conn:
        assert conn.execute(text('SELECT current_revision FROM stadiums')).scalar() == 4
        assert conn.execute(text('SELECT count(*) FROM stadium_revisions_v2')).scalar() == 1
        simple = conn.execute(text('SELECT * FROM stadium_revisions_v2')).mappings().one()
        assert simple['schema_version'] == 2 and simple['average_temperature_c'] == 21.5
        assert json.loads(simple['dimensions_m']) == expected['dimensions_m']
        assert tuple(conn.execute(text('SELECT * FROM stadium_revisions')).one()) == history
        for table in ('teams', 'tournament_fixtures', 'matches'):
            assert tuple(conn.execute(text(f'SELECT * FROM {table}')).one()) == ('s', 'unchanged v1 snapshot')
    engine.dispose()


@pytest.mark.parametrize('fmt', ('T10', 'T20', 'ListA', 'Hundred'))
def test_temperature_wear_is_carried_into_tie_breakers(fmt, monkeypatch):
    from engine.match import Match
    from tests.test_bowler_consecutive_guards import _build_match_data
    snapshot = conditions(fmt, profile={**neutral_profile(), 'average_temperature_c': 32})
    match = Match({**_build_match_data(fmt), 'is_knockout': True, 'conditions_model_version':'stadium_v2',
                   'conditions_snapshot':snapshot})
    match.innings = 2
    match.stadium_completed_balls = snapshot['scheduled_innings_balls']
    match.innings_balls_bowled = snapshot['scheduled_innings_balls'] // 2
    before = match._compute_pitch_wear()
    monkeypatch.setattr(match, '_save_second_innings_stats', lambda: None)
    monkeypatch.setattr(match, '_generate_detailed_scorecard', lambda: {})
    match._setup_super_over()
    assert match.super_over_pitch_wear == before
    match.innings = 4  # The normal tied-match transition sets this before persisting.
    state = json.loads(json.dumps(match.serialize_super_over_snapshot()))
    resumed = Match(deepcopy(match.data))
    resumed.restore_super_over_snapshot(state)
    assert resumed.super_over_pitch_wear == before


def test_invalid_numeric_form_preserves_user_inputs(app, qa):
    form = {'name':'Correct this ground', **{f'pitch_{fmt}':'Dry' for fmt in SUPPORTED_FORMATS},
            'straight_a':70, 'straight_b':70, 'square_a':65, 'square_b':65,
            'outfield_speed':'bad', 'average_temperature_c':30}
    response = qa.post('/stadiums/new', data=form)
    assert response.status_code == 400
    assert b'value="Correct this ground"' in response.data
    assert b'value="bad"' in response.data
    assert b'value="Dry" selected' in response.data


def test_invalid_pitch_override_is_a_validation_error():
    with pytest.raises(ValueError, match='Pitch is not available'):
        conditions(overrides={'pitch': {'injected':True}})


def test_team_forms_select_stadiums_without_inline_creation(app, qa):
    from bs4 import BeautifulSoup
    from services.stadium_tuning import create_personal
    team = make_team(code='AUS')
    old = create_custom(ACCOUNTS['user1']['email'], neutral_profile('Old private ground'))
    venue = create_personal(ACCOUNTS['user1']['email'], neutral_profile('New format ground'))
    db.session.commit()
    for path in ('/team/create', '/team/AUS/edit'):
        response = qa.get(path)
        assert response.status_code == 200
        page = BeautifulSoup(response.data, 'html.parser')
        selector = page.select_one('#tc-stadium')
        values = [option.get('value') for option in selector.select('option')]
        assert venue.id in values and old.id not in values and '__custom__' not in values
        assert not page.select_one('[name="custom_stadium_name"]')
        assert page.select_one('.tc-stadium-heading a')['href'] == '/stadiums'
        assert page.select_one('#tc-ground').has_attr('readonly')
        assert page.select_one('#tc-pitch').has_attr('disabled')


def test_edit_team_cannot_create_or_modify_a_legacy_ground(app, qa):
    from tests.test_team_routes import _valid_team_form
    team = populated_team('AUS')
    form = _valid_team_form('Existing team AUS', 'AUS')
    form.update(home_ground=team.home_ground, pitch_preference=team.pitch_preference,
                stadium_id='__custom__', custom_stadium_name='Forbidden inline ground')
    before = Stadium.query.count()
    response = qa.post('/team/AUS/edit', data=form)
    assert b'Create your stadium on the Stadiums page' in response.data
    assert Stadium.query.count() == before
    form.update(stadium_id='', home_ground='Changed text ground')
    response = qa.post('/team/AUS/edit', data=form)
    assert b'To change your ground' in response.data
    db.session.refresh(team)
    assert team.home_ground == 'Original ground' and team.stadium_id is None
