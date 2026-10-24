"""Network policy checks use shared QA accounts and isolated Flask databases."""
from datetime import timedelta
import json
from unittest.mock import patch

import pytest
from sqlalchemy import select, insert, update, delete
from flask import session

from database import db
from database.models import User, ActiveSession, IdentityNetworkAppeal as Appeal
from services import identity_network as network
from utils import network_classifier as classifier
from scripts.dev_test_accounts import ACCOUNTS, logged_in_client
from migrations.add_identity_network import MODELS, run_migration

IP = '8.8.8.8'


def result(state='flagged', categories=('vpn',)):
    at=classifier.now()
    return classifier.Classification(state,categories if state=='flagged' else (), 'proxycheck',at,at+timedelta(minutes=5))


def payload(**flags):
    return {'status':'ok',IP:{'detections':{k:flags.get(k,False) for k in classifier.FLAGS},'network':{'type':'Residential'}}}


@pytest.fixture
def configured(app,admin_client,monkeypatch):
    app.config.update(IDENTITY_NETWORK_ENFORCE=True,IDENTITY_SIGNUP_KEY='s'*40,PROXYCHECK_API_KEY='QA-NO-LIVE-KEY')
    monkeypatch.setattr(classifier,'provider',lambda ip,at: result())
    return app


def call(app,client,path,method='get',**kwargs):
    # A fresh app context prevents Flask-Login's test-context cache leaking
    # between independent clients under the repository's long-lived fixture.
    with app.app_context():
        return getattr(client,method)(path,environ_overrides={'REMOTE_ADDR':IP},**kwargs)


def member(app,key='user1'):
    with app.app_context():
        return logged_in_client(app,key)


def user(key='user1'):
    return db.session.get(User,ACCOUNTS[key]['email'])


@pytest.mark.parametrize('flag',['vpn','proxy','tor','hosting','anonymous'])
def test_parser_explicit_detection_categories(flag):
    actual=classifier.parse_response(payload(**{flag:True}),IP,classifier.now())
    assert actual.state=='flagged' and flag in actual.categories


def test_parser_business_is_not_vpn_and_hosting_is_blocked():
    body=payload();body[IP]['network']['type']='Business'
    assert classifier.parse_response(body,IP,classifier.now()).state=='clean'
    body[IP]['network']['type']='Hosting'
    assert classifier.parse_response(body,IP,classifier.now()).categories==('hosting',)


@pytest.mark.parametrize('body',[None,{}, {'status':'error'}, {'status':'ok',IP:{}}, {'status':'ok',IP:{'detections':{'vpn':'yes'}}}])
def test_malformed_provider_is_unknown(body):
    assert classifier.parse_response(body,IP,classifier.now()).state=='unknown'


def test_null_network_and_bad_booleans_unknown():
    body=payload();body[IP]['network']['type']=None
    assert classifier.parse_response(body,IP,classifier.now()).state=='unknown'
    body=payload();body[IP]['detections']['vpn']=1
    assert classifier.parse_response(body,IP,classifier.now()).state=='unknown'


def test_delist_boundary_limits_cache():
    at=classifier.now()
    body=payload(vpn=True)
    body[IP]['detection_history']={'anonymous':{'delisted':False,'date':(at+timedelta(seconds=20)).isoformat()+'Z'}}
    assert classifier.parse_response(body,IP,at).expires_at==at+timedelta(seconds=20)
    body[IP]['detection_history']['anonymous']['date']=at.isoformat()+'Z'
    assert classifier.parse_response(body,IP,at).state=='unknown'


def test_provider_sends_only_ip_key_and_no_logging_tags(configured):
    # Call original implementation despite the fixture's outbound-provider stub.
    import importlib
    # unwrap through a saved reference captured at module import below
    with patch.object(classifier,'_download',return_value=json.dumps(payload()).encode()) as download:
        assert REAL_PROVIDER(IP,classifier.now()).state=='clean'
    url=download.call_args.args[0];params=download.call_args.kwargs['params']
    assert url=='https://proxycheck.io/v3/'+IP
    assert params=={'key':'QA-NO-LIVE-KEY','tag':'0','ver':'24-June-2026'}


