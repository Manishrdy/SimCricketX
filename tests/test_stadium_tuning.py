"""v3 isolation, captured baselines, legal-ball phases and old-model compatibility."""
from copy import deepcopy
import json
import random

import pytest
from sqlalchemy import create_engine, text
from flask import Flask
from types import SimpleNamespace

from database import db
from database.models import Stadium, StadiumRevision, StadiumRevisionV3, StadiumAdoption, UserGroundConfig
from engine.format_catalog import SUPPORTED_FORMATS
from engine.format_config import get_any_format
from engine.ground_config import get_defaults, get_effective_config
from engine.stadium_conditions import resolve_conditions, public_match_data
from engine.stadium_tuning import apply_adjustments, legal_ball_phase, phase_ranges
from services.stadiums import neutral_profile, current_profile, create_custom, seed_catalogue
from services.stadium_tuning import default_settings, create_personal, validate_settings
from scripts.dev_test_accounts import seed, logged_in_client, ACCOUNTS


@pytest.fixture
def qa(app):
    seed(app)
    seed_catalogue()
    return logged_in_client(app, 'user1')


def baseline():
    return {fmt: get_defaults(fmt, mutable=True) for fmt in SUPPORTED_FORMATS}


def snapshot(fmt='T20', settings=None, configs=None):
    configs = configs or baseline()
    profile = {**neutral_profile(), 'format_settings': settings or default_settings(configs)}
    return resolve_conditions(profile, 'private', 1, get_any_format(fmt), {}, False, {}, 42,
                              baseline_configs=configs, baseline_provenance={})


def test_copies_are_private_idempotent_and_capture_user_tuning(app, qa):
    other = logged_in_client(app, 'user2')
    admin = logged_in_client(app, 'admin')
    official = Stadium.query.filter_by(kind='official').first()
    source_revision = official.current_revision
    row = UserGroundConfig(user_id=ACCOUNTS['user1']['email'], match_format='FC',
        config_json={'blending': {'pitch_weight': .27, 'skill_weight': .73}})
    db.session.add(row); db.session.commit()
    path = f'/api/stadiums/{official.id}/adopt'
    first = qa.post(path, json={'revision': source_revision})
    second = other.post(path, json={'revision': source_revision})
    assert first.status_code == second.status_code == 200
    a, b = first.json, second.json
    assert a['id'] != b['id'] != official.id
    assert a['schema_version'] == b['schema_version'] == 3
    assert a['format_settings']['FC']['blending']['pitch_weight'] == .27
    assert b['format_settings']['FC']['blending']['pitch_weight'] == get_defaults('FC')['blending']['pitch_weight']
    assert not {'baseline_configs', 'baseline_provenance'} & a.keys()
    assert qa.post(path, json={'revision': source_revision}).json['id'] == a['id']
    assert StadiumAdoption.query.count() == 2
    assert other.get('/api/stadiums/' + a['id']).status_code == 404
    assert admin.get('/api/stadiums/' + a['id']).status_code == 404
    for client in (other, admin):
        assert client.patch('/api/stadiums/' + a['id'], json={'revision':1,'profile':{'name':'Stolen'}}).status_code == 400
        assert client.post('/api/stadiums/' + a['id'] + '/preview', json={}).status_code == 400
        assert client.post('/api/stadiums/' + a['id'] + '/retire').status_code == 404
    assert qa.patch('/api/stadiums/' + official.id, json={'revision':source_revision,'profile':{'name':'Global'}}).status_code == 400
    revision = db.session.get(StadiumRevisionV3, (a['id'],1))
    old = deepcopy(revision.baseline_configs)
    row.config_json = {'blending':{'pitch_weight':.5,'skill_weight':.5}}; db.session.commit()
    assert revision.baseline_configs == old
    assert qa.patch('/api/stadiums/' + a['id'], json={'revision':1,'profile':{'name':'Changed'}}).status_code == 200
    assert qa.post(path, json={'revision':source_revision}).json['id'] == a['id']
    assert db.session.get(StadiumRevisionV3, (a['id'],2)).baseline_configs == old
    assert other.get('/api/stadiums/' + b['id']).json['name'] == b['name']
    assert qa.post('/api/stadiums/' + a['id'] + '/retire').status_code == 200
    assert other.post('/api/stadiums/' + b['id'] + '/preview', json={}).status_code == 200
    assert qa.post(path, json={'revision':source_revision}).status_code == 400


