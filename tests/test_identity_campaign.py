"""Campaign tests use only shared QA users in isolated file-backed databases."""
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
import json

import pytest
from sqlalchemy import select, update, insert, delete
from database import db
from database.models import User, Team, CommunityPost, CommunityComment, CommunityImage, ActiveSession
from scripts.dev_test_accounts import ACCOUNTS, logged_in_client
from services import identity_campaign as c, identity_email_change as changes


@pytest.fixture
def configured(app, admin_client, tmp_path, monkeypatch):
    app.config.update(IDENTITY_EMAIL_BACKUP_DIR=str(tmp_path/'backups'),
        IDENTITY_EMAIL_MATCH_DIR=str(tmp_path/'matches'), ARCHIVES_FOLDER=str(tmp_path/'archives'),
        COMMUNITY_MEDIA_DIR=str(tmp_path/'media'), IDENTITY_SIGNUP_ENFORCE=True, IDENTITY_SIGNUP_KEY='campaign-test-key-'*4)
    for name in ('matches','archives','media'):
        (tmp_path/name).mkdir()
    monkeypatch.setattr('utils.email_service.send_email', lambda *a, **k: True)
    return app


def actor():
    return db.session.get(User, ACCOUNTS['admin']['email']).stable_id


def subject():
    return db.session.get(User, ACCOUNTS['user1']['email']).stable_id


def enroll():
    # Keep shared fixture addresses usable by logged_in_client. Exercise actual
    # policy separately; emulate a disposable-list finding for this QA address.
    original = c.validate_new_email
    def policy(value):
        if value == ACCOUNTS['user1']['email']:
            raise c.InvalidEmail('disposable_domain')
        return original(value)
    with patch.object(c, 'validate_new_email', side_effect=policy):
        plan, token = c.preview(actor())
        cid = c.activate(actor(), token)
    return cid, c.pending(subject())['id']


def expire(monkeypatch, cid):
    deadline = db.session.execute(select(c.C.c.deadline).where(c.C.c.id == cid)).scalar_one()
    monkeypatch.setattr(c, 'now', lambda: deadline)
    return deadline


def enable(cid):
    c.enable_deletion(actor(), cid, True)


def test_campaign_fixed_clock_and_frozen_membership(configured, monkeypatch):
    at = datetime(2026, 11, 8, 12)
    monkeypatch.setattr(c,'now',lambda:at)
    cid, mid = enroll()
    row = c.pending(subject())
    assert row['deadline'] == at+timedelta(days=30)
    assert db.session.get(User,ACCOUNTS['user1']['email']).force_email_verify
    monkeypatch.setattr(c,'now',lambda:at+timedelta(days=5))
    assert c.pending(subject())['deadline'] == row['deadline']
    assert db.session.execute(select(c.M)).all().__len__() == 1
    with pytest.raises(ValueError,match='current campaign'):
        c.activate(actor(), c.preview(actor())[1])


@pytest.mark.parametrize('address,reason', [('bad','invalid_syntax'),('a+tag@example.com','plus_tag_not_allowed'),
    ('a@mailinator.com','disposable_domain'),('ü@example.com','non_ascii_local')])
def test_real_legacy_policy(configured,address,reason):
    with changes.transaction() as conn:
        conn.execute(update(User).where(User.id==ACCOUNTS['user1']['email']).values(id=address))
    with db.engine.connect() as conn:
        result=c.inspect_candidates(conn)
    assert any(r['address']==address and r['reason']==reason for r in result['members'])


def test_unverified_alone_not_enrolled(configured):
    user=db.session.get(User,ACCOUNTS['user1']['email']); user.email_verified=False; db.session.commit()
    assert c.preview(actor())[0]['members']==[]


def test_stale_wrong_actor_preview(configured):
    _,token=c.preview(actor())
    with pytest.raises(ValueError,match='fresh preview'):
        with patch.object(c,'inspect_candidates',return_value={'members':[1]}):
            c.activate(actor(),token)
    with pytest.raises(ValueError):
        c.activate(subject(), token)


