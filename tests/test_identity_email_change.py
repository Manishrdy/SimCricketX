"""Feature 8 uses shared QA identities and isolated file-backed databases."""
from datetime import timedelta
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select, insert, update, text
from database import db
from database.models import User, ActiveSession, Team, AuthEventLog, IdentityEmailReservation
from services import identity_email_change as change
from scripts.dev_test_accounts import ACCOUNTS, logged_in_client

NEW = 'qa-renamed@example.com'


@pytest.fixture
def configured(app, admin_client, tmp_path, monkeypatch):
    root = tmp_path/'match-files'
    root.mkdir()
    app.config.update(IDENTITY_EMAIL_BACKUP_DIR=str(tmp_path/'operation-backups'), IDENTITY_EMAIL_MATCH_DIR=str(root))
    monkeypatch.setattr('utils.email_service.send_email', lambda *a, **kw: True)
    return app


def actor():
    return db.session.get(User, ACCOUNTS['admin']['email']).stable_id


def old():
    return ACCOUNTS['user1']['email']


def ready(recovery=False):
    captured = {}
    with patch.object(change, 'deliver_proofs', side_effect=lambda cid, tokens: captured.update(tokens)):
        cid = change.create(actor(), old(), NEW, 'Requested by account owner',
            'Ownership established through documented support recovery.' if recovery else '')
    if not recovery:
        change.verify(cid, 'old', captured['old'])
    change.verify(cid, 'new', captured['new'])
    return cid, captured


def apply(cid):
    plan, token = change.preview(actor(), cid)
    return change.execute(actor(), cid, token, NEW)


def call(app, client, path, method='get', **kwargs):
    with app.app_context():
        return getattr(client, method)(path, **kwargs)


def test_full_change_preserves_identity_references_security_and_backup(configured):
    u = db.session.get(User, old())
    stable, password = u.stable_id, u.password_hash
    u.reset_token='old-reset'; u.pw_change_otp_hash='old-otp'; u.force_password_reset=True
    u.is_banned=True; u.ban_reason='unchanged restriction'
    db.session.add(Team(name='QA team', short_code='QA', user_id=old()))
    db.session.add(AuthEventLog(email=old(), user_id=old(), event_type='login', status='pending'))
    db.session.commit()
    cid, _ = ready()
    result = apply(cid)
    assert result['counts']['teams.user_id']==1
    u = db.session.get(User, NEW)
    assert (u.stable_id,u.password_hash)==(stable,password)
    assert u.is_banned and u.force_password_reset and u.ban_reason=='unchanged restriction'
    assert u.reset_token is None and u.pw_change_otp_hash is None
    assert db.session.query(Team).one().user_id==NEW
    event=db.session.query(AuthEventLog).filter_by(event_type='login').one()
    assert event.email==old() and event.user_id==NEW
    assert not db.session.query(ActiveSession).filter_by(user_id=old()).first()
    backup=Path(configured.config['IDENTITY_EMAIL_BACKUP_DIR'])/result['backup']
    assert backup.exists() and backup.stat().st_mode & 0o777 == 0o600
    import sqlite3
    with sqlite3.connect(backup) as conn:
        assert conn.execute('select id from users where stable_id=?',(stable,)).fetchone()[0]==old()
    assert db.session.get(IdentityEmailReservation,NEW)


def test_canonical_collision_even_when_signup_disabled(configured):
    with pytest.raises(ValueError):
        change.create(actor(),old(),ACCOUNTS['user2']['email'].upper(),'Account owner request')


@pytest.mark.parametrize('email',['test+alias@example.com','broken','test@mailinator.com'])
def test_new_address_policy(configured,email):
    with pytest.raises(ValueError):
        change.create(actor(),old(),email,'Account owner request')


def test_admin_target_forbidden(configured):
    with pytest.raises(ValueError,match='non-admin'):
        change.create(actor(),ACCOUNTS['admin']['email'],NEW,'Account owner request')


def test_pending_proof_replay_expiry_and_recovery(configured):
    cid,tokens=ready(recovery=True)
    with pytest.raises(ValueError,match='already-used'):
        change.verify(cid,'new',tokens['new'])
    assert apply(cid)['new']==NEW


def test_both_proofs_required(configured):
    with patch.object(change,'deliver_proofs'):
        cid=change.create(actor(),old(),NEW,'Account owner request')
    with pytest.raises(ValueError,match='verification'):
        change.preview(actor(),cid)


def test_request_expiry(configured):
    cid,_=ready()
    with db.engine.begin() as conn:
        conn.execute(update(change.C).where(change.C.c.id==cid).values(expires_at=change.now()))
    with pytest.raises(ValueError,match='expired'):
        change.preview(actor(),cid)