def test_private_adoption_preserves_v2_and_retry(app, qa):
    venue = create_custom(ACCOUNTS['user1']['email'], neutral_profile())
    db.session.commit()
    old = deepcopy(current_profile(venue)); ident = venue.id
    response = qa.post(f'/api/stadiums/{ident}/adopt', json={'revision':1})
    assert response.status_code == 200
    assert response.json['id'] == ident and response.json['revision'] == 2
    assert db.session.get(StadiumRevision,(ident,1)).name == old['name']
    assert qa.post(f'/api/stadiums/{ident}/adopt',json={'revision':1}).json['revision'] == 2
    assert StadiumRevisionV3.query.filter_by(stadium_id=ident).count() == 1
    assert qa.post(f'/api/stadiums/{ident}/adopt',json={'revision':False}).status_code == 400
    assert qa.post(f'/api/stadiums/{ident}/adopt',json={'revision':99}).status_code == 400


@pytest.mark.parametrize('fmt', SUPPORTED_FORMATS)
def test_api_zero_roundtrip_and_preview(app, qa, fmt):
    configs = baseline(); settings = default_settings(configs)
    settings[fmt]['blending'] = {'pitch_weight':0,'skill_weight':1}
    settings[fmt]['pitches']['Hard'] = dict.fromkeys(('scoring','wicket','pace','spin'),0)
    if fmt != 'FC':
        settings[fmt]['phases']['middle'].update(scoring=0,boundary=0,wicket=0)
        settings[fmt]['phases']['middle']['chasing'].update(scoring=0,wicket=0)
    response = qa.post('/api/stadiums',json={**neutral_profile(), 'format_settings':settings})
    assert response.status_code == 201
    ident=response.json['id']
    assert response.json['format_settings'] == settings
    preview=qa.post(f'/api/stadiums/{ident}/preview',json={'match_format':fmt})
    assert preview.status_code == 200
    assert preview.json['format_settings'] == settings[fmt]
    assert preview.json['phase_ranges'] == {k:list(v) for k,v in phase_ranges(get_any_format(fmt)).items()}
    if fmt == 'ListA':
        short=qa.post(f'/api/stadiums/{ident}/preview',json={'match_format':fmt,'scheduled_overs':40})
        assert short.json['phase_ranges']['powerplay']==[0,48]
    assert qa.patch(f'/api/stadiums/{ident}',json={'revision':1,'profile':{'format_settings':settings}}).status_code==200
    assert qa.patch(f'/api/stadiums/{ident}',json={'revision':1,'profile':{'name':'Stale'}}).status_code==400
    for payload in ({'baseline_configs':configs}, {'baseline_provenance':{}}, {'unknown':1}):
        assert qa.patch(f'/api/stadiums/{ident}',json={'revision':2,'profile':payload}).status_code==400


@pytest.mark.parametrize('mutate', [
    lambda s:s['FC'].update(phases={}),
    lambda s:s['T20']['blending'].update(pitch_weight=.5),
    lambda s:s['T20']['blending'].update(pitch_weight=True),
    lambda s:s['T20']['pitches']['Hard'].update(scoring=float('nan')),
    lambda s:s['T20']['pitches']['Hard'].update(wicket=3.01),
    lambda s:s['T20']['phases']['middle']['chasing'].update(wicket=-.1),
    lambda s:s['Hundred']['phases'].update(new_phase={}),
    lambda s:s['T10']['pitches'].pop('Green'),
])
def test_invalid_settings(mutate):
    configs=baseline();settings=default_settings(configs);mutate(settings)
    with pytest.raises(ValueError):validate_settings(settings, configs)