def test_deletion_disabled_and_boundary_hold(configured,monkeypatch):
    cid,mid=enroll(); deadline=expire(monkeypatch,cid)
    assert c.delete_member(mid) is False
    enable(cid)
    c.administer(actor(),mid,'hold','Support investigation is ongoing',2)
    assert c.delete_member(mid) is False
    monkeypatch.setattr(c,'now',lambda:deadline+timedelta(days=2))
    assert c.delete_member(mid)
    assert not c.delete_member(mid)
    assert db.session.get(User,ACCOUNTS['user1']['email']) is None


def test_not_before_deadline_even_enabled(configured,monkeypatch):
    cid,mid=enroll(); enable(cid)
    deadline=expire(monkeypatch,cid)
    monkeypatch.setattr(c,'now',lambda:deadline-timedelta(microseconds=1))
    assert not c.delete_member(mid)


@pytest.mark.parametrize('days',[0,31,'bad'])
def test_hold_limits(configured,days):
    _,mid=enroll()
    with pytest.raises(ValueError,match='1–30'):
        c.administer(actor(),mid,'hold','Reviewing ownership issue',days)


def test_support_not_automatic_hold_and_private(configured,admin_client):
    _,mid=enroll(); uid=subject()
    c.member_action(uid,'Cannot access the replacement mailbox yet')
    assert c.pending(uid)['hold_until'] is None
    client=logged_in_client(configured,'user1')
    assert client.get('/identity/email-campaign/support').status_code==200
    stranger=logged_in_client(configured,'user2')
    assert stranger.get('/identity/email-campaign/support').status_code==404
    assert stranger.get('/admin/identity/email-campaign/'+mid).status_code in (302,403)
    assert admin_client.get('/admin/identity/email-campaign/'+mid).status_code==200


def test_http_gate_all_registered_endpoints(configured):
    enroll(); client=logged_in_client(configured,'user1')
    # Test the early server guard itself against every registered endpoint,
    # including routes that have parameters or would mutate unrelated data.
    from flask_login import login_user
    user=db.session.get(User,ACCOUNTS['user1']['email'])
    for endpoint in configured.view_functions:
        with configured.test_request_context('/api/probe',method='POST',json={}):
            login_user(user)
            from flask import request
            request.url_rule=type('Rule',(),{'endpoint':endpoint})()
            response=c.guard()
            assert (response is None) == (endpoint in c.ALLOWED), endpoint
    assert client.get('/api/community/posts').status_code==403
    assert client.get('/force-verify-email').status_code==200
    assert b'Replacement email required' in client.get('/force-verify-email').data
    assert client.post('/force-verify-email/send').status_code==302


def test_support_ban_and_muted_unverified(configured):
    enroll(); user=db.session.get(User,ACCOUNTS['user1']['email']); user.email_verified=False
    user.community_muted_until=datetime.utcnow()+timedelta(days=5)
    db.session.commit()
    client=logged_in_client(configured,'user1')
    assert client.post('/identity/email-campaign/support',data={'message':'Help with my replacement email'}).status_code==303
    user.is_banned=True; db.session.commit()
    assert client.get('/identity/email-campaign/support').status_code==403


def test_backup_failure_preserves_everything(configured,monkeypatch):
    cid,mid=enroll(); enable(cid); expire(monkeypatch,cid)
    with patch.object(changes,'backup_locked',side_effect=OSError('disk')):
        with pytest.raises(OSError):c.delete_member(mid)
    assert db.session.get(User,ACCOUNTS['user1']['email'])
    assert not db.session.execute(select(c.F)).first()
    assert c.pending(subject())


