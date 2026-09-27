from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from types import SimpleNamespace
import threading

import pytest
from sqlalchemy import event
from database import db
from database.models import User, Team, Match, ActiveSession, LoginHistory, FailedLoginAttempt
from scripts.dev_test_accounts import ACCOUNTS, logged_in_client, seed
from services import admin_workspace as workspace


@pytest.fixture
def qa(app):
    seed(app)
    return logged_in_client(app, 'admin')


def user_url(key='user1', tab='overview'):
    return f"/admin/users/{ACCOUNTS[key]['email']}?tab={tab}"


def test_all_tabs_and_roles(app, qa):
    for key in ('user1', 'admin', 'newbie'):
        for tab in ('overview', 'security', 'activity', 'actions', 'invalid'):
            response = qa.get(user_url(key, tab))
            assert response.status_code == 200, (key, tab, response.status_code)
            assert b'aria-current="page"' in response.data
    user = db.session.get(User, ACCOUNTS['user1']['email'])
    user.is_banned = True
    user.ban_reason = 'QA test restriction'
    user.lockout_until = datetime.utcnow() + timedelta(hours=1)
    db.session.commit()
    for tab in ('overview', 'security', 'activity', 'actions'):
        response = qa.get(user_url(tab=tab))
        assert response.status_code == 200
        assert b'Locked' in response.data
    actions = qa.get(user_url(tab='actions')).data
    assert b'Unban user' in actions and b'Unlock account' in actions
    admin_actions = qa.get(user_url('admin', 'actions')).data
    assert b'name="new_password"' not in admin_actions
    assert b'Promote to admin' not in admin_actions
    assert b'Demote to user' not in admin_actions
    assert b'Delete account' not in admin_actions
    for tab in ('overview','database','tasks','invalid'):
        response = qa.get('/admin/health?tab=' + tab)
        assert response.status_code == 200
        sidebar = response.get_data(as_text=True).split('<nav class="admin-nav">')[1].split('</nav>')[0]
        assert 'data-label="Database"' not in sidebar and 'data-label="Tasks"' not in sidebar


def test_redirects_and_permissions(app, qa):
    email = ACCOUNTS['user1']['email']
    for suffix,tab in [('360','overview'),('analytics','overview'),('login-history','security')]:
        response = qa.get(f'/admin/users/{email}/{suffix}?page=3&event=login&date_from=2026-01-01')
        assert response.status_code == 302
        parsed = urlparse(response.location)
        assert parsed.path == f'/admin/users/{email}'
        assert parse_qs(parsed.query)['tab'] == [tab]
        assert 'history_page' not in parse_qs(parsed.query)
        assert 'event' not in parse_qs(parsed.query)
        assert qa.get('/admin/users/missing@example.com/' + suffix).status_code == 404
    for old,tab in [('database/stats','database'),('scheduled-tasks','tasks')]:
        response = qa.get('/admin/' + old)
        assert response.status_code == 302
        assert response.location == '/admin/health?tab=' + tab
    regular = logged_in_client(app, 'user1')
    for path in [user_url(), user_url(tab='actions'), '/admin/health', '/admin/database/stats', f'/admin/users/{email}/360']:
        assert regular.get(path).status_code == 403
    assert qa.get('/admin/unknown-tab-page').status_code == 404
    app.config['WTF_CSRF_ENABLED'] = True
    assert qa.post(f'/admin/users/{email}/force-reset').status_code == 400


def test_overview_counts_and_tab_query_isolation(app, qa):
    email = ACCOUNTS['user1']['email']
    for i in range(12):
        db.session.add(Team(name=f'QA Team {i}', short_code=f'Q{i}', user_id=email))
    now = datetime.utcnow()
    for i in range(12):
        db.session.add(ActiveSession(user_id=email, session_token=f'qa-session-{i}', last_active=now))
    db.session.add(ActiveSession(user_id=email, session_token='qa-stale', last_active=now-timedelta(days=3)))
    db.session.commit()
    data = workspace.user_overview(email)
    assert data['counts']['teams'] == 12
    assert set(data) == {'counts', 'active_sessions'}
    assert data['active_sessions'] == 12
    assert b'Recent Teams' not in qa.get(user_url()).data
    queries=[]
    def capture(conn,cursor,statement,parameters,context,executemany):
        queries.append((statement.lower(), parameters))
    event.listen(db.engine,'before_cursor_execute',capture)
    try:
        assert qa.get(user_url(tab='actions')).status_code == 200
    finally:
        event.remove(db.engine,'before_cursor_execute',capture)
    assert not any(('from matches' in sql or 'from teams' in sql or 'from players' in sql) and email in params for sql, params in queries)