@pytest.mark.parametrize('fmt,pp,death,length', [('Hundred',25,80,None),('T20',36,96,None),('T10',18,42,None),('ListA',60,240,50),('ListA',48,192,40)])
def test_phase_boundaries(fmt,pp,death,length):
    rules=get_any_format(fmt,scheduled_overs=length)
    assert legal_ball_phase(rules,0)=='powerplay'
    assert legal_ball_phase(rules,pp-1)=='powerplay'
    assert legal_ball_phase(rules,pp)=='middle'
    assert legal_ball_phase(rules,death-1)=='middle'
    assert legal_ball_phase(rules,death)=='death'
    assert legal_ball_phase(rules,rules.overs*rules.balls_per_over-1)=='death'
    with pytest.raises(ValueError):legal_ball_phase(rules,rules.overs*rules.balls_per_over)


@pytest.mark.parametrize('sets', range(5,21))
def test_hundred_reduced_powerplay_inside_set(sets):
    rules=get_any_format('Hundred');rules.revise_short_innings(sets)
    pp=rules.powerplay_balls
    assert legal_ball_phase(rules,pp-1)=='powerplay'
    assert legal_ball_phase(rules,pp) != 'powerplay'
    assert phase_ranges(rules)['death'][0] == max(pp, sets*5-20)
    # Illegal deliveries repeat the same legal-ball count/phase.
    assert legal_ball_phase(rules,pp-1)==legal_ball_phase(rules,pp-1)


@pytest.mark.parametrize('fmt', SUPPORTED_FORMATS)
def test_neutral_adjustments_and_snapshot_independence(fmt):
    configs=baseline();settings=default_settings(configs);snap=snapshot(fmt,settings,configs)
    weights={key:1.0 for key in ('Dot','Single','Double','Three','Four','Six','Wicket','Extras')}
    assert apply_adjustments(weights,snap,'Fast',get_any_format(fmt),0)==weights
    assert snap['ground_config']==configs[fmt]
    configs[fmt]['blending']['pitch_weight']=.77
    settings[fmt]['pitches']['Hard']['scoring']=2
    assert snap['format_settings']['pitches']['Hard']['scoring']==1
    assert snap['ground_config']['blending']['pitch_weight']!=.77
    public=public_match_data({'conditions_snapshot':snap,'ground_config':snap['ground_config']})
    assert 'ground_config' not in public
    assert not {'conditions_seed','format_settings','ground_config','baseline_provenance'} & public['conditions_snapshot'].keys()


def test_multipliers_compound_once_and_tiebreakers_omit_phases():
    snap=snapshot();pitch=snap['format_settings']['pitches']['Hard'];pitch.update(scoring=2,wicket=2,pace=1.5,spin=.5)
    phase=snap['format_settings']['phases']['powerplay'];phase.update(scoring=1.5,boundary=2,wicket=.5)
    phase['chasing'].update(scoring=.5,wicket=2)
    weights={key:1 for key in ('Dot','Single','Double','Three','Four','Six','Wicket','Extras')}
    rules=get_any_format('T20')
    result=apply_adjustments(weights,snap,'Fast',rules,0,2)
    assert result['Single']==1.5 and result['Four']==3 and result['Wicket']==3
    assert result['Dot']==result['Extras']==1
    assert apply_adjustments(weights,snap,'Off spin',rules,0,2)['Wicket']==1
    assert apply_adjustments(weights,snap,'unknown',rules,0,2)['Wicket']==2
    assert apply_adjustments(weights,snap,'Medium',rules,0,2)['Wicket']==2
    tied=apply_adjustments(weights,snap,'Fast',tie_breaker=True)
    assert tied['Single']==tied['Four']==2 and tied['Wicket']==3
    assert weights['Four']==1


def test_creation_flag_does_not_disable_snapshot_play(app,qa):
    official=Stadium.query.filter_by(kind='official').first()
    app.config['STADIUM_V3_ENABLED']=False
    assert qa.post('/api/stadiums',json=neutral_profile()).status_code==400
    assert qa.post(f'/api/stadiums/{official.id}/adopt',json={'revision':official.current_revision}).status_code==400
    weights={'Four':1,'Wicket':1}
    assert apply_adjustments(weights,snapshot(), 'Fast',get_any_format('T20'),0)==weights


