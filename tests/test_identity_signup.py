"""Enforcement tests use shared QA identities only, in disposable databases."""
from datetime import timedelta, timezone
import hashlib
import json
import re
from unittest.mock import patch

import pytest
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import delete, insert, select, update

from database import db
from database.models import User, IdentityDeviceSighting, IdentityEmailReservation
from services import identity_signup as signup
from utils import identity_devices as devices
from scripts.dev_test_accounts import ACCOUNTS
from migrations.add_identity_signup import MODELS, run_migration

FP = json.dumps(dict(language='en-US', timezone='America/New_York', screen=[1080,1920], platform='QA'))
PASSWORD = 'IsolatedSignup123!'


@pytest.fixture
def configured(app, admin_client):
    app.config.update(IDENTITY_SIGNUP_ENFORCE=True, IDENTITY_SIGNUP_KEY='s'*40, IDENTITY_DEVICE_KEY='d'*40)
    app.extensions['identity_signup_pow'] = lambda *a: (True, '')
    yield app


def unused(key='newbie'):
    """Reuse a QA identity after removing it only from this test's disposable DB."""
    email = ACCOUNTS[key]['email']
    user = db.session.get(User, email)
    if user:
        db.session.delete(user)
        db.session.commit()
    db.session.execute(delete(IdentityEmailReservation).where(IdentityEmailReservation.canonical_email == email))
    db.session.commit()
    return email


def context(app, email, *, ip='192.0.2.10', token='a'*64, fp=FP, proof=False):
    cookie = URLSafeTimedSerializer(app.config['IDENTITY_DEVICE_KEY'], salt=devices.SALT).dumps(token) if token else ''
    return app.test_request_context('/register', method='POST', data={'device_fingerprint': fp},
        headers={'Cookie': devices.COOKIE+'='+cookie}, environ_base={'REMOTE_ADDR': ip})


def grant(app, email, scopes, *, marker=None):
    from flask import session
    at = signup.now()
    with db.engine.begin() as conn:
        result = conn.execute(insert(signup.P).values(email=email, identity_hash=signup.identity_hash(signup.normalize_email(email)),
            open_key=signup.identity_hash(signup.normalize_email(email)), status='approved', message='QA request',
            created_at=at, verified_at=at, expires_at=at+timedelta(days=7), scopes=json.dumps(scopes),
            marker_generation=marker))
    session['identity_signup_proof'] = {'email': email, 'until': (at+timedelta(minutes=30)).replace(tzinfo=timezone.utc).timestamp()}
    return result.inserted_primary_key[0]


def test_atomic_success_metadata_device_and_ip(configured):
    email = unused()
    with context(configured, email):
        assert signup.create_user(email.upper(), PASSWORD, 'QA') == email
    db.session.expire_all()
    user = db.session.get(User, email)
    assert user.canonical_email == email and not user.email_verified
    assert db.session.query(IdentityDeviceSighting).filter_by(user_stable_id=user.stable_id).count() == 1
    assert db.session.execute(select(signup.S)).first()
    assert db.session.get(IdentityEmailReservation, email)


@pytest.mark.parametrize('email,code', [('qa.newbie+tag@example.com','plus_tag_not_allowed'), ('qa.newbie@mailinator.com','disposable_domain')])
def test_invalid_new_email_no_account(configured, email, code):
    with context(configured, email), pytest.raises(signup.InvalidEmail) as caught:
        signup.create_user(email, PASSWORD, 'QA')
    assert caught.value.code == code
    assert not db.session.get(User, email)
    assert db.session.execute(select(signup.T.c.reason)).scalar_one() == code


@pytest.mark.parametrize('token,fp', [(None,FP), ('a'*64,''), ('a'*64,'bad')])
def test_missing_signals_hold_without_account_or_cap(configured, token, fp):
    email = unused()
    with context(configured,email,token=token,fp=fp), pytest.raises(signup.Refused) as caught:
        signup.create_user(email,PASSWORD,'QA')
    assert 'missing_signal' in caught.value.reasons
    assert not db.session.get(User,email)
    assert not db.session.execute(select(signup.S)).first()