def test_audit_failure_rolls_back_delete(configured,monkeypatch):
    cid,mid=enroll(); enable(cid); expire(monkeypatch,cid)
    with patch.object(c,'audit',side_effect=RuntimeError('audit')):
        with pytest.raises(RuntimeError):c.delete_member(mid)
    assert db.session.get(User,ACCOUNTS['user1']['email'])


def test_corrected_at_boundary_wins(configured,monkeypatch):
    cid,mid=enroll(); enable(cid); expire(monkeypatch,cid)
    tokens={}
    with patch.object(changes,'deliver_proofs',side_effect=lambda cid,proofs:tokens.update(proofs)):
        change=changes.create(subject(),ACCOUNTS['user1']['email'],'qa.campaign-fixed@example.com',
            'Mandatory correction requested by owner','Authenticated account owner mandatory correction exception',self_service=True)
    changes.verify(change,'new',tokens['new'])
    assert changes.complete_mandatory(change)
    assert not c.delete_member(mid)
    row=db.session.execute(select(c.M).where(c.M.c.id==mid)).mappings().one()
    assert row['status']=='corrected' and row['address'] is None


def test_old_mailbox_proof_cannot_clear_campaign(configured):
    enroll(); user=db.session.get(User,ACCOUNTS['user1']['email'])
    user.email_verify_token=changes.digest('old-token'); user.email_verify_token_expires=datetime.utcnow()+timedelta(hours=1)
    db.session.commit()
    response=configured.test_client().get('/verify-email?token=old-token')
    assert response.status_code==302
    db.session.expire_all()
    assert db.session.get(User,ACCOUNTS['user1']['email']).force_email_verify


def test_deletion_data_and_private_public_content(configured,monkeypatch):
    owner=ACCOUNTS['user1']['email']; other=ACCOUNTS['user2']['email']
    db.session.add(Team(name='Owned QA',short_code='QA',user_id=owner))
    public=CommunityPost(public_id='pub-test',author_id=owner,flair='question',title='Public discussion',body='Keep text',visibility='public',user_agent='private-agent')
    private=CommunityPost(public_id='pri-test',author_id=owner,flair='question',title='Private discussion',body='Private body',visibility='private')
    db.session.add_all([public,private]);db.session.flush()
    db.session.add(CommunityComment(post_id=public.id,author_id=other,body='Other member reply'))
    db.session.commit(); pubid=public.id; priid=private.id
    cid,mid=enroll(); enable(cid); expire(monkeypatch,cid)
    assert c.delete_member(mid)
    assert db.session.query(Team).count()==0
    public=db.session.get(CommunityPost,pubid)
    assert public and public.author_id is None and public.user_agent is None
    assert db.session.get(CommunityPost,priid) is None
    assert db.session.query(CommunityComment).one().author_id==other
    from services.identity_signup import M
    assert db.session.execute(select(M.c.reason)).scalar_one()=='deleted'
    assert list(Path(configured.config['IDENTITY_EMAIL_BACKUP_DIR']).glob('email-campaign-*.sqlite3'))


def test_file_cleanup_retry_and_access_denial(configured,monkeypatch):
    owner=ACCOUNTS['user1']['email']
    root=Path(configured.config['IDENTITY_EMAIL_MATCH_DIR'])
    snapshot={'created_by':owner,'match_id':'campaign-test-match','current_state':'in_progress'}
    (root/'campaign-test.json').write_text(json.dumps(snapshot))
    archives=Path(configured.config['ARCHIVES_FOLDER']); archive=archives/('match_'+owner+'_test.zip');archive.write_bytes(b'zip')
    cid,mid=enroll();enable(cid);expire(monkeypatch,cid)
    with patch.object(Path,'unlink',side_effect=OSError('busy')):
        assert c.delete_member(mid)
    assert db.session.execute(select(c.F).where(c.F.c.cleaned_at.is_(None))).first()
    from utils.match_ownership import owns_match_snapshot, owns_archive
    assert not owns_match_snapshot(snapshot,owner)
    assert not owns_archive(archive,owner)
    c.cleanup_files()
    assert not archive.exists() and not (root/'campaign-test.json').exists()