def test_retired_editor_redirects_and_never_writes(app,qa):
    before=UserGroundConfig.query.count()
    assert qa.get('/ground-conditions').status_code==302
    assert qa.get('/ground-conditions/guide').status_code==302
    assert qa.get('/stadiums/conditions-guide').status_code==200
    for path in ('save','reset','mode'):
        assert qa.post('/ground-conditions/'+path,json={'match_format':'T20'}).status_code==410
    assert UserGroundConfig.query.count()==before
    assert b'/ground-conditions' not in qa.get('/').data
    assert b'href="/ground-conditions"' not in qa.get('/match/setup').data


def test_additive_migration_idempotent(tmp_path):
    from migrations.add_stadium_tuning import run_migration
    engine=create_engine('sqlite:///'+str(tmp_path/'old.db'))
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE users (id VARCHAR(120) PRIMARY KEY)'))
        conn.execute(text('CREATE TABLE stadiums (id VARCHAR(36) PRIMARY KEY, current_revision INTEGER)'))
        conn.execute(text("INSERT INTO stadiums VALUES ('old',7)"))
        conn.execute(text('CREATE TABLE matches (conditions_snapshot TEXT)'))
        conn.execute(text("INSERT INTO matches VALUES ('original')"))
    app=Flask('migration');database=SimpleNamespace(engine=engine)
    run_migration(database,app);run_migration(database,app)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT * FROM stadiums')).one()==('old',7,2)
        assert conn.execute(text('SELECT * FROM matches')).scalar()=='original'
        assert conn.execute(text('SELECT count(*) FROM stadium_revisions_v3')).scalar()==0
    engine.dispose()


@pytest.mark.parametrize('fmt', SUPPORTED_FORMATS)
def test_v3_neutral_play_matches_v2_and_resume(monkeypatch, fmt):
    from engine.match import Match
    from tests.test_bowler_consecutive_guards import _build_match_data
    monkeypatch.setattr('engine.match.print', lambda *args,**kwargs:None)
    base=_build_match_data(fmt);base['created_at']=1
    configs=baseline()
    if fmt == 'T20':
        configs[fmt]['phase_boosts']['powerplay']['overs_end'] = 1
        configs[fmt]['phase_boosts']['death_overs']['overs_start'] = 18
    old=resolve_conditions(neutral_profile(),'private',1,get_any_format(fmt),configs[fmt],False,{},42)
    new=snapshot(fmt, configs=configs)
    sequences=[]
    for conditions in (old,new):
        random.seed(654)
        match=Match({**deepcopy(base),'conditions_model_version':conditions['conditions_model_version'],
                     'conditions_snapshot':deepcopy(conditions)})
        results=[match.next_ball() for _ in range(35)]
        assert all('error' not in result for result in results)
        sequences.append((match.score,match.wickets,match.current_over,match.current_ball,
                          [(r.get('runs'),r.get('wicket'),r.get('outcome')) for r in results]))
    assert sequences[0]==sequences[1]
    if fmt=='Hundred':
        from engine.hundred_snapshot import serialize,restore
        state=json.loads(json.dumps(serialize(match)))
        resumed=Match({**deepcopy(base),'conditions_model_version':'stadium_v3','conditions_snapshot':deepcopy(new)})
        restore(resumed,state)
        assert resumed.ground_config['_stadium']['format_settings']==new['format_settings']
        for _ in range(10):
            assert match.next_ball()==resumed.next_ball()
    elif fmt=='FC':
        state=json.loads(json.dumps(match.serialize_fc_snapshot()))
        resumed=Match({**deepcopy(base),'conditions_model_version':'stadium_v3','conditions_snapshot':deepcopy(new)})
        resumed.restore_fc_snapshot(state)
        assert resumed._compute_pitch_wear()==match._compute_pitch_wear()
        assert resumed.ground_config['_stadium']['format_settings']==new['format_settings']


