"""Feature 10: isolated databases, shared QA identities, mocked outbound mail."""
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from sqlalchemy import select, update, insert, text
from database import db
from database.models import User, Team, IdentityReviewCase, ActiveSession, CommunityPost, CommunityVote
from scripts.dev_test_accounts import ACCOUNTS, logged_in_client
from services import identity_merge as merge

SOURCE = ACCOUNTS['user1']['email']
SURVIVOR = ACCOUNTS['user2']['email']


@pytest.fixture
def configured(app, admin_client, tmp_path, monkeypatch):
    matches, archives = tmp_path/'matches', tmp_path/'archives'
    matches.mkdir(); archives.mkdir()
    app.config.update(IDENTITY_EMAIL_BACKUP_DIR=str(tmp_path/'backups'), IDENTITY_EMAIL_MATCH_DIR=str(matches),
                      ARCHIVES_FOLDER=str(archives), IDENTITY_SIGNUP_ENFORCE=True, IDENTITY_SIGNUP_KEY='merge-test-key-not-production-000000000000')
    monkeypatch.setattr('utils.email_service.send_email', lambda *a, **kw: True)
    a, b = [db.session.get(User, address) for address in (SOURCE, SURVIVOR)]
    db.session.add(IdentityReviewCase(left_id=a.stable_id, right_id=b.stable_id, status='investigating',
                                    created_at=merge.base.now(), updated_at=merge.base.now()))
    db.session.commit()
    return app


def actor():
    return db.session.get(User, ACCOUNTS['admin']['email']).stable_id


def ready(recovery=''):
    tokens = {}
    cid = db.session.query(IdentityReviewCase).first().id
    with patch.object(merge, 'deliver', side_effect=lambda mid, t: tokens.update(t)):
        mid = merge.create(actor(), cid, SOURCE, SURVIVOR, 'Common ownership established through member request and proofs.', recovery)
    if not recovery:
        merge.verify(mid, 'source', tokens['source'])
    merge.verify(mid, 'survivor', tokens['survivor'])
    return mid, tokens


def apply(mid):
    plan, token = merge.preview(actor(), mid)
    return merge.execute(actor(), mid, token, SURVIVOR)


def test_merge_and_idempotent_receipt(configured):
    s = db.session.get(User, SURVIVOR)
    stable, password = s.stable_id, s.password_hash
    db.session.add_all([Team(name='Same', short_code='QA', user_id=SOURCE), Team(name='Same', short_code='QA', user_id=SURVIVOR)])
    s.reset_token='revoke-me'
    db.session.commit()
    mid, _ = ready()
    plan, token = merge.preview(actor(), mid)
    assert len(plan['renames']) == 2
    result = merge.execute(actor(), mid, token, SURVIVOR)
    assert merge.execute(actor(), mid, token, SURVIVOR) == result
    assert db.session.get(User, SOURCE) is None
    s = db.session.get(User, SURVIVOR)
    assert (s.stable_id, s.password_hash) == (stable, password)
    assert s.reset_token is None
    assert db.session.query(Team).filter_by(user_id=SURVIVOR).count() == 2
    assert db.session.query(IdentityReviewCase).one().status == 'resolved'
    assert db.session.execute(select(merge.signup.M.c.reason)).scalar_one() == 'merged'
    backup = Path(configured.config['IDENTITY_EMAIL_BACKUP_DIR']) / result['backup']
    assert backup.exists() and backup.stat().st_mode & 0o777 == 0o600
    import sqlite3
    with sqlite3.connect(backup) as conn:
        assert conn.execute('select count(*) from users where id=?',(SOURCE,)).fetchone()[0] == 1


@pytest.mark.parametrize('field', ['is_admin', 'is_banned', 'force_email_verify', 'force_password_reset'])
@pytest.mark.parametrize('address', [SOURCE, SURVIVOR])
def test_restricted_accounts_rejected(configured, field, address):
    setattr(db.session.get(User, address), field, True)
    db.session.commit()
    with pytest.raises(ValueError):
        ready()


def test_stale_preview(configured):
    mid, _ = ready()
    _, token = merge.preview(actor(), mid)
    db.session.add(Team(name='New data', short_code='NEW', user_id=SOURCE)); db.session.commit()
    with pytest.raises(ValueError, match='changed'):
        merge.execute(actor(), mid, token, SURVIVOR)
    assert db.session.get(User, SOURCE)