def test_reminder_failures_retry_schedule(configured,monkeypatch):
    cid,mid=enroll()
    with patch('utils.email_service.send_email',return_value=False) as send:
        c.send_notices();assert send.call_count==1
    with patch('utils.email_service.send_email',return_value=True) as send:
        c.send_notices();c.send_notices();assert send.call_count==1
        deadline=db.session.execute(select(c.C.c.deadline)).scalar_one()
        monkeypatch.setattr(c,'now',lambda:deadline-timedelta(days=7))
        c.send_notices();assert send.call_count==2
        monkeypatch.setattr(c,'now',lambda:deadline-timedelta(days=1))
        c.send_notices();assert send.call_count==3
        monkeypatch.setattr(c,'now',lambda:deadline)
        c.send_notices();assert send.call_count==3


def test_admin_promotion_excludes_deletion(configured,monkeypatch):
    cid,mid=enroll();enable(cid);expire(monkeypatch,cid)
    user=db.session.get(User,ACCOUNTS['user1']['email']);user.is_admin=True;db.session.commit()
    assert not c.delete_member(mid)
    db.session.expire_all()
    assert db.session.get(User,ACCOUNTS['user1']['email']).force_email_verify is False


def test_retention(configured,monkeypatch):
    cid,mid=enroll();enable(cid);at=expire(monkeypatch,cid);c.delete_member(mid)
    monkeypatch.setattr(c,'now',lambda:at+timedelta(days=180))
    assert c.purge()==1
    assert not db.session.execute(select(c.M)).first()
    assert not db.session.execute(select(c.C)).first()
    assert not db.session.execute(select(c.A)).first()
    from services.identity_signup import M
    assert db.session.execute(select(M)).first()


def test_migration_idempotent(configured):
    from migrations.add_identity_campaign import run_migration
    cid,mid=enroll()
    run_migration(db,configured);run_migration(db,configured)
    assert c.pending(subject())['id']==mid


def test_socket_live_campaign_and_deleted_session(configured,monkeypatch):
    client=logged_in_client(configured,'user1')
    ws=configured.extensions['socketio'].test_client(configured,flask_test_client=client)
    assert ws.is_connected()
    try:
        cid,mid=enroll()
        ws.emit('next_ball',{'match_id':'must-not-advance'})
        assert any(e['name']=='ws_error' and e['args'][0]['code']=='email_correction_required' for e in ws.get_received())
        enable(cid);expire(monkeypatch,cid);c.delete_member(mid)
        ws.emit('next_ball',{'match_id':'must-not-advance'})
        assert any(e['name']=='ws_error' and e['args'][0]['code']=='auth_required' for e in ws.get_received())
    finally:
        if ws.is_connected():ws.disconnect()


def test_admin_csrf_impersonation_ban(configured,admin_client):
    configured.config['WTF_CSRF_ENABLED']=True
    assert admin_client.post('/admin/identity/email-campaign',data={'action':'preview'}).status_code==400
    configured.config['WTF_CSRF_ENABLED']=False
    with admin_client.session_transaction() as saved:saved['impersonating_from']=ACCOUNTS['user2']['email']
    assert admin_client.get('/admin/identity/email-campaign').status_code==403
    with admin_client.session_transaction() as saved:saved.pop('impersonating_from')
    admin=db.session.get(User,ACCOUNTS['admin']['email']);admin.is_banned=True;db.session.commit()
    assert admin_client.get('/admin/identity/email-campaign').status_code==403