@pytest.mark.parametrize('fmt', SUPPORTED_FORMATS)
def test_setup_uses_baseline_not_current_user_config(app,qa,fmt,monkeypatch):
    from tests.test_stadiums import populated_team,read_created
    from engine.match import Match
    configs=baseline()
    settings=default_settings(configs)
    settings[fmt]['blending']={'pitch_weight':.77,'skill_weight':.23}
    response=qa.post('/api/stadiums',json={**neutral_profile(), 'format_settings':settings})
    venue=db.session.get(Stadium,response.json['id'])
    home=populated_team('HOM','stadium',venue.id);away=populated_team('AWY')
    db.session.add(UserGroundConfig(user_id=ACCOUNTS['user1']['email'],match_format=fmt,
        config_json={'blending':{'pitch_weight':.01,'skill_weight':.99}}));db.session.commit()
    data,path=read_created(qa.post('/match/setup',json={'team_home':home.id,'team_away':away.id,
        'match_format':fmt,'pitch':'Hard','toss':'Heads',
        'conditions_snapshot':{'conditions_model_version':'stadium_v2'},'conditions_model_version':'legacy'}))
    try:
        assert data['conditions_model_version']=='stadium_v3'
        assert data['ground_config']['blending']==settings[fmt]['blending']
        snap=deepcopy(data['conditions_snapshot'])
        assert qa.patch('/api/stadiums/'+venue.id,json={'revision':1,'profile':{'name':'Edited'}}).status_code==200
        match=Match(data)
        assert match.ground_config['blending']==settings[fmt]['blending']
        assert match.ground_config['_stadium']==snap
        monkeypatch.setattr('engine.match.print',lambda *a,**k:None)
        assert 'error' not in match.next_ball()
        app.config['STADIUM_V3_ENABLED']=False
        assert qa.post('/match/setup',json={'team_home':home.id,'team_away':away.id,'match_format':fmt}).status_code==503
        assert 'error' not in match.next_ball()
    finally:path.unlink(missing_ok=True)


def test_editor_has_all_controls_and_keeps_invalid_inputs(app,qa):
    from bs4 import BeautifulSoup
    page=BeautifulSoup(qa.get('/stadiums/new').data,'html.parser')
    panels=page.select('.tuning-format-panel')
    assert {p['data-format'] for p in panels}==set(SUPPORTED_FORMATS)
    fc=page.select_one('[data-format="FC"].tuning-format-panel')
    assert not fc.select('.tuning-phase')
    assert len(page.select('[data-tuning-path]'))==5*5*4+4*3*5
    fields={node['name']:node.get('value','') for node in page.select('#stadium-editor input[name]')}
    fields.update({f'pitch_{fmt}':'Hard' for fmt in SUPPORTED_FORMATS})
    settings=json.loads(fields['format_settings']);settings['Hundred']['pitches']['Hard']['spin']=0
    fields.update(name='QA preserved input',format_settings=json.dumps(settings),outfield_speed=1.2)
    invalid=qa.post('/stadiums/new',data=fields)
    assert invalid.status_code==400
    rendered=BeautifulSoup(invalid.data,'html.parser')
    assert json.loads(rendered.select_one('#format-settings')['value'])==settings
    assert rendered.select_one('input[name="name"]')['value']=='QA preserved input'
    fields['outfield_speed']=1
    saved=qa.post('/stadiums/new',data=fields)
    assert saved.status_code==302
    row=Stadium.query.filter_by(kind='custom').one()
    assert current_profile(row)['format_settings']['Hundred']['pitches']['Hard']['spin']==0


def test_rejected_creation_and_selection_cannot_inject_baselines(app,qa):
    from tests.test_stadiums import populated_team
    original=Stadium.query.filter_by(kind='official').first()
    payload={'revision':original.current_revision,'baseline_configs':baseline()}
    assert qa.post('/api/stadiums/'+original.id+'/adopt',json=payload).status_code==400
    assert qa.post('/api/stadiums',json={**neutral_profile(),'baseline_configs':baseline()}).status_code==400
    assert qa.post('/api/stadiums',json={**neutral_profile(),'baseline_provenance':{}}).status_code==400
    home=populated_team('HOM');away=populated_team('AWY')
    assert qa.post('/api/team/'+str(home.id)+'/stadium',json={'stadium_id':original.id}).status_code==400
    assert qa.post('/match/setup',json={'team_home':home.id,'team_away':away.id,'stadium_id':original.id}).status_code==400
    assert qa.post('/team/create',data={'team_name':'No global venue','short_code':'GLO','stadium_id':original.id}).status_code==400
    other=logged_in_client(app,'user2')
    private=other.post('/api/stadiums',json=neutral_profile()).json
    assert qa.post('/match/setup',json={'team_home':home.id,'team_away':away.id,'stadium_id':private['id']}).status_code==400