REAL_PROVIDER=classifier.provider


@pytest.mark.parametrize('exc',[TimeoutError(),ValueError(),classifier.requests.ConnectionError()])
def test_provider_failures_are_unknown_without_secret_output(configured,exc,caplog):
    with patch.object(classifier,'_download',side_effect=exc):
        assert REAL_PROVIDER(IP,classifier.now()).state=='unknown'
    assert 'QA-NO-LIVE-KEY' not in caplog.text


def test_cache_expiry_quota_and_no_raw_ip(configured):
    at=classifier.now()
    with patch.object(classifier,'now',return_value=at),patch.object(classifier,'provider',return_value=result()) as provider:
        assert classifier.classify(IP).state=='flagged'
        assert classifier.classify(IP).state=='flagged'
        assert provider.call_count==1
    rows=db.session.execute(select(classifier.C)).mappings().all()
    assert len(rows)==1 and IP not in str(rows)
    with db.engine.begin() as conn:
        conn.execute(update(classifier.C).values(expires_at=at))
        conn.execute(update(classifier.B).values(attempts=1000))
    with patch.object(classifier,'provider') as provider:
        assert classifier.classify(IP).source=='quota_exhausted'
        provider.assert_not_called()


def test_private_ip_no_provider_and_failed_lookup_cache(configured):
    with patch.object(classifier,'provider',return_value=classifier.unknown()) as provider:
        assert classifier.classify('127.0.0.1').state=='unknown'
        assert classifier.classify('not an IP').state=='unknown'
        provider.assert_not_called()
        assert classifier.classify(IP).state=='unknown'
        assert classifier.classify(IP).state=='unknown'
        assert provider.call_count==1


def test_apple_feed_atomic_validation_and_expiry(configured):
    at=classifier.now()
    assert classifier.import_relay_csv(b'8.8.8.0/24,US,US-CA,Test City\n',at)==1
    with patch.object(classifier,'provider') as provider:
        assert classifier.classify(IP).categories==('private_relay',)
        provider.assert_not_called()
    with pytest.raises(ValueError):classifier.import_relay_csv(b'0.0.0.0/0,US\n')
    assert db.session.execute(select(classifier.F.c.imported_at)).scalar_one()==at
    with patch.object(classifier,'now',return_value=at+timedelta(hours=48)),patch.object(classifier,'provider',return_value=classifier.unknown()) as provider:
        assert classifier.classify(IP).state=='unknown'
        assert provider.call_count==1


def test_http_gate_banner_community_and_api(configured):
    client=member(configured)
    assert call(configured,client,'/').status_code==303
    response=call(configured,client,'/community')
    assert response.status_code==200,response.data[:500]
    assert b'Community-only access' in response.data
    assert b'profile-dropdown-wrapper' not in response.data
    assert response.headers['Cache-Control']=='private, no-store'
    response=call(configured,client,'/api/nonexistent')
    assert response.status_code==403 and response.json['code']=='network_restricted'
    assert call(configured,client,'/delete_account',method='post').status_code==403
    assert call(configured,client,'/privacy/network-checks').status_code==200


def test_all_registered_endpoints_are_explicitly_gated(configured):
    client=member(configured)
    with client.session_transaction() as saved: credentials=dict(saved)
    checked=0
    for rule in configured.url_map.iter_rules():
        if rule.endpoint in network.PUBLIC:
            continue
        method=next(iter(sorted(rule.methods-{'HEAD','OPTIONS'})))
        with configured.test_request_context('/policy-probe',method=method,headers={'Accept':'application/json'},environ_base={'REMOTE_ADDR':IP}):
            session.update(credentials)
            from flask import request
            request.url_rule=rule
            response=network.guard()
            if rule.endpoint in network.ALLOWED:
                assert response is None,rule.endpoint
            else:
                assert response and response[1]==403,rule.endpoint
            checked+=1
    assert checked>100
    assert network.COMMUNITY.issubset(set(configured.view_functions))