def test_device_and_fingerprint_blocks_and_deduplicates(configured):
    email = unused()
    owner = db.session.get(User, ACCOUNTS['user1']['email'])
    at = signup.now()
    fp,_ = devices.fingerprint(FP)
    db.session.add(IdentityDeviceSighting(user_stable_id=owner.stable_id, key_id=devices.key_id('d'*40),
        device_hash=devices.digest('d'*40,'device','a'*64), fingerprint_hash=devices.digest('d'*40,'fingerprint-v1',fp),
        fingerprint_status='ok',cookie_confirmed=True,first_seen=at,last_seen=at))
    db.session.commit()
    for _ in range(2):
        with context(configured,email), pytest.raises(signup.Refused) as caught:
            signup.create_user(email,PASSWORD,'QA')
        assert set(caught.value.reasons) == {'device_match','fingerprint_match'}
    rows = db.session.execute(select(signup.T)).mappings().all()
    assert len(rows)==2 and all(r['count']==2 for r in rows)
    assert owner.stable_id in rows[0]['evidence']
    assert 'fingerprint_hash' not in rows[0]['evidence']


def test_expired_or_unconfirmed_device_does_not_block(configured):
    email=unused()
    owner=db.session.get(User,ACCOUNTS['user1']['email'])
    at=signup.now()
    db.session.add(IdentityDeviceSighting(user_stable_id=owner.stable_id,key_id=devices.key_id('d'*40),
        device_hash=devices.digest('d'*40,'device','a'*64),fingerprint_hash='',fingerprint_status='missing',
        cookie_confirmed=False,first_seen=at,last_seen=at))
    db.session.commit()
    with context(configured,email):
        signup.create_user(email,PASSWORD,'QA')
    assert db.session.get(User,email)


def test_exact_ip_cap_boundary_and_no_subnet_block(configured):
    email=unused()
    at=signup.now()
    with context(configured,email):
        network=signup.ip_hash()
        with db.engine.begin() as conn:
            conn.execute(insert(signup.S).values(ip_hash=network,created_at=at-timedelta(hours=23)))
        with pytest.raises(signup.Refused) as caught:
            signup.create_user(email,PASSWORD,'QA')
        assert caught.value.reasons==['ip_cap']
        with db.engine.begin() as conn:
            conn.execute(update(signup.S).values(created_at=at-timedelta(hours=24)))
        with patch.object(signup,'now',return_value=at):
            signup.create_user(email,PASSWORD,'QA')
    other=unused('user2')
    with context(configured,other,ip='192.0.2.11',token='b'*64,fp=FP.replace('QA','Other')):
        signup.create_user(other,PASSWORD,'QA')


def test_approval_scoped_mailbox_required_and_consumed(configured):
    from flask import session
    email=unused()
    with context(configured,email,token=None):
        pid=grant(configured,email,['missing_signal'])
        session.pop('identity_signup_proof')
        with pytest.raises(signup.Refused):
            signup.create_user(email,PASSWORD,'QA')
        session['identity_signup_proof']={'email':email,'until':(signup.now()+timedelta(minutes=30)).replace(tzinfo=timezone.utc).timestamp()}
        signup.create_user(email,PASSWORD,'QA')
    row=db.session.execute(select(signup.P).where(signup.P.c.id==pid)).mappings().one()
    assert row['status']=='used' and row['open_key'] is None


def test_unrelated_ip_cap_not_waived_and_grant_not_consumed(configured):
    email=unused()
    with context(configured,email,token=None):
        grant(configured,email,['missing_signal'])
        with db.engine.begin() as conn:
            conn.execute(insert(signup.S).values(ip_hash=signup.ip_hash(),created_at=signup.now()))
        with pytest.raises(signup.Refused) as caught:
            signup.create_user(email,PASSWORD,'QA')
        assert caught.value.reasons==['ip_cap']
    assert db.session.execute(select(signup.P.c.status)).scalar_one()=='approved'


