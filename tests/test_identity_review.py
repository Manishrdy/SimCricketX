"""Identity admin review uses shared QA accounts in disposable databases."""
from datetime import timedelta
import re
from unittest.mock import patch

import pytest
from sqlalchemy import select, update

from database import db
from database.models import User, IdentityDeviceSighting, IdentityReviewCase as Case, IdentityReviewEvidence as Evidence, IdentityReviewAction as Action
from services import identity_review as review
from migrations.add_identity_review import run_migration
from scripts.dev_test_accounts import ACCOUNTS


@pytest.fixture
def candidates(admin_client, authenticated_client):
    ids = [db.session.get(User, ACCOUNTS[k]['email']).stable_id for k in ('admin', 'user1')]
    at = review.now()
    for uid in ids:
        db.session.add(IdentityDeviceSighting(user_stable_id=uid, key_id='test', device_hash='DO_NOT_EXPORT_DEVICE',
            fingerprint_hash='DO_NOT_EXPORT_FP', fingerprint_status='ok', cookie_confirmed=True,
            first_seen=at-timedelta(days=1), last_seen=at))
    db.session.commit()
    return ids


def preview(client):
    response = client.post('/admin/identity/scan/preview')
    assert response.status_code == 200
    return re.search(rb'name="token" value="([^"]+)"', response.data)[1].decode()


def imported(client):
    token = preview(client)
    response = client.post('/admin/identity/scan/import', data={'token': token, 'confirmed': 'yes'})
    assert response.status_code == 303
    db.session.expire_all()
    return db.session.execute(select(Case)).scalar_one()


def act(client, case, action, **values):
    return client.post(f'/admin/identity/duplicates/{case.id}', data={'version': case.version, 'action': action,
                       'reason': 'QA review decision', **values})


@pytest.mark.parametrize('path', ['', '/duplicates', '/policy', '/audit', '/duplicates/1', '/accounts/missing'])
def test_private_routes(client, authenticated_client, path):
    assert client.get('/admin/identity'+path).status_code == 302
    assert authenticated_client.get('/admin/identity'+path).status_code == 403


def test_scan_preview_has_no_writes_and_import_is_explicit(admin_client, candidates):
    token = preview(admin_client)
    assert db.session.query(Case).count() == 0
    assert db.session.query(Action).count() == 0
    assert admin_client.get('/admin/identity/scan/import').status_code == 405
    assert admin_client.post('/admin/identity/scan/import', data={'token': token}).status_code == 400
    case = imported(admin_client)
    assert db.session.query(Evidence).count() == 2
    assert db.session.query(Action).count() == 1
    content = admin_client.get(f'/admin/identity/duplicates/{case.id}')
    assert content.status_code == 200
    assert b'DO_NOT_EXPORT' not in content.data
    assert content.headers['Cache-Control'] == 'no-store, private'


def test_repeated_import_and_closed_case_preserved(admin_client, candidates):
    case = imported(admin_client)
    token = preview(admin_client)
    assert admin_client.post('/admin/identity/scan/import', data={'token': token, 'confirmed': 'yes'}).status_code == 303
    assert admin_client.post('/admin/identity/scan/import', data={'token': token, 'confirmed': 'yes'}).status_code == 409
    assert db.session.query(Case).count() == 1
    db.session.refresh(case)
    assert act(admin_client, case, 'dismissed').status_code == 303
    db.session.refresh(case)
    version = case.version
    imported(admin_client)
    db.session.refresh(case)
    assert case.status == 'dismissed' and case.version == version


def test_transitions_reopen_stale_edits_and_no_access_change(admin_client, candidates):
    case = imported(admin_client)
    assert act(admin_client, case, 'resolved').status_code == 409
    old_version = case.version
    assert act(admin_client, case, 'investigating').status_code == 303
    assert act(admin_client, case, 'note', version=old_version).status_code == 409
    db.session.refresh(case)
    assert act(admin_client, case, 'resolved').status_code == 303
    db.session.refresh(case)
    assert case.closed_at is not None
    assert act(admin_client, case, 'open').status_code == 303
    db.session.refresh(case)
    assert case.closed_at is None and case.status == 'open'
    assert all(not u.is_banned for u in db.session.execute(select(User).where(User.stable_id.in_(candidates))).scalars())