def test_campaign_restriction_survives_flag_clear(configured):
    enroll();u=db.session.get(User,ACCOUNTS['user1']['email']);u.force_email_verify=False;db.session.commit()
    client=logged_in_client(configured,'user1')
    assert client.get('/force-verify-email').status_code==200
    assert client.get('/api/community/posts').status_code==403
    with patch.object(changes,'deliver_proofs'):
        assert changes.create(subject(),ACCOUNTS['user1']['email'],'qa.changed@example.com',
            'Mandatory correction requested','Authenticated mandatory correction exception',self_service=True)


def test_invalid_admin_excluded_and_collision_only_review(configured):
    # Test using the five existing QA identities without creating accounts.
    with changes.transaction() as conn:
        conn.exec_driver_sql('PRAGMA defer_foreign_keys = ON')
        conn.execute(update(ActiveSession).where(ActiveSession.user_id==ACCOUNTS['admin']['email']).values(user_id='qa.admin+tag@example.com'))
        conn.execute(update(User).where(User.id==ACCOUNTS['admin']['email']).values(id='qa.admin+tag@example.com'))
        conn.execute(update(User).where(User.id==ACCOUNTS['user1']['email']).values(id='qa.rohit@gmail.com'))
        conn.execute(update(User).where(User.id==ACCOUNTS['user2']['email']).values(id='qarohit@googlemail.com'))
    with db.engine.connect() as conn:plan=c.inspect_candidates(conn)
    assert len(plan['admins'])==1 and len(plan['collisions'])==1 and plan['members']==[]


def test_file_backup_failure_prevents_deletion(configured,monkeypatch):
    path=Path(configured.config['IDENTITY_EMAIL_MATCH_DIR'])/'owned.json'
    path.write_text(json.dumps({'created_by':ACCOUNTS['user1']['email'],'match_id':'owned'}))
    cid,mid=enroll();enable(cid);expire(monkeypatch,cid)
    with patch.object(c,'backup_artifacts',side_effect=OSError('disk full')):
        with pytest.raises(OSError):c.delete_member(mid)
    assert path.exists() and db.session.get(User,ACCOUNTS['user1']['email'])


def test_changed_cleanup_file_is_preserved_and_receipt_retained(configured,monkeypatch):
    path=Path(configured.config['IDENTITY_EMAIL_MATCH_DIR'])/'owned.json'
    path.write_text(json.dumps({'created_by':ACCOUNTS['user1']['email'],'match_id':'owned'}))
    cid,mid=enroll();enable(cid);at=expire(monkeypatch,cid)
    with patch.object(c,'cleanup_files'):c.delete_member(mid)
    path.write_text('different content')
    c.cleanup_files()
    assert path.read_text()=='different content'
    monkeypatch.setattr(c,'now',lambda:at+timedelta(days=181))
    assert c.purge()==0


def test_deletion_requires_return_enforcement(configured):
    cid,_=enroll();configured.config['IDENTITY_SIGNUP_ENFORCE']=False
    with pytest.raises(ValueError,match='signup identity enforcement'):enable(cid)


def test_complete_gameplay_graph_deletion(configured,monkeypatch):
    from database.models import TeamProfile, Player, Match, MatchScorecard, Tournament, TournamentTeam, TournamentFixture
    owner=ACCOUNTS['user1']['email'];other=ACCOUNTS['user2']['email']
    a=Team(name='QA owned',short_code='QAO',user_id=owner);b=Team(name='QA other',short_code='QAB',user_id=other)
    db.session.add_all([a,b]);db.session.flush()
    tournament=Tournament(name='QA tournament',user_id=owner,mode='round_robin',format_type='T20')
    db.session.add(tournament);db.session.flush()
    match=Match(id='campaign-full-match',user_id=owner,home_team_id=a.id,away_team_id=b.id,tournament_id=tournament.id)
    db.session.add(match);db.session.commit();other_id=b.id
    cid,mid=enroll();enable(cid);expire(monkeypatch,cid)
    assert c.delete_member(mid)
    assert db.session.get(Team,other_id)
    assert not db.session.get(Match,'campaign-full-match')
    assert db.session.query(Tournament).count()==0