def test_audit_failure_rolls_back_user_reservation_approval_and_evidence(configured):
    email=unused()
    with context(configured,email):
        grant(configured,email,['ip_cap'])
        with patch.object(signup,'audit',side_effect=RuntimeError('injected')),pytest.raises(RuntimeError):
            signup.create_user(email,PASSWORD,'QA')
    assert not db.session.get(User,email)
    assert not db.session.get(IdentityEmailReservation,email)
    assert not db.session.execute(select(signup.S)).first()
    assert db.session.execute(select(signup.P.c.status)).scalar_one()=='approved'


def test_marker_requires_generation_bound_return_and_preserves_empty_account(configured):
    email=unused()
    with context(configured,email):
        ident=signup.normalize_email(email)
        with db.engine.begin() as conn:
            conn.execute(insert(signup.M).values(identity_hash=signup.identity_hash(ident),key_id=devices.key_id('s'*40),
                reason='deleted',generation='generation',created_at=signup.now()))
            conn.execute(insert(IdentityEmailReservation).values(canonical_email=email))
        with pytest.raises(signup.Refused): signup.create_user(email,PASSWORD,'QA')
        grant(configured,email,['deleted_return'],marker='wrong')
        with pytest.raises(signup.Refused): signup.create_user(email,PASSWORD,'QA')
        with db.engine.begin() as conn: conn.execute(update(signup.P).values(marker_generation='generation'))
        signup.create_user(email,PASSWORD,'QA')
    user=db.session.get(User,email)
    assert user and not user.teams and not user.matches
    assert db.session.execute(select(signup.M)).first()


def test_banned_marker_cannot_be_bypassed(configured):
    email=unused()
    with context(configured,email):
        with db.engine.begin() as conn:
            conn.execute(insert(signup.M).values(identity_hash=signup.identity_hash(signup.normalize_email(email)),
                key_id=devices.key_id('s'*40),reason='banned',generation='generation',created_at=signup.now()))
        grant(configured,email,['deleted_return'],marker='generation')
        with pytest.raises(signup.Refused): signup.create_user(email,PASSWORD,'QA')


def test_mailbox_request_verification_single_use_rate_limit(configured):
    email=ACCOUNTS['newbie']['email']
    with context(configured,email):
        for _ in range(5): address,token=signup.request_mailbox(email,'QA review')
        with pytest.raises(ValueError,match='Too many'): signup.request_mailbox(email,'QA review')
        assert signup.verify_mailbox(token)==email
        assert signup.mailbox_proof()==email
        with pytest.raises(ValueError): signup.verify_mailbox(token)
    assert db.session.execute(select(signup.P.c.status)).scalar_one()=='open'
    assert not any(token in str(r) for r in db.session.execute(select(signup.V)))


def open_request(app,email):
    with context(app,email):
        _,token=signup.request_mailbox(email,'Please review <script>QA</script>')
        signup.verify_mailbox(token)
    return db.session.execute(select(signup.P)).mappings().one()


def test_admin_lifecycle_stale_ownership_reason_and_revocation(configured,admin_client):
    row=open_request(configured,ACCOUNTS['newbie']['email'])
    path=f"/admin/identity/signup/{row['id']}"
    assert b'&lt;script&gt;QA&lt;/script&gt;' in admin_client.get(path).data
    data={'version':row['version'],'action':'approve','reason':'Mailbox verified; QA exception','scopes':['missing_signal'],'confirmed':'yes'}
    assert admin_client.post(path,data={**data,'reason':''}).status_code==409
    assert admin_client.post(path,data=data).status_code==303
    assert admin_client.post(path,data=data).status_code==409
    assert admin_client.post(path,data={**data,'version':row['version']+1,'action':'revoke'}).status_code==303
    assert db.session.execute(select(signup.P.c.status)).scalar_one()=='revoked'


