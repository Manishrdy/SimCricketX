"""Removed admin tools stay unreachable; retained workflows remain usable."""

import pytest
from flask import g
from scripts.dev_test_accounts import ACCOUNTS, logged_in_client, seed


@pytest.fixture()
def qa_admin(app):
    seed(app)
    return logged_in_client(app, 'admin')


def test_removed_tools_and_unknown_routes_return_404(app, qa_admin):
    removed = {
        '/admin/files': ('GET',),
        '/admin/api/files': ('GET', 'DELETE'),
        '/admin/sql': ('GET', 'POST'),
        '/admin/backup-database': ('POST',),
        '/admin/restore-center': ('GET',),
        '/admin/restore/apply': ('POST',),
        '/admin/export': ('GET',),
        '/admin/export/users/csv': ('GET',),
        '/admin/export/all/json': ('GET',),
        '/admin/not-a-real-page': ('GET',),
    }
    assert '/admin/<path:subpath>' not in {r.rule for r in app.url_map.iter_rules()}
    for path, methods in removed.items():
        for method in methods:
            response = qa_admin.open(path, method=method)
            assert response.status_code == 404, (method, path, response.status_code)
            assert 'Location' not in response.headers
    response = qa_admin.get('/admin/not-a-real-page', headers={'Accept': 'application/json'})
    assert response.status_code == 404
    assert response.json == {'error': 'Not found'}


def test_retained_admin_pages_and_navigation(app, qa_admin):
    email = ACCOUNTS['user1']['email']
    paths = [
        '/admin/dashboard', '/admin/users', '/admin/users/create',
        '/admin/backups', '/admin/database/stats', '/admin/scheduled-tasks',
        f'/admin/users/{email}', f'/admin/users/{email}/360',
        f'/admin/users/{email}/analytics', f'/admin/users/{email}/login-history',
    ]
    for path in paths:
        response = qa_admin.get(path, follow_redirects=True)
        assert response.status_code == 200, (path, response.status_code)
        html = response.get_data(as_text=True)
        for removed in ['/admin/files', '/admin/sql', '/admin/restore', '/admin/export',
                        '/admin/backup-database', 'downloadDatabase', 'backupModal']:
            assert removed not in html, (path, removed)
        sidebar = html.split('<nav class="admin-nav"', 1)[-1].split('</nav>', 1)[0]
        assert 'href="/admin/users/create"' not in sidebar
    assert 'href="/admin/users/create"' in qa_admin.get('/admin/users').get_data(as_text=True)
    assert qa_admin.get(f'/admin/users/{email}/export').status_code == 200
    response = qa_admin.get('/admin')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/admin/dashboard')


def test_create_user_remains_admin_only(app, qa_admin):
    regular = logged_in_client(app, 'user1')
    assert regular.get('/admin/users/create').status_code == 403
    g.pop('_login_user', None)
    assert app.test_client().get('/admin/users/create').status_code == 302


def test_audit_schema_and_log_routes_are_removed(app, qa_admin):
    from sqlalchemy import inspect
    from database import db
    from auth.user_auth import log_admin_action
    assert 'admin_audit_log' not in inspect(db.engine).get_table_names()
    log_admin_action(ACCOUNTS['admin']['email'], 'test_operation')
    for path in ['/admin/audit-log', '/admin/logs', '/admin/logs/download']:
        assert qa_admin.get(path).status_code == 404
    for path in ['/admin/dashboard', '/admin/activity',
                 f"/admin/users/{ACCOUNTS['user1']['email']}/360"]:
        response = qa_admin.get(path, follow_redirects=True)
        assert response.status_code == 200
        assert '/admin/audit-log' not in response.get_data(as_text=True)
        assert '/admin/logs' not in response.get_data(as_text=True)
    response = qa_admin.get('/admin/dashboard/stream')
    assert b'event: feed' in response.data
    assert b'"audit"' not in response.data


def test_bulk_session_termination_preserves_all_admins(app, qa_admin):
    from database import db
    from database.models import User, ActiveSession
    db.session.get(User, ACCOUNTS['user2']['email']).is_admin = True
    db.session.commit()
    second_admin = logged_in_client(app, 'user2')
    regular = logged_in_client(app, 'user1')
    logged_in_client(app, 'user1')  # a second device for the same user
    logged_in_client(app, 'newbie')
    assert regular.post('/admin/sessions/terminate-non-admins').status_code == 403
    before = {s.session_token for s in ActiveSession.query.all() if s.user_id in
              [ACCOUNTS['admin']['email'], ACCOUNTS['user2']['email']]}
    app.config['WTF_CSRF_ENABLED'] = True
    assert qa_admin.post('/admin/sessions/terminate-non-admins').status_code == 400
    app.config['WTF_CSRF_ENABLED'] = False
    response = qa_admin.post('/admin/sessions/terminate-non-admins')
    assert response.status_code == 200
    assert response.json['terminated'] == 3
    assert {s.session_token for s in ActiveSession.query.all()} == before
    assert second_admin.get('/admin/sessions').status_code == 200
    assert regular.get('/my-analytics').status_code in (302, 401)
    assert qa_admin.post('/admin/sessions/terminate-non-admins').json['terminated'] == 0


def test_exception_cards_open_complete_details(app, qa_admin):
    from database import db
    from database.models import ExceptionLog
    from datetime import datetime
    row = ExceptionLog(exception_type='QAExampleError', exception_message='Full message <script>unsafe()</script>',
                       traceback='Traceback: QA full diagnostic', context_json='{"detail": "QA context"}',
                       last_seen_at=datetime(2026, 9, 25), first_seen_at=datetime(2026, 9, 1),
                       occurrence_count=4, filename='example.py', function='example', line_number=42)
    db.session.add(row)
    db.session.commit()
    path = f'/admin/issues/exceptions/{row.id}'
    listing = qa_admin.get('/admin/issues?q=QAExampleError')
    assert listing.status_code == 200
    assert path in listing.get_data(as_text=True)
    assert b'View full details' in listing.data
    assert b'<script>unsafe()' not in listing.data
    detail = qa_admin.get(path)
    assert detail.status_code == 200
    for content in [b'QA full diagnostic', b'QA context', b'example.py:42', b'Full message']:
        assert content in detail.data
    assert b'Linked reports' not in detail.data
    assert qa_admin.get('/admin/issues/exceptions/99999999').status_code == 404