def test_reason_assignment_notes_escape(admin_client, candidates):
    case = imported(admin_client)
    assert act(admin_client, case, 'note', reason=' ').status_code == 409
    assert act(admin_client, case, 'note', reason='x'*2001).status_code == 409
    assert act(admin_client, case, 'assign', assignee=candidates[1]).status_code == 409
    assert act(admin_client, case, 'assign', assignee=candidates[0]).status_code == 303
    db.session.refresh(case)
    assert case.assignee == candidates[0]
    assert act(admin_client, case, 'note', reason='<script>QA</script>').status_code == 303
    page = admin_client.get(f'/admin/identity/duplicates/{case.id}')
    assert b'&lt;script&gt;QA&lt;/script&gt;' in page.data
    assert b'<script>QA</script>' not in page.data


def test_csrf_all_mutations(app, admin_client, candidates):
    app.config['WTF_CSRF_ENABLED'] = True
    for path in ['/scan/preview', '/scan/import', '/duplicates/1']:
        assert admin_client.post('/admin/identity'+path).status_code == 400
    response = admin_client.get('/admin/identity')
    csrf = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)[1].decode()
    assert admin_client.post('/admin/identity/scan/preview', data={'csrf_token': csrf}).status_code == 200


def test_tampered_expired_preview_and_changed_evidence(admin_client, candidates):
    token = preview(admin_client)
    assert admin_client.post('/admin/identity/scan/import', data={'token': token+'x', 'confirmed': 'yes'}).status_code == 409
    from routes.admin_identity_routes import serializer
    contents = serializer().loads(token)
    with patch('itsdangerous.TimestampSigner.get_timestamp', return_value=int(__import__('time').time())-901):
        expired = serializer().dumps(contents)
    assert admin_client.post('/admin/identity/scan/import', data={'token': expired, 'confirmed': 'yes'}).status_code == 409
    db.session.execute(update(IdentityDeviceSighting).values(cookie_confirmed=False))
    db.session.commit()
    assert admin_client.post('/admin/identity/scan/import', data={'token': token, 'confirmed': 'yes'}).status_code == 409
    assert db.session.query(Case).count() == 0


def test_transaction_rolls_back_with_failed_audit(admin_client, candidates):
    token = preview(admin_client)
    with patch.object(review, 'audit', side_effect=RuntimeError('injected')):
        with pytest.raises(RuntimeError):
            review.import_scan(candidates[0], __import__('routes.admin_identity_routes', fromlist=['serializer']).serializer().loads(token)['digest'])
    assert db.session.query(Case).count() == 0
    assert db.session.query(Evidence).count() == 0


def test_expiry_hidden_and_cleanup(runner, admin_client, candidates):
    case = imported(admin_client)
    at = review.now()
    db.session.execute(update(Evidence).values(expires_at=at))
    db.session.commit()
    assert b'No unexpired evidence' in admin_client.get(f'/admin/identity/duplicates/{case.id}').data
    assert act(admin_client, case, 'dismissed').status_code == 303
    db.session.execute(update(Case).values(closed_at=at-timedelta(days=180)))
    db.session.commit()
    assert admin_client.get(f'/admin/identity/duplicates/{case.id}').status_code == 404
    assert b'QA review decision' not in admin_client.get('/admin/identity/audit').data
    result = runner.invoke(args=['purge-identity-reviews'])
    assert result.exit_code == 0, result.output
    db.session.expire_all()
    assert db.session.query(Case).count() == db.session.query(Evidence).count() == db.session.query(Action).count() == 0


def test_device_expiry_not_extended_on_import(admin_client, candidates):
    at = review.now()
    db.session.execute(update(IdentityDeviceSighting).values(first_seen=at-timedelta(days=180), last_seen=at-timedelta(days=179)))
    db.session.commit()
    imported(admin_client)
    assert all(e.expires_at < at+timedelta(days=2) for e in db.session.query(Evidence))