def test_public_and_admin_access_csrf_and_no_get_mutation(configured,client,authenticated_client,admin_client):
    row=open_request(configured,ACCOUNTS['newbie']['email'])
    def get(client, path):
        with configured.app_context():
            return client.get(path)
    for path in ['/admin/identity/signup','/admin/identity/signup/attempts',f"/admin/identity/signup/{row['id']}"]:
        assert get(client,path).status_code==302
        assert get(authenticated_client,path).status_code==403
        assert get(admin_client,path).status_code==200
    assert get(client,'/signup-review').status_code==200
    assert get(client,'/signup-review/status').status_code==302
    configured.config['WTF_CSRF_ENABLED']=True
    assert client.post('/signup-review').status_code==400
    assert client.post('/signup-review/verify').status_code==400
    assert admin_client.post(f"/admin/identity/signup/{row['id']}").status_code==400
    with admin_client.session_transaction() as sess: sess['impersonating_from']=ACCOUNTS['admin']['email']
    assert admin_client.get('/admin/identity/signup').status_code==403


def test_cleanup_expired_approval_hidden_and_markers_retained(configured,admin_client,runner):
    row=open_request(configured,ACCOUNTS['newbie']['email'])
    at=signup.now()
    with db.engine.begin() as conn:
        conn.execute(update(signup.P).values(status='approved',expires_at=at-timedelta(days=181)))
        conn.execute(insert(signup.M).values(identity_hash='h',key_id=devices.key_id('s'*40),reason='deleted',generation='g',created_at=at-timedelta(days=500)))
    response=admin_client.get(f"/admin/identity/signup/{row['id']}")
    assert response.status_code==404
    result=runner.invoke(args=['purge-signup-evidence'])
    assert result.exit_code==0,result.output
    assert not db.session.execute(select(signup.P)).first()
    assert db.session.execute(select(signup.M)).first()


def test_migration_rerun_model_parity_and_no_user_changes(configured):
    before=[(u.id,u.stable_id) for u in db.session.query(User)]
    for model in reversed(MODELS): model.__table__.drop(db.engine)
    run_migration(db,configured)
    run_migration(db,configured)
    assert [(u.id,u.stable_id) for u in db.session.query(User)]==before
    from migrations.verify import model_drift
    assert not model_drift(db.engine,db.metadata)


def test_public_signup_route_uses_normalized_identity(configured,client):
    email=unused()
    with patch('routes.auth_routes.verify_turnstile',return_value=(True,'')), patch('routes.auth_routes.send_verification_email',return_value=True):
        client.get('/register',base_url='https://localhost')
        response=client.post('/register',base_url='https://localhost',data={'email':email.upper(),'display_name':'QA',
            'password':PASSWORD,'confirm_password':PASSWORD,'device_fingerprint':FP})
    assert response.status_code==302,response.data[:500]
    assert db.session.get(User,email)


def test_admin_creation_bypasses_signals_but_not_email_policy(configured,admin_client):
    email=unused()
    response=admin_client.post('/admin/users/create',data={'email':email,'password':PASSWORD,'display_name':'QA'})
    assert response.status_code==200,response.data
    assert not db.session.execute(select(signup.S)).first()
    assert admin_client.post('/admin/users/create',data={'email':'qa.newbie+bad@example.com','password':PASSWORD}).status_code==400


def test_existing_login_and_disabled_mode_unchanged(app,client,authenticated_client):
    assert app.config['IDENTITY_SIGNUP_ENFORCE'] is False
    assert client.get('/signup-review').status_code==404
    assert authenticated_client.get('/my-profile').status_code==200


def test_deletion_marker_and_audit_are_atomic(configured):
    from auth.user_auth import delete_user
    email=ACCOUNTS['newbie']['email']
    with context(configured,email):
        with patch.object(signup,'audit',side_effect=RuntimeError('injected')), patch('auth.user_auth.log_exception'):
            assert not delete_user(email)
        assert db.session.get(User,email)
        assert not db.session.execute(select(signup.M)).first()
        assert delete_user(email)
    assert not db.session.get(User,email)
    assert db.session.execute(select(signup.M.c.reason)).scalar_one()=='deleted'


