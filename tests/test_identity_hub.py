"""Read-only hub projections: shared QA users, isolated DB, no outbound operations."""
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select, update, delete
from database import db
from database import models as m
from scripts.dev_test_accounts import ACCOUNTS, logged_in_client
from services import identity_hub as hub

AT = datetime(2026, 11, 9, 16, 0)


@pytest.fixture
def data(app, admin_client, monkeypatch):
    monkeypatch.setattr(hub, 'now', lambda: AT)
    admin, user, other = [db.session.get(m.User, ACCOUNTS[k]['email']) for k in ('admin','user1','user2')]
    case = m.IdentityReviewCase(left_id=user.stable_id, right_id=other.stable_id, status='investigating', created_at=AT, updated_at=AT)
    db.session.add(case);db.session.flush()
    db.session.add(m.IdentityReviewAction(case_id=case.id,actor_id=admin.stable_id,action='note',reason='QA reviewed evidence',created_at=AT-timedelta(hours=6),expires_at=AT+timedelta(days=1)))
    signup=m.IdentitySignupAppeal(email=user.id,identity_hash='a'*64,status='approved',message='QA support',created_at=AT-timedelta(hours=5),verified_at=AT-timedelta(hours=5),expires_at=AT+timedelta(days=7),scopes='["deleted_return"]')
    network=m.IdentityNetworkAppeal(user_stable_id=user.stable_id,status='approved',message='QA network',categories='[]',created_at=AT-timedelta(hours=4),expires_at=AT+timedelta(days=7))
    db.session.add_all([signup,network]);db.session.flush()
    db.session.add(m.IdentitySignupAction(appeal_id=signup.id,actor_id=admin.stable_id,action='approve',reason='QA scoped return approved',created_at=AT-timedelta(hours=5),expires_at=AT+timedelta(days=180)))
    db.session.add(m.IdentityNetworkAction(appeal_id=network.id,actor_id=admin.stable_id,action='approve',reason='QA network approved',created_at=AT-timedelta(hours=4),expires_at=AT+timedelta(days=180)))
    db.session.add(m.IdentityEmailChange(id='hub-email',user_stable_id=user.stable_id,old_email=user.id,new_email='qa.new@example.com',actor_id=admin.stable_id,reason='QA email ownership reviewed',status='completed',created_at=AT-timedelta(hours=3),expires_at=AT+timedelta(days=1),completed_at=AT-timedelta(hours=2),old_token_hash='SECRET_PROOF_HASH',receipt_json=json.dumps({'actor':admin.stable_id,'backup':'email-safe-backup.sqlite3','password':'SECRET_RECEIPT_FIELD'})))
    db.session.add(m.IdentityEmailCampaign(id='hub-campaign',started_at=AT-timedelta(days=2),deadline=AT+timedelta(days=28),created_by=admin.stable_id))
    db.session.flush()
    member=m.IdentityCampaignMember(id='hub-member',campaign_id='hub-campaign',subject=user.stable_id,address=user.id,reason='plus_tag',status='pending',hold_until=AT+timedelta(days=1),notices='{"initial":"failed"}')
    db.session.add(member);db.session.flush()
    db.session.add(m.IdentityCampaignAction(member_id=member.id,campaign_id='hub-campaign',actor=admin.stable_id,action='hold',reason='QA support hold',created_at=AT-timedelta(hours=1)))
    db.session.add(m.IdentityMerge(id='hub-merge',case_key=case.id,source=user.stable_id,survivor=other.stable_id,source_address=user.id,survivor_address=other.id,administrator=admin.stable_id,reason='QA ownership verified',status='pending',created_at=AT,expires_at=AT+timedelta(days=1),source_proof='SECRET_MERGE_HASH',delivery='failed'))
    db.session.add(m.IdentityDeviceSighting(user_stable_id=user.stable_id,key_id='key',device_hash='SECRET_DEVICE_HASH',fingerprint_hash='SECRET_FINGERPRINT_HASH',fingerprint_status='ok',cookie_confirmed=True,first_seen=AT-timedelta(days=2),last_seen=AT))
    db.session.add(m.IdentityNetworkEvent(user_stable_id=user.stable_id,state='restricted',categories='[]',created_at=AT,expires_at=AT+timedelta(days=1)))
    db.session.commit()
    return {'app':app,'admin':admin_client,'user':user.stable_id,'other':other.stable_id,'actor':admin.stable_id,'case':case.id,'signup':signup.id,'network':network.id}


def request(data,path):
    with data['app'].app_context():
        return data['admin'].get('/admin/identity'+path)