@pytest.mark.parametrize('failure', ['backup_locked', 'audit'])
def test_failure_rolls_back(configured, failure):
    db.session.add(Team(name='Preserved', short_code='QA', user_id=SOURCE)); db.session.commit()
    mid, _ = ready()
    target = merge.base if failure == 'backup_locked' else merge
    with patch.object(target, failure, side_effect=RuntimeError('injected')):
        with pytest.raises(RuntimeError):
            apply(mid)
    db.session.expire_all()
    assert db.session.get(User, SOURCE)
    assert db.session.query(Team).one().user_id == SOURCE
    assert not db.session.execute(select(merge.signup.M)).first()


def test_private_history_votes_and_mute(configured):
    post = CommunityPost(public_id='qa-merge', author_id=SOURCE, flair='question', title='Private QA', body='test', visibility='private')
    db.session.add(post); db.session.flush()
    db.session.add_all([CommunityVote(post_id=post.id, user_id=SOURCE, value=-1), CommunityVote(post_id=post.id, user_id=SURVIVOR, value=1)])
    until = merge.base.now()+timedelta(days=3)
    db.session.get(User, SOURCE).community_muted_until = until
    db.session.commit()
    mid, _ = ready()
    result = apply(mid)
    assert len(result['losses']) == 1
    db.session.refresh(post)
    assert (post.author_id, post.visibility, post.vote_count, post.downvote_count, post.score) == (SURVIVOR, 'private', 1, 0, 1)
    assert db.session.get(User, SURVIVOR).community_muted_until == until
    assert result['provenance']['community_posts.author_id'] == [{'id': post.id}]


def test_source_recovery_requires_survivor_proof(configured):
    mid, _ = ready('Source mailbox unavailable; documented support recovery establishes ownership.')
    with merge.base.transaction() as conn:
        conn.execute(update(merge.M).where(merge.M.c.id == mid).values(survivor_verified=None))
    with pytest.raises(ValueError, match='proofs'):
        merge.preview(actor(), mid)


def test_tokens_single_use_and_cancel(configured):
    mid, tokens = ready()
    with pytest.raises(ValueError, match='already used'):
        merge.verify(mid, 'source', tokens['source'])
    merge.cancel(actor(), mid)
    with pytest.raises(ValueError, match='closed'):
        apply(mid)


def test_readonly_preview(configured):
    mid, _ = ready()
    with db.engine.connect() as conn:
        before = merge.base.digest('\n'.join(conn.connection.driver_connection.iterdump()))
    merge.preview(actor(), mid)
    with db.engine.connect() as conn:
        assert merge.base.digest('\n'.join(conn.connection.driver_connection.iterdump())) == before


def test_active_file_blocks(configured):
    path = Path(configured.config['IDENTITY_EMAIL_MATCH_DIR'])/'active.json'
    path.write_text(json.dumps({'created_by': SOURCE, 'current_state': 'in_progress'}))
    mid, _ = ready()
    with pytest.raises(ValueError, match='Finish'):
        apply(mid)


def test_unknown_reference_blocks(configured):
    mid, _ = ready()
    with db.engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE unknown_reference (id INTEGER PRIMARY KEY, owner TEXT REFERENCES users(stable_id))')
    with pytest.raises(ValueError, match='coverage'):
        apply(mid)


def test_admin_routes_and_csrf(configured, admin_client):
    mid, _ = ready()
    with configured.app_context():
        assert admin_client.get('/admin/identity/merges').status_code == 200
        response = admin_client.post('/admin/identity/merges/'+mid, data={'action':'preview'})
        assert response.status_code == 200
        assert b'private Community history' in response.data
        response = admin_client.post('/admin/identity/merges/'+mid, data={'action':'apply'})
        assert response.status_code == 409
    configured.config['WTF_CSRF_ENABLED'] = True
    with configured.app_context():
        assert admin_client.post('/admin/identity/merges/'+mid, data={'action':'cancel'}).status_code == 400


def test_nonadmin_denied(configured):
    with configured.app_context():
        client = logged_in_client(configured, 'user1')
        assert client.get('/admin/identity/merges').status_code in (302, 403)