@pytest.mark.parametrize('fmt', ['T20','Hundred'])
def test_v3_tiebreaker_checkpoint_retains_tuning_and_wear(fmt,monkeypatch):
    from engine.match import Match
    from tests.test_bowler_consecutive_guards import _build_match_data
    monkeypatch.setattr('engine.match.print',lambda *a,**k:None)
    snap=snapshot(fmt)
    snap['format_settings']['pitches']['Hard'].update(scoring=1.2,wicket=.8)
    # These intentionally extreme phases must not apply to the tie breaker.
    for phase in snap['format_settings']['phases'].values():
        phase.update(scoring=0,boundary=0,wicket=0)
    data={**_build_match_data(fmt),'is_knockout':True,'created_at':1,
          'conditions_model_version':'stadium_v3','conditions_snapshot':snap}
    match=Match(deepcopy(data));match.innings=4
    match.innings_balls_bowled=snap['scheduled_innings_balls'];match.stadium_completed_balls=snap['scheduled_innings_balls']
    match._setup_super_over();assert match.start_super_over('home').get('super_over_started')
    match.next_super_over_ball()
    state=json.loads(json.dumps(match.serialize_super_over_snapshot()))
    resumed=Match(deepcopy(data));resumed.restore_super_over_snapshot(state)
    assert resumed.ground_config['_stadium']['format_settings']==snap['format_settings']
    assert resumed.super_over_pitch_wear==match.super_over_pitch_wear
    if fmt=='Hundred':
        # Full Hundred checkpoint includes its independent random stream.
        from engine.hundred_snapshot import serialize,restore
        state=json.loads(json.dumps(serialize(match)))
        restore(resumed,state)
        assert resumed.next_super_over_ball()==match.next_super_over_ball()
    else:
        assert 'error' not in resumed.next_super_over_ball()


def test_official_updates_follow_unedited_copies_and_preserve_overrides(app, qa):
    other = logged_in_client(app, 'user2')
    admin = logged_in_client(app, 'admin')
    source = Stadium.query.filter_by(kind='official').first()
    path = f'/api/stadiums/{source.id}'
    original = deepcopy(current_profile(source))
    inherited = qa.post(path + '/adopt', json={'revision': source.current_revision}).json
    overridden = other.post(path + '/adopt', json={'revision': source.current_revision}).json
    # A private save marks the whole configuration as a user override.
    private_settings = deepcopy(overridden['format_settings'])
    private_settings['T20']['pitches']['Hard']['spin'] = 0
    edit = other.patch('/api/stadiums/' + overridden['id'], json={'revision': 1,
        'profile': {'average_temperature_c': 17, 'format_settings': private_settings}})
    assert edit.status_code == 200
    saved_private = deepcopy(edit.json)
    initial_copy = db.session.get(StadiumRevisionV3, (inherited['id'], 1))
    old_settings = deepcopy(initial_copy.format_settings)
    global_settings = default_settings(baseline())
    global_settings['T20']['pitches']['Hard']['scoring'] = 1.3
    changed = admin.patch(path, json={'revision': source.current_revision,
        'profile': {'average_temperature_c': 31, 'format_settings': global_settings}})
    assert changed.status_code == 200
    assert changed.json['schema_version'] == 3
    following = qa.get('/api/stadiums/' + inherited['id']).json
    assert following['revision'] == 2
    assert following['average_temperature_c'] == 31
    assert following['format_settings'] == global_settings
    assert other.get('/api/stadiums/' + overridden['id']).json == saved_private
    assert db.session.get(StadiumRevision, (source.id, 1)).average_temperature_c == original['average_temperature_c']
    assert initial_copy.format_settings == old_settings
    # Further official saves append revisions and never overwrite private revisions.
    latest = admin.patch(path, json={'revision': changed.json['revision'], 'profile': {'outfield_speed': 1.08}})
    assert latest.status_code == 200
    following = qa.get('/api/stadiums/' + inherited['id']).json
    assert following['revision'] == 3 and following['outfield_speed'] == 1.08
    assert other.get('/api/stadiums/' + overridden['id']).json == saved_private
    # Re-adding after an official update reuses the same personal venue/team link.
    again = qa.post(path + '/adopt', json={'revision': latest.json['revision']})
    assert again.status_code == 200 and again.json['id'] == inherited['id']
    assert StadiumAdoption.query.filter_by(user_id=ACCOUNTS['user1']['email'], source_stadium_id=source.id).count() == 1
    newcomer = logged_in_client(app, 'user3')
    new_copy = newcomer.post(path + '/adopt', json={'revision': latest.json['revision']}).json
    assert new_copy['average_temperature_c'] == 31 and new_copy['format_settings'] == global_settings
    # Official v3 venues still require adding to My stadiums before new selection.
    assert qa.post('/team/create', data={'team_name': 'No direct official', 'short_code': 'NDO', 'stadium_id': source.id}).status_code == 400
    # Revision checking and permission checks cover the updated official model too.
    assert qa.patch(path, json={'revision': latest.json['revision'], 'profile': {'name': 'Unauthorized'}}).status_code == 400
    assert admin.patch(path, json={'revision': changed.json['revision'], 'profile': {'name': 'Stale'}}).status_code == 400