@pytest.mark.parametrize('path',['','/audit','/approvals','/exemptions','/policy'])
def test_hub_pages_private_and_safe(data,path):
    response=request(data,path)
    assert response.status_code==200
    assert 'no-store' in response.headers['Cache-Control']
    assert response.headers['Referrer-Policy']=='no-referrer'
    assert b'SECRET_' not in response.data
    assert b'Identity sections' in response.data


def test_activity_union_order_actor_and_filter(data):
    rows,more=hub.audit_rows(hub.filters({}))
    assert not more
    assert {r['workflow'] for r in rows}==set(hub.WORKFLOWS)
    assert [r['occurred'] for r in rows]==sorted([r['occurred'] for r in rows],reverse=True)
    completed=next(r for r in rows if r['workflow']=='email' and r['action']=='completed')
    assert completed['actor_label']==ACCOUNTS['admin']['email']
    assert completed['backup']=='email-safe-backup.sqlite3'
    filtered,_=hub.audit_rows(hub.filters({'workflow':'network','action':'approve','q':ACCOUNTS['user1']['email'],'from':'2026-11-09','to':'2026-11-09'}))
    assert len(filtered)==1
    assert not hub.audit_rows(hub.filters({'q':'does-not-exist'}))[0]
    assert not hub.audit_rows(hub.filters({'from':'2026-11-10'}))[0]


def test_account_includes_signup_and_restrictions_without_hashes(data):
    account_path = '/admin/identity/accounts/' + data['user']
    response=request(data,'/accounts/'+data['user'])
    assert response.status_code==200
    assert b'Identity sections' not in response.data
    assert b'Account actions' in response.data
    assert b'Back to previous page' in response.data
    assert b'User settings, password &amp; ban' in response.data
    assert b'Email changes' in response.data
    assert b'Duplicate reviews' in response.data
    assert b'Account merges' in response.data
    assert b'Approvals &amp; scoped exemptions' in response.data
    assert b'Email campaign &amp; holds' in response.data
    assert f'back={account_path}'.encode() in response.data or b'back=%2Fadmin%2Fidentity%2Faccounts%2F' in response.data
    assert b'QA scoped return approved' in response.data
    assert b'SECRET_' not in response.data
    assert b'hold until' in response.data
    assert b'not a live access decision' in response.data
    # Visiting an action from the account page renders a Back to previous page link and suppresses the full workflow bar
    for dest in (
        f"/admin/users/{ACCOUNTS['user1']['email']}?tab=actions&back={account_path}",
        f"/admin/identity/email-changes?email={ACCOUNTS['user1']['email']}&back={account_path}",
        f"/admin/identity/duplicates?q={ACCOUNTS['user1']['email']}&back={account_path}",
        f"/admin/identity/merges?source={ACCOUNTS['user1']['email']}&back={account_path}",
        f"/admin/identity/exemptions?q={data['user']}&status=&back={account_path}",
    ):
        with data['app'].app_context():
            sub = data['admin'].get(dest)
        assert sub.status_code == 200
        assert b'Back to previous page' in sub.data
        assert account_path.encode() in sub.data
        assert b'Identity sections' not in sub.data


def test_expiry_without_cleanup_and_retention(data):
    db.session.execute(update(m.IdentitySignupAppeal).values(expires_at=AT))
    db.session.execute(update(m.IdentityNetworkAppeal).values(expires_at=AT))
    db.session.commit()
    active,_=hub.grants(hub.filters({'status':'active'},grants=True),exemptions=True)
    expired,_=hub.grants(hub.filters({'status':'expired'},grants=True),exemptions=True)
    assert not active and len(expired)==2
    db.session.execute(update(m.IdentitySignupAppeal).values(expires_at=AT-timedelta(days=180)))
    db.session.execute(update(m.IdentityNetworkAppeal).values(expires_at=AT-timedelta(days=180)))
    db.session.commit()
    assert not hub.grants(hub.filters({},grants=True))[0]
    assert not hub.audit_rows(hub.filters({'workflow':'signup'}))[0]
    assert not hub.audit_rows(hub.filters({'workflow':'network'}))[0]


def test_review_and_device_evidence_expiry(data):
    db.session.execute(update(m.IdentityReviewCase).values(status='resolved',closed_at=AT-timedelta(days=180)))
    db.session.execute(update(m.IdentityDeviceSighting).values(last_seen=AT-timedelta(days=180)))
    db.session.commit()
    assert not hub.audit_rows(hub.filters({'workflow':'review'}))[0]
    assert not hub.account(data['user'])['sightings']