def test_expired_request(configured):
    mid, _ = ready()
    with merge.base.transaction() as conn:
        conn.execute(update(merge.M).where(merge.M.c.id == mid).values(expires_at=merge.base.now()-timedelta(seconds=1)))
    with pytest.raises(ValueError, match='expired'):
        apply(mid)


def test_same_canonical_merge_does_not_mark_survivor(configured):
    from services.identity_email_change import normalize_email
    addresses = ('q.a.merge@gmail.com', 'qamerge@googlemail.com')
    with db.engine.begin() as conn:
        for old, new in zip((SOURCE, SURVIVOR), addresses):
            conn.execute(update(User).where(User.id == old).values(id=new, canonical_email=normalize_email(new).canonical))
    tokens = {}
    with patch.object(merge, 'deliver', side_effect=lambda mid, t: tokens.update(t)):
        mid = merge.create(actor(), db.session.query(IdentityReviewCase).first().id, *addresses,
                           'Verified common ownership of provider aliases.')
    for side in tokens:
        merge.verify(mid, side, tokens[side])
    _, token = merge.preview(actor(), mid)
    merge.execute(actor(), mid, token, addresses[1])
    assert not db.session.execute(select(merge.signup.M)).first()
    assert db.session.get(User, addresses[1])
    assert db.session.get(merge.base.IdentityEmailReservation, 'qamerge@gmail.com')


def test_archive_and_snapshot_access(configured):
    from database.models import Match
    from utils.match_ownership import owns_archive, owns_match_snapshot
    data = {'match_id':'merge-match', 'created_by':SOURCE, 'current_state':'completed'}
    db.session.add(Match(id='merge-match', user_id=SOURCE)); db.session.commit()
    path = Path(configured.config['IDENTITY_EMAIL_MATCH_DIR'])/'match.json'
    path.write_text(json.dumps(data))
    archive = Path(configured.config['ARCHIVES_FOLDER'])/('match_'+SOURCE+'_qa.zip')
    archive.write_bytes(b'unchanged archive bytes')
    before = path.read_bytes()
    mid, _ = ready(); apply(mid)
    assert path.read_bytes() == before
    assert owns_archive(archive, SURVIVOR) and not owns_archive(archive, SOURCE)
    assert owns_match_snapshot(data, SURVIVOR) and not owns_match_snapshot(data, SOURCE)


def test_security_sessions_and_pending_changes_revoked(configured):
    from services import identity_email_change as change
    with configured.app_context():
        left, right = logged_in_client(configured, 'user1'), logged_in_client(configured, 'user2')
    with patch.object(change, 'deliver_proofs'):
        cid = change.create(actor(), SOURCE, 'qa-unused@example.com', 'Verified member request for email change.')
    mid, _ = ready(); apply(mid)
    with db.engine.connect() as conn:
        row = change.get_change(conn, cid, pending=False)
        assert row['status']=='cancelled' and row['old_token_hash'] is None and row['new_token_hash'] is None
        assert not conn.execute(select(ActiveSession).where(ActiveSession.user_id.in_([SOURCE,SURVIVOR]))).first()
    for client in (left, right):
        with configured.app_context():
            assert client.get('/admin/identity/merges').status_code in (302,403)


def test_device_evidence_and_source_exemption(configured):
    from database.models import IdentityDeviceSighting, IdentityNetworkAppeal
    at = merge.base.now()
    ids = [db.session.get(User, a).stable_id for a in (SOURCE, SURVIVOR)]
    for i, stable in enumerate(ids):
        db.session.add(IdentityDeviceSighting(user_stable_id=stable, key_id='qa', device_hash='d', fingerprint_hash='f',
            fingerprint_status='ok', cookie_confirmed=bool(i), first_seen=at-timedelta(days=i+1), last_seen=at))
    db.session.add(IdentityNetworkAppeal(user_stable_id=ids[0], open_key=ids[0], status='approved', message='QA exemption',
        categories='[]', created_at=at, expires_at=at+timedelta(days=1)))
    db.session.commit()
    mid, _=ready(); apply(mid)
    sighting=db.session.query(IdentityDeviceSighting).one()
    assert sighting.user_stable_id==ids[1] and sighting.cookie_confirmed and sighting.first_seen==at-timedelta(days=2)
    appeal=db.session.query(IdentityNetworkAppeal).one()
    assert appeal.status=='revoked' and appeal.open_key is None and appeal.user_stable_id==ids[1]


