"""Report acceptance on isolated DBs; authentication uses shared QA accounts."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from unittest.mock import patch

import pytest
from sqlalchemy import select, update, delete
from database import db
from database import models as m
from scripts.dev_test_accounts import ACCOUNTS
from services import identity_account_review as review

AT = datetime(2026, 11, 12, 17, tzinfo=timezone.utc)


@pytest.fixture
def data(app, admin_client, monkeypatch):
    monkeypatch.setattr(review, 'utcnow', lambda: AT)
    actor = db.session.get(m.User, ACCOUNTS['admin']['email']).stable_id
    ids = [db.session.get(m.User, ACCOUNTS[k]['email']).stable_id for k in ('user1','user2','user3','newbie')]
    return app, admin_client, db.engine.url.database, actor, ids


def run(data):
    review.run(data[2], data[3], {'signup': False})
    return review.read(view='all', at=AT)


def digest(path, reports=False):
    with sqlite3.connect(path) as conn:
        lines = [line for line in conn.iterdump() if ('identity_account_' in line) == reports and not line.startswith('CREATE TRIGGER')]
    return hashlib.sha256('\n'.join(lines).encode()).hexdigest()


def sight(owner, last=AT, **overrides):
    values = dict(user_stable_id=owner, key_id='key',device_hash='SECRET_DEVICE', fingerprint_hash='SECRET_FINGERPRINT',
                  fingerprint_status='ok',cookie_confirmed=True,first_seen=(last-timedelta(days=1)).replace(tzinfo=None),last_seen=last.replace(tzinfo=None))
    values.update(overrides)
    db.session.add(m.IdentityDeviceSighting(**values))


def test_zero_evidence_writes_only_report_data(data):
    before = digest(data[2])
    result = run(data)
    assert digest(data[2]) == before
    assert result['header']['metadata']['accounts'] == 5
    assert result['header']['metadata']['device_accounts'] == 0
    assert result['header']['metadata']['network_accounts'] == 0
    assert all({'missing_device','missing_network'} <= set(r['findings']) for r in result['items'])
    with sqlite3.connect(data[2]) as conn:
        stored = str(conn.execute('SELECT * FROM identity_account_results').fetchall())
    assert '@' not in stored and 'SECRET_' not in stored
    response = data[1].get('/admin/identity/account-review')
    assert response.status_code == 200
    assert b'All accounts' in response.data and b'<dt>Accounts scanned</dt><dd>5</dd>' in response.data
    assert 'no-store' in response.headers['Cache-Control']


def test_state_findings_and_blocked_report_count(data):
    user, other = data[4][:2]
    db.session.execute(update(m.User).where(m.User.stable_id == user).values(email_verified=False,force_email_verify=True,is_banned=True))
    db.session.add(m.IdentityReviewCase(left_id=user,right_id=other,status='open',created_at=AT,updated_at=AT))
    db.session.add(m.IdentityEmailCampaign(id='qa-campaign',started_at=AT,deadline=AT+timedelta(days=30),created_by=data[3]))
    db.session.flush()
    db.session.add(m.IdentityCampaignMember(id='qa-member',campaign_id='qa-campaign',subject=user,status='pending',reason='plus_tag'))
    db.session.add(m.IdentitySignupAttempt(identity_hash='SECRET_HASH',email='no-account@example.com',reason='device',first_seen=AT,last_seen=AT,expires_at=AT+timedelta(days=1)))
    db.session.commit()
    result = run(data)
    row = next(r for r in result['items'] if r['stable_id']==user)
    assert {'unverified_email','forced_verification','active_ban','legacy_correction','open_review'} <= set(row['findings'])
    assert result['header']['metadata']['blocked_signup_groups']==1
    assert b'no-account@example.com' not in data[1].get('/admin/identity/account-review').data


def test_device_network_expiry_and_filters(data):
    user, other = data[4][:2]
    sight(user, AT-timedelta(days=179)); sight(other)
    db.session.add(m.IdentityNetworkEvent(user_stable_id=user,state='restricted',categories='[]',created_at=AT,expires_at=AT+timedelta(days=1)))
    db.session.commit()
    result = run(data)
    matches = review.read(finding='confirmed_device',at=AT)['items']
    assert len(matches)==2
    later = review.read(finding='confirmed_device',at=AT+timedelta(days=1))
    assert not later['items']
    expired = review.read(q=user,view='all',at=AT+timedelta(days=1))['items'][0]
    assert not expired['device'] and not expired['network']
    assert 'fingerprint' not in expired['findings']
    assert result['header']['metadata']['device_accounts']==2
    assert result['header']['metadata']['network_accounts']==1


@pytest.mark.parametrize('overrides',[
    {'last_seen':(AT-timedelta(days=180)).replace(tzinfo=None),'first_seen':(AT-timedelta(days=181)).replace(tzinfo=None)},
    {'last_seen':(AT+timedelta(days=1)).replace(tzinfo=None)},
    {'first_seen':(AT+timedelta(days=1)).replace(tzinfo=None)},
    {'key_id':''}, {'cookie_confirmed':False,'fingerprint_status':'missing'},
])
def test_unusable_device_evidence(data,overrides):
    sight(data[4][0],**overrides); db.session.commit()
    assert run(data)['header']['metadata']['device_accounts']==0


def test_report_expiry_purge_and_user_deletion(data):
    run(data)
    assert review.read(at=AT+timedelta(days=30)) is None
    with db.engine.begin() as conn:
        conn.execute(delete(m.User).where(m.User.stable_id==data[4][-1]))
    assert len(review.read(view='all',at=AT)['items'])==4
    with db.engine.begin() as conn:
        assert review.cleanup(conn,(AT+timedelta(days=30)).replace(tzinfo=None))==1
    assert not db.session.execute(select(m.IdentityAccountResult)).all()


def test_current_labels_search_and_get_is_read_only(data):
    run(data)
    before=digest(data[2],reports=True)
    # Label resolution uses the live table, verified without mutating shared QA email.
    assert len(review.read(q=ACCOUNTS['user1']['email'],view='all',at=AT)['items'])==1
    assert len(review.read(q=data[4][0],view='all',at=AT)['items'])==1
    assert review.read(q='%',view='all',at=AT)['items']==[]
    assert data[1].get('/admin/identity/account-review?view=bad').status_code==400
    assert data[1].get('/admin/identity/account-review?finding=bad').status_code==400
    assert data[1].get('/admin/identity/account-review?page=bad').status_code==400
    assert digest(data[2],reports=True)==before


def test_failed_timeout_and_busy_runs_keep_previous(data,monkeypatch):
    run(data)
    before=digest(data[2],reports=True)
    with review.scan_lock(data[2]), pytest.raises(review.ReviewBusy):
        run(data)
    with pytest.raises(TimeoutError):
        review.run(data[2],data[3],{},seconds=-1)
    def fail(*args):
        raise RuntimeError('synthetic scan failure')
    monkeypatch.setattr(review,'snapshot',fail)
    with pytest.raises(RuntimeError):
        run(data)
    assert digest(data[2],reports=True)==before


def test_write_failure_rolls_back_previous_report(data):
    run(data)
    before=digest(data[2],reports=True)
    with sqlite3.connect(data[2]) as conn:
        conn.execute("CREATE TRIGGER fail_report BEFORE INSERT ON identity_account_results BEGIN SELECT RAISE(ABORT,'QA failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        run(data)
    assert digest(data[2],reports=True)==before


def test_routes_authorization_csrf_and_failure(data,authenticated_client,monkeypatch):
    app,admin,*_=data
    assert authenticated_client.post('/admin/identity/account-review/run').status_code==403
    assert authenticated_client.get('/admin/identity/account-review').status_code==403
    assert admin.post('/admin/identity/account-review/run').status_code==303
    before=digest(data[2],reports=True)
    with patch.object(review,'execute',side_effect=TimeoutError):
        response=admin.post('/admin/identity/account-review/run',follow_redirects=True)
        assert b'previous successful report is unchanged' in response.data
    assert digest(data[2],reports=True)==before
    app.config['WTF_CSRF_ENABLED']=True
    assert admin.post('/admin/identity/account-review/run').status_code==400
    app.config['WTF_CSRF_ENABLED']=False
    with admin.session_transaction() as session:
        session['impersonating_from']=ACCOUNTS['user1']['email']
    assert admin.get('/admin/identity/account-review').status_code==403
    assert admin.post('/admin/identity/account-review/run').status_code==403


def test_live_review_stream(data, authenticated_client):
    app, admin, path, *_ = data
    endpoint = '/admin/identity/account-review/run'
    headers = {'Accept': 'application/x-ndjson'}
    assert authenticated_client.post(endpoint, headers=headers).status_code == 403
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        assert admin.post(endpoint, headers=headers).status_code == 400
    finally:
        app.config['WTF_CSRF_ENABLED'] = False

    response = admin.post(endpoint, headers=headers, buffered=False)
    assert response.mimetype == 'application/x-ndjson'
    assert response.headers['X-Accel-Buffering'] == 'no'
    assert 'no-store' in response.headers['Cache-Control']
    stream = iter(response.response)
    assert json.loads(next(stream))['message'] == 'Starting identity review'
    assert review.read(at=AT) is None
    events = [json.loads(line) for line in stream]
    assert next(event['total'] for event in events if 'total' in event) == 5
    assert [event['processed'] for event in events if 'processed' in event] == [0, 5]
    assert events[-2]['message'] == 'Saving report'
    assert events[-1]['status'] == 'complete'
    assert review.read(view='all', at=AT)['total'] == 5
    assert '@' not in json.dumps(events)

    before = digest(path, reports=True)
    with review.scan_lock(path):
        response = admin.post(endpoint, headers=headers, buffered=True)
    failed = json.loads(response.data.splitlines()[-1])
    assert failed['status'] == 'failed' and 'already running' in failed['message']
    assert digest(path, reports=True) == before


def test_migration_repeatable_and_identity_workflow_coverage(data):
    from migrations.add_identity_account_review import run_migration
    from services import identity_email_change, identity_merge
    with db.engine.begin() as conn:
        m.IdentityAccountResult.__table__.drop(conn)
        m.IdentityAccountReport.__table__.drop(conn)
    before=digest(data[2])
    run_migration(db,data[0]); run_migration(db,data[0])
    assert before==digest(data[2])
    with db.engine.connect() as conn:
        identity_email_change.coverage(conn)
        identity_merge.coverage(conn)


@pytest.fixture
def synthetic(tmp_path):
    """Scanner-only schema fixtures, never login identities or persistent QA users."""
    path=tmp_path/'synthetic.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE users(id TEXT PRIMARY KEY,stable_id TEXT UNIQUE,email_verified INTEGER,force_email_verify INTEGER,is_banned INTEGER,banned_until TEXT);
            CREATE TABLE identity_device_sightings(user_stable_id TEXT,key_id TEXT,device_hash TEXT,fingerprint_hash TEXT,fingerprint_status TEXT,cookie_confirmed INTEGER,first_seen TEXT,last_seen TEXT);
            CREATE TABLE identity_network_events(id INTEGER PRIMARY KEY,user_stable_id TEXT,state TEXT,created_at TEXT,expires_at TEXT);
            CREATE TABLE identity_review_cases(left_id TEXT,right_id TEXT,status TEXT);
            CREATE TABLE identity_campaign_members(subject TEXT,status TEXT);
            CREATE TABLE identity_signup_attempts(expires_at TEXT);
        ''')
    return path


def test_email_checks_and_orphaned_evidence(synthetic,monkeypatch):
    monkeypatch.setattr(review,'utcnow',lambda:AT)
    with sqlite3.connect(synthetic) as conn:
        conn.executemany('INSERT INTO users VALUES (?,?,1,0,0,NULL)',[
            ('bad-address','bad'),('a.b@gmail.com','one'),('ab+tag@googlemail.com','two'),('ok@example.com','ok')])
        conn.execute('INSERT INTO identity_device_sightings VALUES (?,?,?,?,?,?,?,?)',
                     ('orphan','key','secret','secret','ok',1,review.stamp(AT-timedelta(days=1)),review.stamp(AT)))
    _,rows,meta=review.snapshot(synthetic,{},review.Deadline(60))
    assert 'malformed_email' in rows['bad']['findings']
    assert 'email_policy' not in rows['bad']['findings']
    assert 'email_policy' in rows['two']['findings']
    assert 'canonical_email' in rows['one']['findings'] and 'canonical_email' in rows['two']['findings']
    assert meta['skipped_device_rows']==1 and meta['device_accounts']==0


def test_large_matching_group_never_expands_pairs(synthetic,monkeypatch):
    monkeypatch.setattr(review,'utcnow',lambda:AT)
    with sqlite3.connect(synthetic) as conn:
        conn.executemany('INSERT INTO users VALUES (?,?,1,0,0,NULL)',[(f'fixture{i}@example.com',f'id{i}') for i in range(2500)])
        conn.executemany('INSERT INTO identity_device_sightings VALUES (?,?,?,?,?,?,?,?)',[
            (f'id{i}','key','secret','secret','ok',1,review.stamp(AT-timedelta(days=1)),review.stamp(AT)) for i in range(2500)])
    with patch('utils.identity_scan.combinations',side_effect=AssertionError('Pair expansion forbidden')):
        _,rows,meta=review.snapshot(synthetic,{},review.Deadline(60))
    assert len(rows)==2500 and meta['device_accounts']==2500
    assert all({'confirmed_device','fingerprint'} <= set(r['findings']) for r in rows.values())
    assert 'secret' not in json.dumps(rows)


def test_pagination_order_and_clean_account_view(data,monkeypatch):
    # Populate report fixtures with existing QA owners, then exercise SQL/page logic
    # with repeated result projections to avoid creating additional login accounts.
    run(data)
    original=review.effective
    def clean(item,at):
        item=original(item,at)
        item['findings']={}
        return item
    monkeypatch.setattr(review,'effective',clean)
    assert review.read(at=AT)['total']==0
    assert review.read(view='all',at=AT)['total']==5
    assert review.read(view='all',page=2,at=AT)['items']==[]


def test_lock_is_cross_process(data):
    import subprocess,sys
    code='''from services.identity_account_review import scan_lock, ReviewBusy
import sys
try:
    with scan_lock(sys.argv[1]): pass
except ReviewBusy:
    sys.exit(23)
'''
    with review.scan_lock(data[2]):
        result=subprocess.run([sys.executable,'-c',code,data[2]],capture_output=True,timeout=15)
    assert result.returncode==23


def test_snapshot_consistency_and_read_only_connection(synthetic,monkeypatch):
    monkeypatch.setattr(review,'utcnow',lambda:AT)
    with sqlite3.connect(synthetic) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute("INSERT INTO users VALUES ('fixture@example.com','one',1,0,0,NULL)")
    original=review.evidence_groups
    def change_after_grouping(conn,at,**kwargs):
        result=original(conn,at,**kwargs)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE users SET email_verified=0")
        with sqlite3.connect(synthetic) as writer:
            writer.execute('UPDATE users SET email_verified=0')
        return result
    monkeypatch.setattr(review,'evidence_groups',change_after_grouping)
    _,rows,_=review.snapshot(synthetic,{},review.Deadline(60))
    assert rows['one']['email_verified'] is True


def test_gevent_hub_remains_responsive(data,monkeypatch):
    import gevent
    import time
    original=review.snapshot
    def slow(*args):
        # Busy CPU work, not cooperative gevent sleep, in the real native pool.
        end=time.monotonic()+0.4
        while time.monotonic()<end:
            pass
        return original(*args)
    monkeypatch.setattr(review,'snapshot',slow)
    ticks=[]
    normal_responses=[]
    def heartbeat():
        with data[0].test_client() as client:
            normal_responses.append(client.get('/login').status_code)
        for _ in range(5):
            gevent.sleep(.01)
            ticks.append(time.monotonic())
    heartbeat_job=gevent.spawn(heartbeat)
    review.execute(data[3],{})
    completed=__import__('time').monotonic()
    heartbeat_job.join()
    assert len(ticks)==5 and max(ticks)<completed
    assert normal_responses==[200]


def test_real_30_account_pages_and_current_label_resolution(synthetic,monkeypatch):
    from flask import Flask
    monkeypatch.setattr(review,'utcnow',lambda:AT)
    with sqlite3.connect(synthetic) as conn:
        conn.executemany('INSERT INTO users VALUES (?,?,1,0,0,NULL)',[(f'fixture{i}@example.com',f'id{i:03}') for i in range(65)])
        conn.execute("UPDATE users SET email_verified=0 WHERE stable_id='id064'")
    isolated=Flask(__name__)
    isolated.config['SQLALCHEMY_DATABASE_URI']='sqlite:///'+str(synthetic)
    db.init_app(isolated)
    with isolated.app_context():
        with db.engine.begin() as conn:
            m.IdentityAccountReport.__table__.create(conn)
            m.IdentityAccountResult.__table__.create(conn)
        review.run(synthetic,'id000',{})
        pages=[review.read(view='all',page=p,at=AT) for p in (1,2,3)]
        assert [len(p['items']) for p in pages]==[30,30,5]
        assert [p['more'] for p in pages]==[True,True,False]
        assert len({r['stable_id'] for p in pages for r in p['items']})==65
        assert pages[0]['items'][0]['stable_id']=='id064'
        with sqlite3.connect(synthetic) as conn:
            conn.execute("UPDATE users SET id='changed@example.com' WHERE stable_id='id064'")
        assert review.read(q='changed@example.com',at=AT)['items'][0]['stable_id']=='id064'
        assert not review.read(q='fixture64@example.com',at=AT)['items']


def test_missing_stable_id_refuses_partial_report(synthetic,monkeypatch):
    monkeypatch.setattr(review,'utcnow',lambda:AT)
    with sqlite3.connect(synthetic) as conn:
        conn.execute("INSERT INTO users VALUES ('fixture@example.com',NULL,1,0,0,NULL)")
    with pytest.raises(review.ReviewUnavailable,match='distinct stable IDs'):
        review.snapshot(synthetic,{},review.Deadline(60))


def test_timeout_during_publication_rolls_back(data,monkeypatch):
    run(data)
    before=digest(data[2],reports=True)
    captured=review.snapshot(data[2],{},review.Deadline(60))
    monkeypatch.setattr(review,'snapshot',lambda *args:captured)
    class ExpireOnPublication:
        calls=0
        def __init__(self,*args):pass
        def __call__(self):
            self.calls+=1
            if self.calls==3:
                raise TimeoutError('QA publication deadline')
    monkeypatch.setattr(review,'Deadline',ExpireOnPublication)
    with pytest.raises(TimeoutError):
        run(data)
    assert digest(data[2],reports=True)==before


def test_success_replaces_single_report_and_purge_cli(data,monkeypatch):
    run(data)
    monkeypatch.setattr(review,'utcnow',lambda:AT+timedelta(hours=1))
    review.run(data[2],data[3],{})
    with sqlite3.connect(data[2]) as conn:
        assert conn.execute('SELECT COUNT(*) FROM identity_account_reports').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM identity_account_results').fetchone()[0]==5
    from services import identity_review
    monkeypatch.setattr(identity_review,'now',lambda:(AT+timedelta(days=31)).replace(tzinfo=None))
    result=data[0].test_cli_runner().invoke(args=['purge-identity-reviews'])
    assert result.exit_code==0 and json.loads(result.output)['account_reports']==1