def test_expired_approval_and_proof_cannot_bypass(configured):
    from flask import session
    email=unused()
    with context(configured,email,token=None):
        grant(configured,email,['missing_signal'])
        session['identity_signup_proof']['until']=0
        with pytest.raises(signup.Refused): signup.create_user(email,PASSWORD,'QA')
        session['identity_signup_proof']['until']=(signup.now()+timedelta(hours=1)).replace(tzinfo=timezone.utc).timestamp()
        with db.engine.begin() as conn: conn.execute(update(signup.P).values(expires_at=signup.now()))
        with pytest.raises(signup.Refused): signup.create_user(email,PASSWORD,'QA')
    assert not db.session.get(User,email)


def test_dedicated_key_change_fails_closed(configured):
    email=unused()
    with db.engine.begin() as conn:
        conn.execute(insert(signup.M).values(identity_hash='h',key_id='different-key',reason='deleted',generation='g',created_at=signup.now()))
    with context(configured,email),pytest.raises(ValueError,match='key mismatch'):
        signup.create_user(email,PASSWORD,'QA')
    assert not db.session.get(User,email)


def test_email_change_cannot_introduce_canonical_collision(configured):
    from auth.user_auth import update_user_email
    # Existing QA identities remain intact; normalization of case is sufficient
    # to exercise collision protection without a new test account.
    with context(configured,ACCOUNTS['user1']['email']):
        ok,_=update_user_email(ACCOUNTS['user1']['email'],ACCOUNTS['user2']['email'].upper())
    assert not ok
    assert db.session.get(User,ACCOUNTS['user1']['email'])