def test_campaign_retention_waits_for_cleanup(data):
    db.session.execute(update(m.IdentityCampaignMember).values(status='deleted',closed_at=AT-timedelta(days=180)))
    db.session.commit()
    assert not hub.audit_rows(hub.filters({'workflow':'campaign'}))[0]
    db.session.add(m.IdentityCampaignFile(member_id='hub-member',area='matches',name='qa.json',content_hash='a'*64,error='QA pending'))
    db.session.commit()
    assert hub.audit_rows(hub.filters({'workflow':'campaign'}))[0]
    assert hub.overview()['failures']['files']==1


def test_retired_identity_lineage_and_missing_account(data):
    db.session.execute(update(m.IdentityMerge).values(status='completed',completed_at=AT,receipt_json='{}'))
    db.session.execute(delete(m.User).where(m.User.stable_id==data['user']))
    db.session.commit()
    response=request(data,'/accounts/'+data['user'])
    assert response.status_code==200
    assert b'Unavailable identity with merge history' in response.data
    assert data['other'].encode() in response.data
    assert request(data,'/accounts/missing').status_code==404


def test_readonly_queries_no_external_work(data):
    def digest():
        with db.engine.connect() as conn:
            return hashlib.sha256('\n'.join(conn.connection.driver_connection.iterdump()).encode()).hexdigest()
    before=digest()
    with data['app'].test_request_context(), patch('utils.email_service.send_email',side_effect=AssertionError('mail')), patch('utils.network_classifier._download',side_effect=AssertionError('network')):
        hub.overview();hub.audit_rows(hub.filters({}));hub.grants(hub.filters({},grants=True));hub.account(data['user']);hub.configuration()
    assert digest()==before


@pytest.mark.parametrize('query',['page=bad','page=0','workflow=unknown','from=not-a-date','from=2026-11-10&to=2026-11-09','to=9999-12-31'])
def test_invalid_filters(data,query):
    assert request(data,'/audit?'+query).status_code==400


def test_literal_search_and_safe_backup_reference(data):
    assert not hub.audit_rows(hub.filters({'q':'%'}))[0]
    db.session.execute(update(m.IdentityEmailChange).values(receipt_json=json.dumps({'actor':data['actor'],'backup':'/private/SECRET-backup.sqlite3'}),reason='<script>unsafe</script>'))
    db.session.commit()
    response=request(data,'/audit?workflow=email')
    assert b'&lt;script&gt;unsafe&lt;/script&gt;' in response.data
    assert b'<script>unsafe</script>' not in response.data
    assert b'/private/SECRET' not in response.data


def test_pagination_keeps_filters_and_tie_order(data):
    for i in range(35):
        db.session.add(m.IdentityReviewAction(case_id=data['case'],actor_id=data['actor'],action='note',reason=f'pagination {i}',created_at=AT,expires_at=AT+timedelta(days=1)))
    db.session.commit()
    f=hub.filters({'workflow':'review','action':'note','q':data['user'],'to':'2026-11-09'})
    first,more=hub.audit_rows(f);assert len(first)==30 and more
    second,more=hub.audit_rows({**f,'page':2});assert len(second)==6 and not more
    assert not {r['key'] for r in first}&{r['key'] for r in second}
    response=request(data,'/audit?workflow=review&action=note&q='+data['user']+'&to=2026-11-09')
    assert b'page=2' in response.data and b'workflow=review' in response.data and b'to=2026-11-09' in response.data


def test_permissions_and_no_new_mutations(data):
    for endpoint in ('/approvals','/exemptions','/policy','/audit'):
        with data['app'].app_context():
            client=logged_in_client(data['app'],'user2')
            assert client.get('/admin/identity'+endpoint).status_code==403
        assert data['admin'].post('/admin/identity'+endpoint).status_code in (400,405)
    with data['admin'].session_transaction() as sess:
        sess['impersonating_from']=ACCOUNTS['admin']['email']
    assert request(data,'/exemptions').status_code==403


def test_effective_states(data):
    for state,expected in [('approved','active'),('used','used'),('revoked','revoked'),('rejected','rejected'),('open','open')]:
        db.session.execute(update(m.IdentitySignupAppeal).values(status=state));db.session.commit()
        rows,_=hub.grants(hub.filters({'workflow':'signup','status':expected},grants=True))
        assert len(rows)==1 and rows[0]['status']==expected


def test_completed_actor_comes_from_receipt(data):
    db.session.execute(update(m.IdentityEmailChange).values(receipt_json=json.dumps({'actor':data['other']})))
    db.session.commit()
    rows,_=hub.audit_rows(hub.filters({'workflow':'email','action':'completed'}))
    assert rows[0]['actor_label']==ACCOUNTS['user2']['email']