def test_security_sessions_and_removed_history(app, qa):
    email = ACCOUNTS['user1']['email']
    now = datetime.utcnow()
    for i in range(30):
        db.session.add(ActiveSession(user_id=email, session_token=f'security-{i}', last_active=now-timedelta(days=3)))
    db.session.add(LoginHistory(user_id=email, event='login', ip_address='removed-history-ip'))
    db.session.add(FailedLoginAttempt(email=email, ip_address='removed-failed-ip'))
    db.session.commit()
    data = workspace.user_security(email, {'sessions_page': '999', 'date_from': 'invalid'})
    assert set(data) == {'sessions', 'cutoff'}
    assert data['sessions']['page'] == 2 and len(data['sessions']['items']) == 5
    assert workspace.user_security(email, {'sessions_page': 'bad'})['sessions']['page'] == 1
    response = qa.get(user_url(tab='security') + '&date_from=invalid')
    assert response.status_code == 200
    assert b'Stale' in response.data and b'Unknown' in response.data
    assert b'sessions_page=2' in response.data
    assert b'history_page=' not in response.data and b'failed_page=' not in response.data
    assert b'removed-history-ip' not in response.data and b'removed-failed-ip' not in response.data
    queries = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement.lower())
    event.listen(db.engine, 'before_cursor_execute', capture)
    try:
        workspace.user_overview(email)
        workspace.user_security(email, {})
    finally:
        event.remove(db.engine, 'before_cursor_execute', capture)
    assert not any('login_history' in sql or 'failed_login_attempts' in sql for sql in queries)


def test_removed_user_sections_and_self_service_unchanged(app, qa):
    user = db.session.get(User, ACCOUNTS['user1']['email'])
    user.last_login = datetime(2026, 1, 5, 12)
    db.session.commit()
    for tab in ('overview', 'security', 'actions', 'activity'):
        response = qa.get(user_url(tab=tab))
        assert response.status_code == 200
        content = response.get_data(as_text=True).split('<div class="user-workspace">', 1)[1]
        for removed in ('Recent Teams', 'Recent Matches', 'Recent Tournaments', 'Login history',
                        'Failed logins', 'Historical login IPs', 'Monthly matches', 'Top players', 'tab=activity'):
            assert removed not in content
        assert 'Last login' in content
        assert 'Never recorded' not in content
    response = qa.get(user_url(tab='activity'))
    assert b'Account Summary' in response.data
    regular = logged_in_client(app, 'user1')
    assert regular.get('/my-analytics').status_code == 200


def test_system_metrics_failure_isolation(app,tmp_path,monkeypatch):
    metrics=workspace.database_metrics()
    assert metrics['size_mb'] == round(__import__('os').path.getsize(db.engine.url.database)/1024**2,2)
    data=workspace.system_overview(tmp_path/'missing-root',None,{},threading.Lock())
    assert data['memory_mb'] is None and data['disk'] is None
    assert data['data_mb'] is None and data['log_mb'] is None and data['matches'] == 0
    backup_dir=tmp_path/'data'/'backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    assert workspace.latest_backup(tmp_path)['timestamp'] is None
    (backup_dir/'scheduled_backup.db').write_bytes(b'QA')
    state={'started':False}
    first=workspace.system_tasks(tmp_path,lambda:state['started'],lambda:(False,None))
    state['started']=True
    second=workspace.system_tasks(tmp_path,lambda:state['started'],lambda:(True,datetime.utcnow()))
    assert first['backup_started'] is False and second['backup_started'] is True
    assert second['backup']['timestamp'] is not None
    assert first['cleanup_last_run'] is None
    assert second['backup_hours'] == workspace.BACKUP_INTERVAL_SECONDS//3600
    def fail(*args,**kwargs): raise PermissionError('QA unavailable')
    monkeypatch.setattr(workspace.os,'walk',fail)
    data=workspace.system_overview(tmp_path,None,{'one':1},threading.Lock())
    assert data['data_mb'] is None and data['backup']['timestamp'] is not None and data['matches']==1


def test_in_memory_database_and_partial_process_metrics(app, monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    engine=create_engine('sqlite:///:memory:')
    db.session.remove()
    db.metadata.create_all(engine)
    monkeypatch.setitem(db.engines,None,engine)
    data=workspace.database_metrics()
    assert data['kind']=='SQLite in-memory'
    assert data['size_mb'] is None and data['counts']['users']==0
    class Process:
        def memory_info(self): raise PermissionError('QA denied')
        def cpu_percent(self,interval): return 3.5
        def create_time(self): return workspace.time.time()-3600
    psutil=SimpleNamespace(Process=Process,disk_usage=lambda root:SimpleNamespace(total=100,used=40,free=60,percent=40))
    data=workspace.system_overview(tmp_path,psutil,{},threading.Lock())
    assert data['memory_mb'] is None
    assert data['cpu']==3.5 and data['uptime_hours']==1.0
    assert data['disk'].free==60
    db.session.remove()
    engine.dispose()


def test_individual_termination_and_admin_action_restrictions(app,qa):
    email=ACCOUNTS['user1']['email']
    logged_in_client(app,'user1')
    session=ActiveSession.query.filter_by(user_id=email).first()
    session_id=session.id
    assert qa.post(f'/admin/sessions/{session_id}/terminate').status_code==200
    assert db.session.get(ActiveSession,session_id) is None
    admin_email=ACCOUNTS['admin']['email']
    assert qa.post(f'/admin/users/{admin_email}/toggle-admin').status_code==400
    for endpoint in ('delete','wipe-data','ban','force-reset'):
        assert qa.post(f'/admin/users/{admin_email}/{endpoint}').status_code==400