def test_ip_change_unknown_and_expired_cache_release_only_network(configured):
    client=member(configured)
    assert call(configured,client,'/').status_code==303
    with patch.object(classifier,'classify',return_value=result('clean')):
        assert call(configured,client,'/').status_code==200
    with patch.object(classifier,'classify',return_value=classifier.unknown()):
        assert call(configured,client,'/').status_code==200
        db.session.execute(update(User).where(User.id==ACCOUNTS['user1']['email']).values(is_banned=True))
        db.session.commit()
        assert call(configured,client,'/').status_code==403


def test_admin_exempt_but_banned_admin_not_exempt(configured,admin_client):
    with patch.object(classifier,'classify') as classify:
        assert call(configured,admin_client,'/admin/identity/network').status_code==200
        classify.assert_not_called()
    db.session.execute(update(User).where(User.id==ACCOUNTS['admin']['email']).values(is_banned=True));db.session.commit()
    assert call(configured,admin_client,'/admin/identity/network').status_code==403


def test_appeal_new_muted_user_only_private_exception(configured):
    client=member(configured,'newbie')
    db.session.execute(update(User).where(User.id==ACCOUNTS['newbie']['email']).values(community_muted_until=network.now()+timedelta(days=1)));db.session.commit()
    response=call(configured,client,'/community/network-appeal',method='post',data={'message':'Please review my restricted work network.'})
    assert response.status_code==303,response.data[:500]
    assert db.session.query(Appeal).count()==1
    assert call(configured,client,'/community/network-appeal',method='post',data={'message':'Repeated request should deduplicate.'}).status_code==303
    assert db.session.query(Appeal).count()==1
    # The ordinary Community posting service still refuses muted/new users.
    from services.community_service import require_can_write,CommunityError
    with pytest.raises(CommunityError):require_can_write(user('newbie'))
    stranger=member(configured,'user2')
    assert b'Please review my restricted work network.' not in call(configured,stranger,'/community/network-appeal').data


def create_appeal(app,key='user1'):
    client=member(app,key)
    assert call(app,client,'/community/network-appeal',method='post',data={'message':'Please review <script>private QA request</script>.'}).status_code==303
    db.session.expire_all()
    return client,db.session.query(Appeal).one()


def test_admin_approval_revocation_stale_and_account_binding(configured,admin_client):
    client,appeal=create_appeal(configured)
    path=f'/admin/identity/network/{appeal.id}'
    assert b'&lt;script&gt;' in call(configured,admin_client,path).data
    data=dict(version=appeal.version,action='approve',reason='Reviewed private request',confirmed='yes')
    assert call(configured,admin_client,path,method='post',data=data).status_code==303
    assert call(configured,admin_client,path,method='post',data=data).status_code==409
    assert call(configured,client,'/').status_code==200
    stranger=member(configured,'user2');assert call(configured,stranger,'/').status_code==303
    db.session.refresh(appeal)
    assert call(configured,admin_client,path,method='post',data={**data,'version':appeal.version,'action':'revoke'}).status_code==303
    assert call(configured,client,'/').status_code==303


def test_approval_cannot_bypass_account_verification_or_bans(configured,admin_client):
    client,appeal=create_appeal(configured)
    db.session.execute(update(User).where(User.id==ACCOUNTS['user1']['email']).values(email_verified=False));db.session.commit()
    response=call(configured,admin_client,f'/admin/identity/network/{appeal.id}',method='post',data=dict(version=appeal.version,action='approve',reason='QA',confirmed='yes'))
    assert response.status_code==409
    assert call(configured,client,'/community/network-appeal',method='post',data={'message':'Please bypass verification.'}).status_code==302
    assert db.session.query(Appeal).count()==1