def test_policy_navigation_filters_account_and_impersonation(admin_client, candidates):
    case = imported(admin_client)
    for path in ['', '/duplicates?status=open&reason=fingerprint&q=qa', '/audit?action=scan_import', '/policy', '/accounts/'+candidates[1]]:
        response = admin_client.get('/admin/identity'+path)
        assert response.status_code == 200
        assert b'DO_NOT_EXPORT' not in response.data
    assert admin_client.get('/admin/identity/duplicates?status=invented').status_code == 400
    assert admin_client.get('/admin/identity/duplicates?page=bad').status_code == 400
    assert admin_client.get('/admin/identity/duplicates?reason=canonical_email').data.count(b'Review #') == 0
    with admin_client.session_transaction() as session:
        session['impersonating_from'] = ACCOUNTS['admin']['email']
    assert admin_client.get('/admin/identity').status_code == 403


def test_migration_rerun(app):
    run_migration(db, app)
    run_migration(db, app)
    from sqlalchemy import inspect
    assert {'identity_review_cases', 'identity_review_evidence', 'identity_review_actions'} <= set(inspect(db.engine).get_table_names())


def test_open_case_retains_only_workflow_after_expiry(runner, admin_client, candidates):
    case = imported(admin_client)
    db.session.execute(update(Evidence).values(expires_at=review.now()-timedelta(seconds=1)))
    db.session.execute(update(Action).values(expires_at=review.now()-timedelta(seconds=1)))
    db.session.commit()
    assert runner.invoke(args=['purge-identity-reviews']).exit_code == 0
    assert db.session.query(Case).count() == 1
    assert db.session.query(Evidence).count() == db.session.query(Action).count() == 0
    assert admin_client.get(f'/admin/identity/duplicates/{case.id}').status_code == 200


def test_pagination_and_search(admin_client, candidates):
    at = review.now()
    for index in range(35):
        db.session.add(Case(left_id=candidates[0], right_id=f'gone-{index}', status='open', version=1, created_at=at, updated_at=at))
    db.session.commit()
    first = admin_client.get('/admin/identity/duplicates')
    second = admin_client.get('/admin/identity/duplicates?page=2')
    assert first.data.count(b'<strong>Review #') == 30
    assert second.data.count(b'<strong>Review #') == 5
    assert b'No reviews here' in admin_client.get('/admin/identity/duplicates?q=not-an-account').data


def test_mutation_rolls_back_when_audit_fails(admin_client, candidates):
    case = imported(admin_client)
    version = case.version
    with patch.object(review, 'audit', side_effect=RuntimeError('injected')):
        with pytest.raises(RuntimeError):
            review.change(case.id, candidates[0], version, 'investigating', 'QA')
    db.session.refresh(case)
    assert case.version == version and case.status == 'open'


def test_preview_bound_to_admin(admin_client, authenticated_client, candidates):
    token = preview(admin_client)
    # Temporarily grant another shared QA account admin within this isolated DB.
    db.session.execute(update(User).where(User.stable_id == candidates[1]).values(is_admin=True))
    db.session.commit()
    assert authenticated_client.post('/admin/identity/scan/import', data={'token': token, 'confirmed': 'yes'}).status_code == 403
    assert db.session.query(Case).count() == 0


def test_two_processes_cannot_save_same_case_version(admin_client, candidates):
    import subprocess
    import sys
    case = imported(admin_client)
    script = '''
import sys
from flask import Flask
from database import db
from services import identity_review as review
app = Flask('identity-race')
app.config['SQLALCHEMY_DATABASE_URI'] = sys.argv[1]
db.init_app(app)
with app.app_context():
    try:
        review.change(int(sys.argv[2]), sys.argv[3], 1, 'investigating', 'Concurrent QA review')
        print('saved')
    except ValueError:
        print('stale')
'''
    args = [sys.executable, '-c', script, str(db.engine.url), str(case.id), candidates[0]]
    processes = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    output = [p.communicate(timeout=30) for p in processes]
    assert all(p.returncode == 0 for p in processes), output
    assert sorted(stdout.strip() for stdout, _ in output) == ['saved', 'stale']
    db.session.refresh(case)
    assert case.version == 2


def test_migration_creates_missing_tables_without_changing_users(app, admin_client):
    before = [(u.id, u.stable_id) for u in db.session.query(User)]
    for model in (Action, Evidence, Case):
        model.__table__.drop(db.engine)
    run_migration(db, app)
    run_migration(db, app)
    assert [(u.id, u.stable_id) for u in db.session.query(User)] == before
    from migrations.verify import model_drift
    assert not model_drift(db.engine, db.metadata)