def test_settings_conflicts_and_nonconflicts(configured):
    from database.models import UserGroundConfig, UserBannerDismissal, League, Tour
    for address in (SOURCE,SURVIVOR):
        db.session.add(UserGroundConfig(user_id=address,match_format='T20',config_json=json.dumps({'owner':address})))
        db.session.add(League(user_id=address,name='QA league'))
        db.session.add(Tour(user_id=address,name='QA tour',creation_token='shared'))
    db.session.add(UserGroundConfig(user_id=SOURCE,match_format='FC',config_json='{}'))
    db.session.commit()
    mid,_=ready(); result=apply(mid)
    configs=db.session.query(UserGroundConfig).all()
    assert len(configs)==2 and all(c.user_id==SURVIVOR for c in configs)
    assert json.loads(next(c for c in configs if c.match_format=='T20').config_json)['owner']==SURVIVOR
    assert len({x.name for x in db.session.query(League)})==2
    assert len({x.creation_token for x in db.session.query(Tour)})==2
    assert len(result['losses'])==1


def test_new_uniqueness_rule_fails_closed(configured):
    mid,_=ready()
    with db.engine.begin() as conn:
        conn.exec_driver_sql('CREATE UNIQUE INDEX qa_new_owner_constraint ON teams(user_id, name)')
    with pytest.raises(ValueError,match='uniqueness'):
        apply(mid)


def test_confirmation_actor_email_expiry(configured):
    mid,_=ready(); _,token=merge.preview(actor(),mid)
    with pytest.raises(ValueError,match='actor'):
        merge.execute(db.session.get(User,SURVIVOR).stable_id,mid,token,SURVIVOR)
    with pytest.raises(ValueError,match='exact'):
        merge.execute(actor(),mid,token,SOURCE)
    with patch('itsdangerous.timed.time.time',return_value=__import__('time').time()+901):
        with pytest.raises(ValueError,match='expired'):
            merge.execute(actor(),mid,token,SURVIVOR)


def test_checkpoint_after_prior_email_change_blocks(configured):
    from database.models import Match
    db.session.add(Match(id='prior-change',user_id=SOURCE)); db.session.commit()
    path=Path(configured.config['IDENTITY_EMAIL_MATCH_DIR'])/'prior.json.tmp'
    path.write_text(json.dumps({'match_id':'prior-change','created_by':'previous@example.com','current_state':'in_progress'}))
    mid,_=ready()
    with pytest.raises(ValueError,match='checkpoint'):
        apply(mid)


def test_enforcement_disabled_blocks(configured):
    mid,_=ready()
    configured.config['IDENTITY_SIGNUP_ENFORCE']=False
    with pytest.raises(ValueError,match='enforcement'):
        apply(mid)


def test_simultaneous_confirmations(configured):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    mid,_=ready(); admin=actor(); _,token=merge.preview(admin,mid)
    barrier=threading.Barrier(2)
    def run():
        with configured.app_context():
            barrier.wait(timeout=10)
            try:
                return merge.execute(admin,mid,token,SURVIVOR)['fingerprint']
            except ValueError as exc:
                assert 'activity' in str(exc)
                return 'busy'
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers=list(pool.map(lambda _: run(),range(2)))
    assert any(a!='busy' for a in answers)
    with db.engine.connect() as conn:
        assert conn.execute(select(merge.M.c.status).where(merge.M.c.id==mid)).scalar_one()=='completed'
    assert len(list(Path(configured.config['IDENTITY_EMAIL_BACKUP_DIR']).glob('*.sqlite3')))==1


def test_mail_delivery_failure_visible_and_get_does_not_verify(configured):
    with patch('utils.email_service.send_email',return_value=False):
        mid=merge.create(actor(),db.session.query(IdentityReviewCase).first().id,SOURCE,SURVIVOR,
                         'Common ownership established by reviewed support evidence.')
    with db.engine.connect() as conn:
        row=merge.get(conn,mid)
        assert row['delivery']=='failed' and row['source_verified'] is None
    response=configured.test_client().get('/identity/merge/verify/'+mid+'/source/invalid-token')
    assert response.status_code==200
    with db.engine.connect() as conn:
        assert merge.get(conn,mid)['source_verified'] is None