def test_csrf_roles_and_impersonation(configured,admin_client):
    client,appeal=create_appeal(configured)
    path=f'/admin/identity/network/{appeal.id}'
    assert call(configured,client,path).status_code==303  # network gate before admin authorization
    with patch.object(classifier,'classify',return_value=result('clean')):
        assert call(configured,client,path).status_code==403
    configured.config['WTF_CSRF_ENABLED']=True
    assert call(configured,client,'/community/network-appeal',method='post').status_code==400
    assert call(configured,admin_client,path,method='post').status_code==400
    with admin_client.session_transaction() as saved:saved['impersonating_from']=ACCOUNTS['admin']['email']
    with patch.object(classifier,'classify',return_value=result('clean')):
        assert call(configured,admin_client,path).status_code==403


def test_approval_expiry_cleanup_no_audit_resurrection(configured,admin_client,runner):
    _,appeal=create_appeal(configured)
    at=network.now()
    with db.engine.begin() as conn:
        conn.execute(update(network.P).values(status='approved',expires_at=at-timedelta(days=181)))
    assert call(configured,admin_client,f'/admin/identity/network/{appeal.id}').status_code==404
    outcome=runner.invoke(args=['purge-network-evidence'])
    assert outcome.exit_code==0,outcome.output
    db.session.expire_all();assert db.session.query(Appeal).count()==0
    assert not db.session.execute(select(network.A)).first()


def test_audit_failure_rolls_back_approval(configured):
    _,appeal=create_appeal(configured)
    with patch.object(network,'audit',side_effect=RuntimeError('injected')),pytest.raises(RuntimeError):
        network.decide(appeal.id,user('admin').stable_id,appeal.version,'approve','QA reason')
    db.session.refresh(appeal)
    assert appeal.status=='open' and appeal.version==1


def test_migration_reruns_preserve_accounts(configured):
    before=[(u.id,u.stable_id) for u in db.session.query(User)]
    for model in reversed(MODELS):model.__table__.drop(db.engine)
    run_migration(db,configured);run_migration(db,configured)
    from migrations.verify import model_drift
    assert not model_drift(db.engine,db.metadata)
    assert [(u.id,u.stable_id) for u in db.session.query(User)]==before


def test_socket_rechecks_live_network_and_revoked_session(configured):
    client=member(configured)
    socketio=configured.extensions['socketio']
    with configured.app_context(),patch.object(classifier,'classify',return_value=result('clean')):
        ws=socketio.test_client(configured,flask_test_client=client,headers={'X-Forwarded-For':IP})
        assert ws.is_connected()
    try:
        with patch.object(classifier,'classify',return_value=result()):
            ws.emit('next_ball',{'match_id':'must-not-advance'})
            events=ws.get_received()
            assert any(e['name']=='ws_error' and e['args'][0]['code']=='network_restricted' for e in events),events
        db.session.execute(delete(ActiveSession).where(ActiveSession.user_id==ACCOUNTS['user1']['email']));db.session.commit()
        with patch.object(classifier,'classify',return_value=result('clean')):
            ws.emit('next_ball',{'match_id':'must-not-advance'})
            assert any(e['args'][0].get('code')=='auth_required' for e in ws.get_received() if e['name']=='ws_error')
    finally:
        if ws.is_connected():ws.disconnect()


def test_flagged_socket_connection_rejected(configured):
    client=member(configured)
    with configured.app_context(),patch.object(classifier,'classify',return_value=result()):
        ws=configured.extensions['socketio'].test_client(configured,flask_test_client=client)
        assert not ws.is_connected()


def test_disabled_mode_unchanged(app,authenticated_client):
    assert not network.enabled()
    assert call(app,authenticated_client,'/').status_code==200
    assert call(app,authenticated_client,'/community/network-appeal').status_code==404
