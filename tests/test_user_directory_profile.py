import json
from datetime import datetime
from sqlalchemy import event
from database import db
from database.models import User, Team, Match
from scripts.dev_test_accounts import seed, logged_in_client, ACCOUNTS


def test_directory_filters_cursor_and_query_bound(app):
    seed(app)
    client = logged_in_client(app, 'admin')
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        response = client.get('/admin/users')
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)
    assert response.status_code == 200
    assert b'aria-current="page">Recent' in response.data
    user_queries = [q for q in statements if 'FROM users' in q]
    assert any('LIMIT' in q and 'ORDER BY users.created_at DESC, users.id ASC' in q for q in user_queries)
    for kind, expected in [('admin', 'qa.admin@example.com'), ('user', 'qa.rohit@example.com')]:
        response = client.get('/admin/users', query_string={'filter':kind, 'partial':'1'})
        assert expected in response.json['html']
        if kind == 'user':
            assert 'qa.admin@example.com' not in response.json['html']
    response = client.get('/admin/users?filter=all&q=qa.rohit&partial=1')
    assert 'qa.rohit@example.com' in response.json['html']
    assert 'qa.priya@example.com' not in response.json['html']
    users = User.query.order_by(User.created_at.desc(), User.id.asc()).all()
    cursor = json.dumps([users[1].created_at.isoformat(), users[1].id])
    response = client.get('/admin/users', query_string={'filter':'all', 'cursor':cursor, 'partial':'1'})
    assert users[0].id not in response.json['html'] and users[1].id not in response.json['html']
    assert users[2].id in response.json['html']
    assert client.get('/admin/users?filter=all&cursor=bad').status_code == 400
    assert client.get('/admin/users?filter=all&cursor=[1,2]').status_code == 400
    regular = logged_in_client(app, 'user1')
    assert regular.get('/admin/users?partial=1').status_code == 403


def test_profile_private_data_and_legacy_link(app):
    seed(app)
    owner = ACCOUNTS['user1']['email']
    other = ACCOUNTS['user2']['email']
    db.session.add_all([Team(user_id=owner, name='My QA XI', short_code='QA'), Team(user_id=other, name='Private rival XI', short_code='PR')])
    db.session.add(Match(id='qa-profile-match', user_id=owner, result_description='QA XI won', date=datetime.utcnow(), match_status='completed'))
    db.session.commit()
    client = logged_in_client(app, 'user1')
    response = client.get('/my-profile')
    assert response.status_code == 200
    for text in [b'My Profile', b'My QA XI', b'QA XI won', b'Member identity', b'Security & sign-ins']:
        assert text in response.data
    assert b'Private rival XI' not in response.data
    assert other.encode() not in response.data
    user = db.session.get(User, owner)
    assert user.password_hash.encode() not in response.data
    assert client.get('/my-analytics').location.endswith('/my-profile')
    assert app.test_client().get('/my-profile').status_code == 302