def test_render_workflow_pages(configured, admin_client):
    import os
    db.session.add_all([Team(name='QA same-name team', short_code='QA', user_id=a) for a in (SOURCE, SURVIVOR)])
    db.session.commit()
    mid,_=ready()
    pages=[]
    with configured.app_context():
        pages.append(admin_client.get('/admin/identity/merges'))
        pages.append(admin_client.get('/admin/identity/merges/'+mid))
        pages.append(admin_client.post('/admin/identity/merges/'+mid,data={'action':'preview'}))
    pages.append(configured.test_client().get('/identity/merge/verify/'+mid+'/source/preview-only'))
    apply(mid)
    with configured.app_context():
        pages.append(admin_client.get('/admin/identity/merges/'+mid))
    assert all(p.status_code==200 for p in pages)
    if os.environ.get('MERGE_RENDER_DIR'):
        root=Path(os.environ['MERGE_RENDER_DIR']); root.mkdir(parents=True,exist_ok=True)
        for i,response in enumerate(pages):
            (root/f'page{i}.html').write_bytes(response.data)


def test_player_custom_rows_and_adoption_conflicts(configured):
    from database.models import MasterPlayer, UserPlayer, Stadium, StadiumAdoption, UserBannerDismissal
    master=MasterPlayer(name='QA merge pool player');db.session.add(master);db.session.flush()
    db.session.add(Stadium(id='merge-public',slug='merge-public',kind='public'))
    for i,address in enumerate((SOURCE,SURVIVOR)):
        db.session.add(UserPlayer(user_id=address,master_player_id=master.id,name='Pool override',batting_rating=60+i))
        db.session.add(UserPlayer(user_id=address,name='Custom distinct player',master_player_id=None))
        db.session.add(Stadium(id=f'merge-private-{i}',slug=f'merge-private-{i}',owner_user_id=address))
        db.session.add(UserBannerDismissal(user_id=address,banner_version=10))
    db.session.flush()
    for i,address in enumerate((SOURCE,SURVIVOR)):
        db.session.add(StadiumAdoption(user_id=address,source_stadium_id='merge-public',source_revision=1,stadium_id=f'merge-private-{i}'))
    db.session.commit()
    mid,_=ready();result=apply(mid)
    assert len(result['losses'])==3
    assert db.session.query(UserPlayer).filter_by(user_id=SURVIVOR,master_player_id=None).count()==2
    assert db.session.query(UserPlayer).filter_by(master_player_id=master.id).one().batting_rating==61
    assert db.session.query(Stadium).filter_by(owner_user_id=SURVIVOR).count()==2
    assert db.session.query(StadiumAdoption).one().stadium_id=='merge-private-1'
    assert db.session.query(UserBannerDismissal).one().user_id==SURVIVOR


def test_active_auction_blocks(configured):
    from database.models import League, Season, Auction
    league=League(user_id=SOURCE,name='QA live auction');db.session.add(league);db.session.flush()
    season=Season(league_id=league.id,name='QA season',status='auction_live');db.session.add(season);db.session.flush()
    db.session.add(Auction(season_id=season.id,started_at=merge.base.now()));db.session.commit()
    mid,_=ready()
    with pytest.raises(ValueError,match='auctions'):
        apply(mid)


def test_existing_campaign_blocks(configured):
    from services import identity_campaign as campaign
    # Campaign restriction survives even if the legacy force flag was mistakenly cleared.
    with patch('services.identity_campaign.pending', return_value=True):
        with pytest.raises(ValueError,match='correction'):
            ready()


@pytest.mark.parametrize('kind',['changed','symlink'])
def test_unsafe_owned_archive_blocks(configured,kind):
    from database.models import IdentityArchiveOwner
    p=Path(configured.config['ARCHIVES_FOLDER'])/('match_'+SOURCE+'_qa.zip')
    if kind=='changed':
        p.write_bytes(b'changed archive')
        db.session.add(IdentityArchiveOwner(filename=p.name,user_stable_id=db.session.get(User,SOURCE).stable_id,content_hash='0'*64))
        db.session.commit()
    else:
        target=p.parent/'unowned.data';target.write_bytes(b'other bytes');p.symlink_to(target)
    mid,_=ready()
    with pytest.raises(ValueError,match='[Aa]rchive'):
        apply(mid)
