"""Tests for in-app support messaging."""

from datetime import datetime, timedelta
import time

from database import db
from database.models import ActiveSession, SupportConversation, SupportMessage
from services import support_retention, support_service
from utils.exception_tracker import log_exception
from database.models import ExceptionLog


def _login(client, user):
    token = f"test-token-{user.id}"
    ActiveSession.query.filter_by(session_token=token).delete()
    db.session.add(ActiveSession(
        session_token=token,
        user_id=user.id,
        ip_address="127.0.0.1",
        user_agent="pytest",
        login_at=datetime.utcnow(),
        last_active=datetime.utcnow(),
    ))
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = user.id
        sess["_fresh"] = True
        sess["session_token"] = token
        sess["cf_ts_verified"] = time.time()


def test_user_can_send_support_message_and_admin_can_reply(client, app, regular_user, admin_user):
    with app.app_context():
        conv = support_service.get_or_create_user_conversation(regular_user.id, {"page_url": "/home"})
        support_service.create_message(
            conv,
            sender_type="user",
            sender_id=regular_user.id,
            body="hello admin",
        )
        db.session.commit()
        conv_id = conv.public_id
        assert conv.status == "pending_admin"
        assert conv.user_id == regular_user.id

    admin_client = app.test_client()
    _login(admin_client, admin_user)
    resp = admin_client.post(f"/api/admin/support/conversations/{conv_id}/messages", json={"body": "hello user"})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["message"]["sender_type"] == "admin"

    with app.app_context():
        conv = SupportConversation.query.filter_by(public_id=conv_id).one()
        assert conv.status == "pending_user"
        assert SupportMessage.query.filter_by(conversation_id=conv.id).count() == 2


def test_user_message_rate_limit_blocks_until_admin_reply(client, app, regular_user, admin_user):
    _login(client, regular_user)
    conv_id = None
    for i in range(5):
        resp = client.post("/api/support/messages", json={"body": f"msg {i}"})
        assert resp.status_code == 201
        conv_id = resp.get_json()["conversation"]["id"]

    resp = client.post("/api/support/messages", json={"body": "blocked"})
    assert resp.status_code == 429
    payload = resp.get_json()
    assert payload["error"] == "rate_limited"
    assert payload["rate"]["blocked_until_admin_reply"] is True
    assert payload["rate"]["limit"] == 5

    with app.app_context():
        conv = support_service.get_conversation(conv_id)
        support_service.create_message(
            conv,
            sender_type="admin",
            sender_id=admin_user.id,
            body="admin reply",
        )
        db.session.commit()

    resp = client.post("/api/support/messages", json={"body": "unblocked"})
    assert resp.status_code == 201


def test_admin_can_list_close_and_reopen(client, app, regular_user, admin_user):
    with app.app_context():
        conv = support_service.get_or_create_user_conversation(regular_user.id, {})
        support_service.create_message(
            conv,
            sender_type="user",
            sender_id=regular_user.id,
            body="need help",
        )
        db.session.commit()
        conv_id = conv.public_id

    admin_client = app.test_client()
    _login(admin_client, admin_user)
    list_resp = admin_client.get("/api/admin/support/conversations?status=open")
    assert list_resp.status_code == 200
    assert any(row["id"] == conv_id for row in list_resp.get_json()["conversations"])

    close_resp = admin_client.post(f"/api/admin/support/conversations/{conv_id}/close", json={})
    assert close_resp.status_code == 200
    assert close_resp.get_json()["conversation"]["status"] == "closed"

    open_resp = admin_client.get("/api/admin/support/conversations?status=open")
    assert open_resp.status_code == 200
    assert not any(row["id"] == conv_id for row in open_resp.get_json()["conversations"])

    closed_resp = admin_client.get("/api/admin/support/conversations?status=closed")
    assert closed_resp.status_code == 200
    assert any(row["id"] == conv_id for row in closed_resp.get_json()["conversations"])

    reopen_resp = admin_client.post(f"/api/admin/support/conversations/{conv_id}/reopen", json={})
    assert reopen_resp.status_code == 200
    assert reopen_resp.get_json()["conversation"]["status"] == "open"


def test_user_message_reopens_same_closed_thread(client, app, regular_user, admin_user):
    with app.app_context():
        conv = support_service.get_or_create_user_conversation(regular_user.id, {})
        support_service.create_message(
            conv,
            sender_type="user",
            sender_id=regular_user.id,
            body="first round",
        )
        support_service.close_conversation(conv, admin_user.id)
        db.session.commit()
        conv_id = conv.id
        public_id = conv.public_id

    _login(client, regular_user)
    resp = client.post("/api/support/messages", json={"body": "second round"})
    assert resp.status_code == 201
    payload = resp.get_json()
    assert payload["conversation"]["id"] == public_id
    assert payload["conversation"]["status"] == "pending_admin"

    with app.app_context():
        assert SupportConversation.query.filter_by(user_id=regular_user.id).count() == 1
        conv = db.session.get(SupportConversation, conv_id)
        assert conv.public_id == public_id
        assert conv.closed_at is None
        assert SupportMessage.query.filter_by(conversation_id=conv_id).count() == 2


def test_admin_can_delete_support_conversation(client, app, regular_user, admin_user):
    with app.app_context():
        conv = support_service.get_or_create_user_conversation(regular_user.id, {})
        support_service.create_message(
            conv,
            sender_type="user",
            sender_id=regular_user.id,
            body="delete me",
        )
        db.session.commit()
        conv_id = conv.id
        public_id = conv.public_id

    admin_client = app.test_client()
    _login(admin_client, admin_user)
    resp = admin_client.delete(f"/api/admin/support/conversations/{public_id}")
    assert resp.status_code == 200
    assert resp.get_json()["conversation_id"] == public_id

    with app.app_context():
        assert db.session.get(SupportConversation, conv_id) is None
        assert SupportMessage.query.filter_by(conversation_id=conv_id).count() == 0


def test_retention_deletes_expired_seen_messages_but_keeps_conversation(app, regular_user, admin_user):
    with app.app_context():
        conv = support_service.get_or_create_user_conversation(regular_user.id, {})
        msg, _ = support_service.create_message(
            conv,
            sender_type="user",
            sender_id=regular_user.id,
            body="cleanup me",
        )
        db.session.commit()
        support_service.mark_read(conv, reader_type="admin", reader_id=admin_user.id)
        support_service.close_conversation(conv, admin_user.id)
        conv.retention_eligible_at = datetime.utcnow() - timedelta(seconds=1)
        msg.created_at = datetime.utcnow() - timedelta(days=8)
        conv.last_message_at = msg.created_at
        conv.last_user_message_at = msg.created_at
        db.session.commit()
        conv_id = conv.id

        result = support_retention.cleanup_expired_support_conversations(app)
        assert result["deleted_messages"] == 1
        db.session.expire_all()
        assert db.session.get(SupportConversation, conv_id) is not None
        assert SupportMessage.query.filter_by(conversation_id=conv_id).count() == 0


def test_exception_logging_still_works(app):
    with app.app_context():
        try:
            raise RuntimeError("support-migration-exception-check")
        except Exception as exc:
            row_id = log_exception(exc, source="backend")

        row = db.session.get(ExceptionLog, row_id)
        assert row is not None
        assert row.exception_message == "support-migration-exception-check"