def test_remediation_with_network_and_password_restrictions(configured):
    enroll();user=db.session.get(User,ACCOUNTS['user1']['email']);user.force_password_reset=True;db.session.commit()
    configured.config['IDENTITY_NETWORK_ENFORCE']=True
    client=logged_in_client(configured,'user1')
    assert client.get('/identity/email-campaign/support').status_code==200
    assert client.get('/force-verify-email').status_code==200
    assert client.get('/change-password').status_code==200


def test_unverified_campaign_login_reaches_correction(configured):
    from scripts.dev_test_accounts import load_credentials
    enroll();user=db.session.get(User,ACCOUNTS['user1']['email']);user.email_verified=False;db.session.commit()
    response=configured.test_client().post('/login',data={'email':user.id,
        'password':load_credentials()['accounts']['user1']['password']})
    assert response.status_code==302 and response.location.endswith('/force-verify-email')


def test_private_reply_removal_preserves_other_replies_and_vote_counts(configured,monkeypatch):
    from database.models import CommunityVote
    owner=ACCOUNTS['user1']['email'];other=ACCOUNTS['user2']['email']
    post=CommunityPost(public_id='other-private',author_id=other,flair='question',title='Other thread',body='Keep',visibility='private',vote_count=1,score=1)
    db.session.add(post);db.session.flush()
    reply=CommunityComment(post_id=post.id,author_id=owner,body='Remove private reply')
    db.session.add(reply);db.session.flush()
    child=CommunityComment(post_id=post.id,author_id=other,parent_id=reply.id,body='Keep other reply')
    db.session.add_all([child,CommunityVote(post_id=post.id,user_id=owner,value=1)]);db.session.commit()
    pid,rid,childid=post.id,reply.id,child.id
    cid,mid=enroll();enable(cid);expire(monkeypatch,cid);assert c.delete_member(mid)
    assert db.session.get(CommunityComment,rid) is None
    assert db.session.get(CommunityComment,childid).body=='Keep other reply'
    post=db.session.get(CommunityPost,pid)
    assert post.score==0 and post.vote_count==0 and post.comment_count==1


def test_simultaneous_deletion_is_single_receipt(configured,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    cid,mid=enroll();enable(cid);expire(monkeypatch,cid)
    barrier=Barrier(2)
    def worker():
        with configured.app_context():
            barrier.wait(timeout=5)
            try:return c.delete_member(mid)
            except ValueError:return False  # Nonblocking admission contention is retryable.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:worker(),range(2)))
    assert sum(results)==1
    assert db.session.execute(select(c.A).where(c.A.c.member_id==mid,c.A.c.action=='deleted')).all().__len__()==1


def test_file_backup_restorable_and_expiry_cleanup(configured,monkeypatch):
    import zipfile, os
    from datetime import timezone
    path=Path(configured.config['IDENTITY_EMAIL_MATCH_DIR'])/'owned.json'
    raw=json.dumps({'created_by':ACCOUNTS['user1']['email'],'match_id':'owned'})
    path.write_text(raw)
    cid,mid=enroll();enable(cid);at=expire(monkeypatch,cid);assert c.delete_member(mid)
    receipt=json.loads(db.session.execute(select(c.M.c.receipt).where(c.M.c.id==mid)).scalar_one())
    backup=Path(configured.config['IDENTITY_EMAIL_BACKUP_DIR'])/receipt['artifact_backup']
    assert backup.stat().st_mode & 0o777==0o600
    with zipfile.ZipFile(backup) as z:assert z.read('matches/owned.json').decode()==raw
    stamp=at.replace(tzinfo=timezone.utc).timestamp();os.utime(backup,(stamp,stamp))
    monkeypatch.setattr(c,'now',lambda:at+timedelta(days=30))
    assert c.purge_artifact_backups()==1 and not backup.exists()
