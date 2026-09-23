"""Notification engine checks using the shared QA account definitions."""
from datetime import datetime

import pytest

from database import db
from database.models import CommunityNotification, User
from scripts.dev_test_accounts import ACCOUNTS, logged_in_client, seed
from services import community_service as cs

SEND_NOTIFICATION_EMAIL = cs.send_notification_email


@pytest.fixture
def qa(app, monkeypatch):
    seed(app)
    monkeypatch.setattr(cs, 'send_notification_email', lambda *a, **kw: None)
    users = {key: db.session.get(User, spec['email']) for key, spec in ACCOUNTS.items()}
    clients = {key: logged_in_client(app, key) for key in ACCOUNTS}
    return users, clients


def thread(user, **extra):
    return cs.create_post(user, dict(flair='question', title='[QA] Where are the rain settings?',
                                    body='Please explain where to find the rain settings for a match.', **extra))


def notes(comment):
    return {n.user_id: n.kind for n in CommunityNotification.query.filter_by(comment_id=comment.id)}


def test_recipients_priority_and_nested_ui(app, qa):
    u, c = qa
    post = thread(u['user1'])
    top = cs.add_comment(post, u['user2'], 'Open the match settings page first.')
    reply = cs.add_comment(post, u['user3'], 'Which tab should I open?', parent_id=top.id)
    result = c['user1'].post(f'/api/community/posts/{post.public_id}/comments', json={
        'body': f"@{u['user3'].display_name} look under the weather tab.",
        'parent_id': reply.id, 'mentions': [u['user3'].stable_id]})
    assert result.status_code == 201
    from database.models import CommunityComment
    nested = db.session.get(CommunityComment, result.json['comment']['id'])
    assert nested.parent_id == top.id
    assert notes(nested) == {u['user3'].id: 'reply'}
    html = c['user1'].get(f'/community/p/{post.public_id}').text
    assert f'data-parent="{reply.id}"' in html
    assert notes(reply) == {u['user2'].id: 'reply', u['user1'].id: 'comment'}
    self_reply = cs.add_comment(post, u['user1'], 'I have found the settings now.')
    assert notes(self_reply) == {}


def test_read_contract_visibility_and_history(app, qa):
    u, c = qa
    post = thread(u['user1'])
    comment = cs.add_comment(post, u['user2'], 'Open the match settings page first.')
    client = c['user1']
    payload = client.get('/api/community/notifications').json
    assert payload['unread'] == 1
    note = payload['items'][0]
    assert note['url'].endswith(f'#c{comment.id}')
    assert u['user2'].display_name in note['text'] and post.title in note['text']
    assert client.get('/community/notifications').status_code == 200
    assert client.get('/api/community/notifications').json['unread'] == 1
    for body in ({}, {'all': False}, {'ids': None}, {'ids': ['1']}, {'ids': [True]}, {'ids': [-1]}, {'ids': [], 'all': True}):
        assert client.post('/api/community/notifications/read', json=body).status_code == 400
    assert client.post('/api/community/notifications/read', json={'ids': []}).json['unread'] == 1
    c['user2'].post('/api/community/notifications/read', json={'ids': [note['id']]})
    assert client.get('/api/community/notifications').json['unread'] == 1
    assert client.post('/api/community/notifications/read', json={'ids': [note['id']]}).json['unread'] == 0
    assert client.get('/api/community/notifications').json['items'][0]['read'] is True
    # Access changes hide previously delivered alerts from both count and list.
    post.author_id = u['user3'].id
    post.visibility = 'private'
    db.session.commit()
    assert client.get('/api/community/notifications').json['items'] == []
    post.visibility = 'public'
    comment.deleted_at = datetime.utcnow()
    db.session.commit()
    assert client.get('/api/community/notifications').json['items'] == []


def test_pagination_and_explicit_mark_all(app, qa):
    u, c = qa
    post = thread(u['user1'])
    now = datetime.utcnow()
    for _ in range(35):
        db.session.add(CommunityNotification(user_id=u['user1'].id, kind='mention', post_id=post.id,
                                            actor_name='<script>unsafe</script>', created_at=now))
    db.session.commit()
    client = c['user1']
    first = client.get('/api/community/notifications?limit=30').json
    second = client.get('/api/community/notifications?limit=30&page=2').json
    assert len(first['items']) == 30 and first['has_next']
    assert len(second['items']) == 5 and not second['has_next']
    assert min(n['id'] for n in first['items']) > max(n['id'] for n in second['items'])
    html = client.get('/community/notifications').text
    assert html.count('data-notification-id=') == 30
    assert '&lt;script&gt;unsafe&lt;/script&gt;' in html
    assert client.get('/api/community/notifications').json['unread'] == 35
    for query in ('page=0', 'page=bad', 'limit=0', 'limit=101', 'page=1.5'):
        assert client.get('/api/community/notifications?' + query).status_code == 400
    assert client.post('/api/community/notifications/read', json={'all': True}).json['unread'] == 0
    post.deleted_at = datetime.utcnow()
    db.session.commit()
    assert client.get('/api/community/notifications').json['items'] == []


def test_mentions_edits_admin_and_csrf(app, qa):
    u, c = qa
    thread(u['user2'])  # eligible autocomplete member
    post = thread(u['user1'])
    body = f"@{u['user2'].display_name} please check the match settings."
    comment = cs.add_comment(post, u['user1'], body, mentions=[u['user2'].stable_id])
    assert notes(comment) == {u['user2'].id: 'mention'}
    cs.edit_comment(comment, u['user1'], body)
    assert CommunityNotification.query.filter_by(comment_id=comment.id).count() == 1
    admin_reply = cs.add_comment(post, u['admin'], 'Open the match settings page first.')
    assert notes(admin_reply) == {u['user1'].id: 'admin_response'}
    cs.set_status(post, u['admin'], 'answered')
    assert CommunityNotification.query.filter_by(user_id=u['user1'].id, kind='status_change').count() == 1
    private = thread(u['user1'], visibility='private')
    hidden = cs.add_comment(private, u['admin'], body, mentions=[u['user2'].stable_id])
    assert u['user2'].id not in notes(hidden)
    app.config['WTF_CSRF_ENABLED'] = True
    assert c['user1'].post('/api/community/notifications/read', json={'all': True}).status_code == 400


def test_admin_email_cooldown_survives_reply_priority(app, qa, monkeypatch):
    u, _ = qa
    sent = []
    monkeypatch.setattr(cs, 'send_notification_email', SEND_NOTIFICATION_EMAIL)
    monkeypatch.setattr('utils.email_service.send_email', lambda *args: sent.append(args))
    post = thread(u['user1'])
    comment = cs.add_comment(post, u['user1'], 'I cannot find the rain settings.')
    with app.test_request_context('/'):
        first = cs.add_comment(post, u['admin'], 'Open the match settings page first.', parent_id=comment.id)
        second = cs.add_comment(post, u['admin'], 'The weather tab has the setting.', parent_id=comment.id)
    assert notes(first) == notes(second) == {u['user1'].id: 'reply'}
    assert len(sent) == 1