@pytest.mark.parametrize('mode',['ip','device','canonical','approval'])
def test_independent_process_signup_races(configured,mode):
    import subprocess
    import sys
    emails=[unused('newbie'),unused('user2')]
    if mode in ('canonical','approval'): emails[1]=emails[0]
    if mode=='approval':
        with context(configured,emails[0]): grant(configured,emails[0],['missing_signal'])
    script='''
import sys,json
from datetime import timedelta,timezone
from flask import Flask,session
from itsdangerous import URLSafeTimedSerializer
from database import db
from services import identity_signup as signup
from utils import identity_devices as devices
app=Flask('signup-race')
app.config.update(SQLALCHEMY_DATABASE_URI=sys.argv[1],SECRET_KEY='test',IDENTITY_SIGNUP_ENFORCE=True,IDENTITY_SIGNUP_KEY='s'*40,IDENTITY_DEVICE_KEY='d'*40)
db.init_app(app)
mode,email,n=sys.argv[2],sys.argv[3],int(sys.argv[4])
token=('a' if mode=='device' or n==0 else 'b')*64
fp=json.dumps(dict(language='en-US',timezone='UTC',screen=[1000,1200],platform='QA'+('same' if mode=='device' else str(n))))
cookie=URLSafeTimedSerializer('d'*40,salt=devices.SALT).dumps(token) if mode!='approval' else ''
with app.test_request_context('/register',method='POST',data={'device_fingerprint':fp},headers={'Cookie':devices.COOKIE+'='+cookie},environ_base={'REMOTE_ADDR':'192.0.2.'+('10' if mode=='ip' else str(n+10))}):
    if mode=='approval': session['identity_signup_proof']={'email':email,'until':(signup.now()+timedelta(minutes=30)).replace(tzinfo=timezone.utc).timestamp()}
    print('READY',flush=True)
    input()
    try:
        signup.create_user(email,'IsolatedSignup123!','QA')
        print('created')
    except signup.Refused:
        print('refused')
'''
    processes=[subprocess.Popen([sys.executable,'-c',script,str(db.engine.url),mode,email,str(i)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for i,email in enumerate(emails)]
    try:
        for p in processes: assert p.stdout.readline().strip()=='READY'
        for p in processes: p.stdin.write('go\n');p.stdin.flush()
        output=[p.communicate(timeout=30) for p in processes]
        assert all(p.returncode==0 for p in processes),output
        assert sorted(out.strip() for out,_ in output)==['created','refused'],output
    finally:
        for p in processes:
            if p.poll() is None: p.kill();p.wait()
    db.session.expire_all()
    assert db.session.query(User).filter(User.id.in_(emails)).count()==1


def test_public_appeal_end_to_end_no_account_and_private_status(configured):
    email=unused()
    with configured.app_context():
        client=configured.test_client()
        with patch('routes.identity_signup_routes.verify_turnstile',return_value=(True,'')),patch('routes.identity_signup_routes.email_service.send_email',return_value=True) as send:
            response=client.post('/signup-review',data={'email':email,'message':'QA signup request'})
            assert response.status_code==200,response.data
            html=send.call_args.args[2]
            token=re.search(r'token=([^"&]+)',html)[1]
        assert not db.session.get(User,email)
        assert client.get('/signup-review/verify?token='+token).status_code==200
        assert db.session.execute(select(signup.P.c.status)).scalar_one()=='pending'
        assert client.post('/signup-review/verify',data={'token':token}).status_code==303
        status=client.get('/signup-review/status')
        assert b'waiting for admin review' in status.data
        assert status.headers['Referrer-Policy']=='no-referrer'
        assert client.post('/signup-review/verify',data={'token':token}).status_code==400
        assert not db.session.get(User,email)


def test_mailbox_link_binds_message_and_survives_older_pending_expiry(configured):
    email=ACCOUNTS['newbie']['email']
    at=signup.now()
    with context(configured,email):
        signup.request_mailbox(email,'Unverified original message')
        with patch.object(signup,'now',return_value=at+timedelta(minutes=50)):
            _,token=signup.request_mailbox(email,'Actual mailbox owner message')
        with patch.object(signup,'now',return_value=at+timedelta(minutes=70)):
            signup.verify_mailbox(token)
    rows=db.session.execute(select(signup.P).where(signup.P.c.status=='open')).mappings().all()
    assert len(rows)==1 and rows[0]['message']=='Actual mailbox owner message'


def test_password_update_unchanged_with_enforcement(configured):
    from auth.user_auth import update_user_password
    email=ACCOUNTS['user1']['email']
    with context(configured,email):
        ok,message=update_user_password(email,PASSWORD)
    assert ok,message


def test_legacy_email_writer_reserves_and_checks_markers(configured):
    from auth.user_auth import update_user_email
    source=ACCOUNTS['user1']['email']
    target=unused()
    with context(configured,source):
        with db.engine.begin() as conn:
            conn.execute(insert(signup.M).values(identity_hash=signup.identity_hash(signup.normalize_email(target)),
                key_id=devices.key_id('s'*40),reason='deleted',generation='g',created_at=signup.now()))
        with patch('auth.user_auth.log_exception'):
            ok,_=update_user_email(source,target)
        assert not ok
        assert not db.session.get(IdentityEmailReservation,target)
        assert db.session.get(User,source)
        with db.engine.begin() as conn: conn.execute(delete(signup.M))
        ok,message=update_user_email(source,target)
        assert not ok
        assert 'Email changes' in message
        assert db.session.get(User,source)
        assert not db.session.get(User,target)
        assert not db.session.get(IdentityEmailReservation,target)


def test_invalid_legacy_email_marker_does_not_block_deletion(configured):
    from types import SimpleNamespace
    with context(configured,ACCOUNTS['user1']['email']):
        signup.mark_deleted(db.session,SimpleNamespace(id='invalid legacy address',is_banned=False))
        db.session.commit()
    row=db.session.execute(select(signup.M)).mappings().one()
    assert row['reason']=='deleted'
    assert 'invalid legacy address' not in str(row)