def test_dashboard_truthful_counts(data):
    result=hub.overview()
    assert result['failures']['merge']==1 and result['failures']['notices']==1
    assert result['observations']=={'restricted':1}
    assert 'restricted users' not in ' '.join(c[0] for c in result['cards']).lower()
    assert result['config']['signup'] is False


def test_render_pages(data):
    import os
    pages=['','/audit','/approvals?status=','/exemptions','/policy','/accounts/'+data['user']]
    for i,path in enumerate(pages):
        response=request(data,path)
        assert response.status_code==200
        if os.environ.get('HUB_RENDER_DIR'):
            root=Path(os.environ['HUB_RENDER_DIR']);root.mkdir(parents=True,exist_ok=True)
            (root/f'page{i}.html').write_bytes(response.data)


def test_missing_expiry_never_claims_active(data):
    db.session.execute(update(m.IdentitySignupAppeal).values(expires_at=None))
    db.session.execute(update(m.IdentityNetworkAppeal).values(expires_at=None))
    db.session.commit()
    assert not hub.grants(hub.filters({'status':'active'},grants=True))[0]
    rows,_=hub.grants(hub.filters({'status':'unavailable'},grants=True))
    assert len(rows)==2


def test_campaign_backup_identifiers_and_pruned_detail(data):
    db.session.execute(update(m.IdentityCampaignAction).values(action='deleted'))
    db.session.execute(update(m.IdentityCampaignMember).values(status='deleted',closed_at=AT,receipt=json.dumps({
        'backup':'email-campaign-safe.sqlite3','artifact_backup':'email-campaign-safe.zip','secret':'SECRET_NOT_FOR_HUB'})))
    db.session.commit()
    response=request(data,'/audit?workflow=campaign')
    assert b'email-campaign-safe.sqlite3' in response.data and b'email-campaign-safe.zip' in response.data
    assert b'SECRET_NOT_FOR_HUB' not in response.data
    db.session.execute(delete(m.IdentityCampaignMember));db.session.commit()
    assert not hub.audit_rows(hub.filters({'workflow':'campaign'}))[0]


def test_grant_pagination_and_escaped_query(data):
    for i in range(32):
        db.session.add(m.IdentitySignupAppeal(email=f'qa-page-{i}@example.com',identity_hash=f'{i:064x}',status='open',
            message='QA pagination request',created_at=AT,verified_at=AT))
    db.session.commit()
    f=hub.filters({'workflow':'signup','status':'open','q':'qa-page'},grants=True)
    first,more=hub.grants(f);second,last=hub.grants({**f,'page':2})
    assert len(first)==30 and more and len(second)==2 and not last
    assert not {r['target'] for r in first}&{r['target'] for r in second}
    assert not hub.grants(hub.filters({'q':'%'},grants=True))[0]


def test_receipt_queries_are_batched(data):
    from sqlalchemy import event
    for i in range(10):
        db.session.add(m.IdentityEmailChange(id=f'batch-{i}',user_stable_id=data['user'],old_email=ACCOUNTS['user1']['email'],
            new_email=f'qa-batch-{i}@example.com',actor_id=data['actor'],reason='QA completed change',status='completed',
            created_at=AT-timedelta(hours=1),expires_at=AT+timedelta(days=1),completed_at=AT,
            receipt_json=json.dumps({'actor':data['actor']})))
    db.session.commit()
    queries=[]
    def record(conn,cursor,statement,parameters,context,executemany):
        queries.append(statement)
    event.listen(db.engine,'before_cursor_execute',record)
    try:
        rows,_=hub.audit_rows(hub.filters({'workflow':'email','action':'completed'}))
    finally:
        event.remove(db.engine,'before_cursor_execute',record)
    assert len(rows)==11 and len(queries)<=3


def test_expired_actions_hidden_even_for_open_cases(data):
    db.session.execute(update(m.IdentityReviewAction).values(expires_at=AT))
    db.session.execute(update(m.IdentitySignupAction).values(expires_at=AT))
    db.session.execute(update(m.IdentityNetworkAction).values(expires_at=AT))
    db.session.commit()
    for workflow in ('review','signup','network'):
        assert not hub.audit_rows(hub.filters({'workflow':workflow}))[0]


def test_future_or_inverted_device_observations_hidden(data):
    db.session.execute(update(m.IdentityDeviceSighting).values(last_seen=AT+timedelta(seconds=1)))
    db.session.commit()
    assert not hub.account(data['user'])['sightings']
    db.session.execute(update(m.IdentityDeviceSighting).values(last_seen=AT,first_seen=AT+timedelta(seconds=1)))
    db.session.commit()
    assert not hub.account(data['user'])['sightings']