def test_catalogue_add_action_and_admin_official_editor(app, qa):
    from bs4 import BeautifulSoup
    admin = logged_in_client(app, 'admin')
    source = Stadium.query.filter_by(kind='official').first()
    page = BeautifulSoup(qa.get('/stadiums').data, 'html.parser')
    form = page.select_one(f'form[action="/stadiums/{source.id}/add"]')
    assert form and form['method'] == 'post'
    assert form['data-adopt-url'] == f'/api/stadiums/{source.id}/adopt'
    assert form.select_one('input[name="revision"]')['value'] == str(source.current_revision)
    detail = BeautifulSoup(qa.get('/stadiums/' + source.id).data, 'html.parser')
    assert not detail.select_one('#stadium-editor')
    admin_detail = BeautifulSoup(admin.get('/stadiums/' + source.id).data, 'html.parser')
    editor = admin_detail.select_one('#stadium-editor')
    assert editor and editor.select_one('[name="format_settings"]')
    # Save through the HTML editor, which upgrades official v2 tuning to v3.
    fields = {node['name']: node.get('value', '') for node in editor.select('input[name]')}
    fields.update({f'pitch_{fmt}': source_pitch for fmt, source_pitch in current_profile(source)['pitch_defaults'].items()})
    fields['average_temperature_c'] = '29'
    assert admin.post('/stadiums/' + source.id, data=fields).status_code == 302
    source = db.session.get(Stadium, source.id)
    assert source.current_profile_version == 3
    assert current_profile(source)['average_temperature_c'] == 29
    # The no-JS form performs adoption and returns to My stadiums, never to detail.
    response = qa.post(f'/stadiums/{source.id}/add', data={'revision': source.current_revision})
    assert response.status_code == 302 and response.location.endswith('/stadiums?kind=custom')
    page = BeautifulSoup(qa.get('/stadiums').data, 'html.parser')
    button = page.select_one(f'form[action="/stadiums/{source.id}/add"] button')
    assert button.has_attr('disabled') and button.get_text() == 'Added to My stadiums'
    detail = BeautifulSoup(qa.get('/stadiums/' + source.id).data, 'html.parser')
    assert detail.select_one('#adopt-stadium')
    assert not detail.select_one('.stadium-actions a')