def test_stale_preview_and_wrong_actor(configured):
    cid,_=ready(); plan,token=change.preview(actor(),cid)
    with db.engine.begin() as conn:
        conn.execute(update(User).where(User.id==old()).values(display_name='Changed after preview'))
    with pytest.raises(ValueError,match='fresh preview'):
        change.execute(actor(),cid,token,NEW)
    with pytest.raises(ValueError,match='another administrator'):
        change.execute('other-actor',cid,token,NEW)
    assert db.session.get(User,old())


def test_backup_failure_no_changes(configured):
    cid,_=ready()
    with patch.object(change,'backup_locked',side_effect=OSError('disk full')),pytest.raises(OSError):
        apply(cid)
    assert db.session.get(User,old()) and not db.session.get(User,NEW)
    assert not db.session.get(IdentityEmailReservation,NEW)


def test_audit_failure_rolls_back_all_updates(configured):
    cid,_=ready()
    with db.engine.begin() as conn:
        conn.exec_driver_sql("CREATE TRIGGER fail_receipt BEFORE UPDATE ON identity_email_changes WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
    from sqlalchemy.exc import SQLAlchemyError
    with pytest.raises(SQLAlchemyError): apply(cid)
    assert db.session.get(User,old()) and not db.session.get(User,NEW)
    assert not db.session.get(IdentityEmailReservation,NEW)


def test_idempotent_confirmation(configured):
    cid,_=ready(); plan,token=change.preview(actor(),cid)
    first=change.execute(actor(),cid,token,NEW)
    assert change.execute(actor(),cid,token,NEW)==first
    assert len(list(Path(configured.config['IDENTITY_EMAIL_BACKUP_DIR']).glob('*.sqlite3')))==1


def test_reference_drift_fails_closed(configured):
    cid,_=ready()
    with db.engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE future_account_data (owner TEXT REFERENCES users(id))')
    with pytest.raises(ValueError,match='coverage'):
        change.preview(actor(),cid)


def test_missing_signal_key_with_markers_fails_closed(configured):
    from services import identity_signup as signup
    with db.engine.begin() as conn:
        conn.execute(insert(signup.M).values(identity_hash='marker',key_id='unknown',reason='deleted',generation='g',created_at=change.now()))
    with pytest.raises(ValueError):
        change.create(actor(),old(),NEW,'Account owner request')


def test_unfinished_saved_match_blocks(configured):
    cid,_=ready()
    path=Path(configured.config['IDENTITY_EMAIL_MATCH_DIR'])/'match_test.json'
    path.write_text(json.dumps({'created_by':old(),'current_state':'live','match_id':'test'}))
    with pytest.raises(ValueError,match='Finish and archive'):
        change.preview(actor(),cid)


def test_cross_process_admission_blocks_operation(configured):
    cid,_=ready()
    with change.admission(),pytest.raises(ValueError,match='activity'):
        apply(cid)


def test_mail_failures_visible_and_retryable(configured):
    with patch('utils.email_service.send_email',return_value=False):
        cid=change.create(actor(),old(),NEW,'Account owner request')
    with db.engine.connect() as conn:
        assert change.get_change(conn,cid)['proof_delivery']=='failed'
    change.cancel(actor(),cid)
    cid,_=ready()
    with patch('utils.email_service.send_email',return_value=False): apply(cid)
    with db.engine.connect() as conn:
        assert not change.get_change(conn,cid,False)['new_notice_sent']
    change.notify_completion(cid)
    with db.engine.connect() as conn:
        assert change.get_change(conn,cid,False)['new_notice_sent']


def test_backup_cleanup_30_days(configured):
    directory=change.directory()
    stale=directory/'email-test.sqlite3'; stale.write_text('test')
    fresh=directory/'email-fresh.sqlite3'; fresh.write_text('test')
    unrelated=directory/'other.sqlite3'; unrelated.write_text('test')
    timestamp=(change.now()-timedelta(days=31)).timestamp()
    os.utime(stale,(timestamp,timestamp))
    assert change.purge_backups()==1
    assert fresh.exists() and unrelated.exists()


def test_routes_permissions_csrf_get_safety(configured,admin_client):
    with configured.app_context(): member=logged_in_client(configured,'user1')
    assert call(configured,member,'/admin/identity/email-changes').status_code==403
    assert call(configured,admin_client,'/admin/identity/email-changes').status_code==200
    configured.config['WTF_CSRF_ENABLED']=True
    assert call(configured,admin_client,'/admin/identity/email-changes',method='post').status_code==400
    configured.config['WTF_CSRF_ENABLED']=False
    cid,tokens=ready()
    path=f"/identity/email-change/verify/{cid}/new/{tokens['new']}"
    assert call(configured,configured.test_client(),path).status_code==200
    assert call(configured,configured.test_client(),path,method='post').status_code==400
    assert call(configured,admin_client,f'/admin/identity/email-changes/{cid}').status_code==200
    response=call(configured,admin_client,f'/admin/identity/email-changes/{cid}',method='post',data={'action':'preview'})
    assert response.status_code==200 and b'Review impact' in response.data


def test_migration_idempotence(configured):
    from migrations.add_identity_email_change import run_migration
    run_migration(db,configured);run_migration(db,configured)


def test_legacy_writer_no_longer_bypasses_proof(configured):
    from auth.user_auth import update_user_email
    assert not update_user_email(old(),NEW)[0]
    assert db.session.get(User,old())


def test_mandatory_self_correction_verifies_before_repointing(configured):
    user=db.session.get(User,old());user.force_email_verify=True;db.session.commit()
    tokens={}
    with patch.object(change,'deliver_proofs',side_effect=lambda cid,t: tokens.update(t)):
        cid=change.create(user.stable_id,old(),NEW,'Mandatory correction requested by owner.',
            'Authenticated mandatory correction exception.',self_service=True)
    assert db.session.get(User,old()) and not db.session.get(User,NEW)
    with pytest.raises(ValueError,match='verification'):
        change.complete_mandatory(cid)
    change.verify(cid,'new',tokens['new'])
    assert change.complete_mandatory(cid)
    assert db.session.get(User,NEW).email_verified
    assert not db.session.get(User,NEW).force_email_verify


def test_member_cannot_claim_self_correction(configured):
    user=db.session.get(User,old())
    with pytest.raises(ValueError,match='not required'):
        change.create(user.stable_id,old(),NEW,'Attempt voluntary change by owner.',
            'Unsupported mandatory correction exception.',self_service=True)


def test_mandatory_correction_post_and_public_confirmation(configured):
    with db.engine.begin() as conn:
        conn.execute(update(User).where(User.id==old()).values(force_email_verify=True))
    with configured.app_context(): client=logged_in_client(configured,'user1')
    captured={}
    with patch.object(change,'deliver_proofs',side_effect=lambda cid,t:captured.update(id=cid,tokens=t)):
        response=call(configured,client,'/force-verify-email/change-email',method='post',data={'new_email':NEW})
    assert response.status_code==302
    path=f"/identity/email-change/verify/{captured['id']}/new/{captured['tokens']['new']}"
    response=call(configured,client,path,method='post')
    assert response.status_code==200 and b'Your email was changed' in response.data


def test_proof_get_does_not_consume_and_tokens_are_hashed(configured):
    captured={}
    with patch.object(change,'deliver_proofs',side_effect=lambda cid,t:captured.update(t)):
        cid=change.create(actor(),old(),NEW,'Account owner request')
    client=configured.test_client()
    path=f"/identity/email-change/verify/{cid}/new/{captured['new']}"
    assert call(configured,client,path).status_code==200
    with db.engine.connect() as conn:
        row=change.get_change(conn,cid)
        assert not row['new_verified_at'] and captured['new'] not in str(row)
    assert call(configured,client,path,method='post').status_code==200


def test_nonadmin_and_impersonation_cannot_create(configured,admin_client):
    member=db.session.get(User,old())
    with pytest.raises(ValueError,match='administrator'):
        change.create(member.stable_id,old(),NEW,'Unprivileged email change')
    with admin_client.session_transaction() as session:
        session['impersonating_from']=ACCOUNTS['admin']['email']
    assert call(configured,admin_client,'/admin/identity/email-changes').status_code==403


def test_same_canonical_rename_and_legacy_reservation(configured):
    # Case/dot aliases are allowed only for the SAME account; no duplicate user.
    with db.engine.begin() as conn:
        conn.execute(update(User).where(User.id==old()).values(id='q.a.owner@gmail.com',canonical_email='qaowner@gmail.com'))
        conn.execute(insert(IdentityEmailReservation).values(canonical_email='qaowner@gmail.com'))
    tokens={}
    with patch.object(change,'deliver_proofs',side_effect=lambda cid,t:tokens.update(t)):
        cid=change.create(actor(),'q.a.owner@gmail.com','qaowner@googlemail.com','Provider-specific alias change')
    change.verify(cid,'old',tokens['old']);change.verify(cid,'new',tokens['new'])
    _,token=change.preview(actor(),cid)
    change.execute(actor(),cid,token,'qaowner@googlemail.com')
    assert db.session.get(User,'qaowner@googlemail.com')


def test_archived_snapshot_follows_db_owner_not_reused_email(configured):
    from database.models import Match
    from utils.match_ownership import owns_match_snapshot
    snapshot={'match_id':'qa-archive', 'created_by':old(), 'current_state':'completed'}
    db.session.add(Match(id='qa-archive',user_id=old()))
    db.session.commit()
    path=Path(configured.config['IDENTITY_EMAIL_MATCH_DIR'])/'match_qa-archive.json'
    path.write_text(json.dumps(snapshot))
    original=path.read_bytes()
    cid,_=ready();apply(cid)
    assert path.read_bytes()==original
    assert owns_match_snapshot(snapshot, NEW)
    assert not owns_match_snapshot(snapshot, old())
    assert db.session.get(IdentityEmailReservation,change.normalize_email(old()).canonical)


def test_backup_is_wal_consistent(configured):
    import sqlite3
    db.session.remove()
    with db.engine.connect() as conn:
        conn.exec_driver_sql('PRAGMA journal_mode=WAL')
    with db.engine.begin() as conn:
        conn.execute(update(User).where(User.id==old()).values(display_name='WAL-only committed update'))
    cid,_=ready();receipt=apply(cid)
    with sqlite3.connect(Path(configured.config['IDENTITY_EMAIL_BACKUP_DIR'])/receipt['backup']) as conn:
        assert conn.execute('select display_name from users where id=?',(old(),)).fetchone()[0]=='WAL-only committed update'


def test_preview_expiry_and_confirmation_mismatch(configured):
    cid,_=ready();_,token=change.preview(actor(),cid)
    with pytest.raises(ValueError,match='exact new email'):
        change.execute(actor(),cid,token,NEW.upper())
    with patch('itsdangerous.timed.time.time',return_value=__import__('time').time()+901),pytest.raises(ValueError,match='expired'):
        change.execute(actor(),cid,token,NEW)


def test_marker_blocks_new_email_with_signup_off(configured):
    from services import identity_signup as signup
    from utils import identity_devices as devices
    configured.config.update(IDENTITY_SIGNUP_ENFORCE=False,IDENTITY_SIGNUP_KEY='s'*40)
    with db.engine.begin() as conn:
        conn.execute(insert(signup.M).values(identity_hash=signup.identity_hash(change.normalize_email(NEW)),
            key_id=devices.key_id('s'*40),reason='deleted',generation='g',created_at=change.now()))
    with pytest.raises(ValueError,match='review'):
        change.create(actor(),old(),NEW,'Account owner request')


def test_two_processes_cannot_repoint_to_same_email(configured):
    import subprocess
    import sys
    jobs=[]
    for email in [old(),ACCOUNTS['user2']['email']]:
        captured={}
        with patch.object(change,'deliver_proofs',side_effect=lambda cid,t:captured.update(t)):
            cid=change.create(actor(),email,NEW,'Account owner requested this change')
        change.verify(cid,'old',captured['old']);change.verify(cid,'new',captured['new'])
        _,token=change.preview(actor(),cid)
        jobs.append((cid,token))
    script='''
import sys
from flask import Flask
from database import db
from services import identity_email_change as change
from utils import email_service
app=Flask('email-race',instance_path=sys.argv[2])
app.config.update(SQLALCHEMY_DATABASE_URI=sys.argv[1],SECRET_KEY='test-secret-key',IDENTITY_EMAIL_BACKUP_DIR=sys.argv[3],IDENTITY_EMAIL_MATCH_DIR=sys.argv[4])
db.init_app(app)
email_service.send_email=lambda *a,**kw: True
with app.app_context():
    print('READY',flush=True)
    input()
    try:
        change.execute(sys.argv[5],sys.argv[6],sys.argv[7],sys.argv[8])
        print('changed')
    except ValueError:
        print('refused')
'''
    args=[str(db.engine.url),configured.instance_path,configured.config['IDENTITY_EMAIL_BACKUP_DIR'],
          configured.config['IDENTITY_EMAIL_MATCH_DIR'],actor()]
    processes=[subprocess.Popen([sys.executable,'-c',script,*args,cid,token,NEW],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for cid,token in jobs]
    try:
        for proc in processes:
            assert proc.stdout.readline().strip()=='READY'
        for proc in processes:
            proc.stdin.write('go\n');proc.stdin.flush()
        outputs=[proc.communicate(timeout=30) for proc in processes]
        assert all(proc.returncode==0 for proc in processes),outputs
        assert sorted(out.strip() for out,_ in outputs)==['changed','refused'],outputs
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill();proc.wait()
    db.session.expire_all()
    assert db.session.query(User).filter_by(id=NEW).count()==1
    assert len(list(Path(configured.config['IDENTITY_EMAIL_BACKUP_DIR']).glob('*.sqlite3')))==1


def test_unknown_non_fk_reference_fails_coverage(configured):
    from sqlalchemy import Table,Column,String
    table=Table('future_reference',db.metadata,Column('user_email',String))
    try:
        cid,_=ready()
        with pytest.raises(ValueError,match='Non-FK'):
            change.preview(actor(),cid)
    finally:
        db.metadata.remove(table)