def test_official_update_changes_future_matches_only(app, qa):
    from tests.test_stadiums import populated_team, read_created
    from services.stadiums import BOUNDARIES
    admin = logged_in_client(app, 'admin')
    source = Stadium.query.filter_by(kind='official').first()
    copied = qa.post(f'/api/stadiums/{source.id}/adopt', json={'revision': source.current_revision}).json
    home = populated_team('HOM', 'stadium', copied['id'])
    away = populated_team('AWY')
    payload = {'team_home': home.id, 'team_away': away.id, 'match_format': 'T20', 'pitch': 'Hard', 'toss': 'Heads'}
    before, before_path = read_created(qa.post('/match/setup', json=payload))
    frozen = before_path.read_bytes()
    later_path = None
    try:
        admin_response = admin.patch(f'/api/stadiums/{source.id}', json={'revision': source.current_revision,
            'profile': {'average_temperature_c': 37, 'dimensions_m': dict.fromkeys(BOUNDARIES, 85)}})
        assert admin_response.status_code == 200
        after, later_path = read_created(qa.post('/match/setup', json=payload))
        assert after['conditions_snapshot']['average_temperature_c'] == 37
        assert after['conditions_snapshot']['geometry_m'] == dict.fromkeys(BOUNDARIES, 85)
        assert after['stadium_id'] == before['stadium_id'] == copied['id']
        assert after['stadium_revision'] == before['stadium_revision'] + 1
        assert before_path.read_bytes() == frozen
    finally:
        before_path.unlink(missing_ok=True)
        if later_path:
            later_path.unlink(missing_ok=True)


def test_unchanged_personal_save_keeps_following_official_updates(app, qa):
    admin = logged_in_client(app, 'admin')
    source = Stadium.query.filter_by(kind='official').first()
    copied = qa.post(f'/api/stadiums/{source.id}/adopt', json={'revision': source.current_revision}).json
    response = qa.patch('/api/stadiums/' + copied['id'], json={'revision': 1, 'profile': {}})
    assert response.status_code == 200
    changed = admin.patch(f'/api/stadiums/{source.id}', json={'revision': source.current_revision,
        'profile': {'average_temperature_c': 34}})
    assert changed.status_code == 200
    assert qa.get('/api/stadiums/' + copied['id']).json['average_temperature_c'] == 34


@pytest.mark.parametrize('fmt', ['T10', 'T20', 'ListA', 'Hundred'])
def test_v3_extras_at_innings_limit_use_legal_progress(monkeypatch, fmt):
    """Extra deliveries must not exhaust phase budgets or strand a reload."""
    import engine.match as match_module
    from services.match_delivery import MatchDelivery
    from tests.test_bowler_consecutive_guards import _build_match_data

    monkeypatch.setattr(match_module, 'print', lambda *args, **kwargs: None)
    match = match_module.Match({**_build_match_data(fmt),
        'conditions_model_version': 'stadium_v3', 'conditions_snapshot': snapshot(fmt)})
    total = match.overs * match.balls_per_over
    match.current_over = match.overs - 1
    match.current_ball = match.balls_per_over - 1
    match.current_bowler = match.bowling_team[0]
    match.bowler_selected_for_over = match.current_over
    # Reproduce the stuck live state: wear has reached the budget while
    # the scoreboard still has one legal ball to bowl.
    match.innings_balls_bowled = total
    original = match_module.calculate_outcome
    seen = []
    outcomes = iter(['Wide', 'No Ball', None, None])

    def calculate(**kwargs):
        seen.append(kwargs['legal_balls_completed'])
        original(**kwargs)  # Run real stadium phase validation and adjustments.
        extra = next(outcomes)
        return {'runs': 1 if extra else 0, 'batter_out': False,
                'is_extra': bool(extra), 'extra_type': extra,
                'description': extra or 'Defended.'}

    monkeypatch.setattr(match_module, 'calculate_outcome', calculate)
    guard = MatchDelivery()
    for _ in range(3):
        token = guard.recover()['delivery_token']
        payload, error = guard.advance(token, lambda: (match.next_ball(), None))
        assert error is None
        assert guard.recover(token)['delivery'] == payload
    assert seen and set(seen) == {total - 1}
    assert match.current_over * match.balls_per_over + match.current_ball == total
    assert match.next_ball().get('innings_end')
