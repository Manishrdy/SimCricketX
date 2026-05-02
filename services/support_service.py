"""Support conversation service.

Centralizes the DB-backed chat behavior used by HTTP routes and Socket.IO.
Manual user support messages stay in-app; only automatic exception logging
continues to use the GitHub issue flow.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_

from database import db
from database.models import (
    SupportConversation,
    SupportConversationReadState,
    SupportMessage,
    User,
)

MAX_MESSAGE_LEN = 4000
USER_CONSECUTIVE_LIMIT = 5
RETENTION_DAYS_AFTER_SEEN = 7


def _now() -> datetime:
    return datetime.utcnow()


def generate_public_id() -> str:
    return "SUP-" + secrets.token_hex(5).upper()


def trim(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def admin_unread_total(admin_id: str) -> int:
    """Total user-sent messages across non-closed conversations not yet read by this admin."""
    if not admin_id:
        return 0
    open_convs = (
        SupportConversation.query
        .filter(SupportConversation.status != "closed")
        .all()
    )
    if not open_convs:
        return 0
    states = {
        s.conversation_id: s.last_read_message_id or 0
        for s in SupportConversationReadState.query
            .filter(SupportConversationReadState.reader_type == "admin")
            .filter(SupportConversationReadState.reader_id == admin_id)
            .filter(SupportConversationReadState.conversation_id.in_([c.id for c in open_convs]))
            .all()
    }
    total = 0
    for conv in open_convs:
        last_id = states.get(conv.id, 0)
        total += (
            SupportMessage.query
            .filter(SupportMessage.conversation_id == conv.id)
            .filter(SupportMessage.sender_type == "user")
            .filter(SupportMessage.id > last_id)
            .count()
        )
    return total


def get_user_active_conversation(user_id: str) -> SupportConversation | None:
    return (
        SupportConversation.query
        .filter(SupportConversation.user_id == user_id)
        .filter(SupportConversation.status != "closed")
        .order_by(SupportConversation.last_message_at.desc().nullslast(), SupportConversation.created_at.desc())
        .first()
    )


def get_user_latest_conversation(user_id: str) -> SupportConversation | None:
    return (
        SupportConversation.query
        .filter(SupportConversation.user_id == user_id)
        .order_by(SupportConversation.last_message_at.desc().nullslast(), SupportConversation.created_at.desc())
        .first()
    )


def get_or_create_user_conversation(user_id: str, context: dict[str, Any] | None = None) -> SupportConversation:
    conv = get_user_latest_conversation(user_id)
    if conv is not None:
        context = context or {}
        if context.get("page_url"):
            conv.source_page_url = trim(context.get("page_url"), 500) or conv.source_page_url
        if context.get("app_version"):
            conv.app_version = trim(context.get("app_version"), 50) or conv.app_version
        if context.get("user_agent"):
            conv.user_agent = trim(context.get("user_agent"), 500) or conv.user_agent
        return conv

    context = context or {}
    now = _now()
    for _ in range(5):
        public_id = generate_public_id()
        if not SupportConversation.query.filter_by(public_id=public_id).first():
            break
    conv = SupportConversation(
        public_id=public_id,
        user_id=user_id,
        status="open",
        priority="normal",
        source_page_url=trim(context.get("page_url"), 500) or None,
        app_version=trim(context.get("app_version"), 50) or None,
        user_agent=trim(context.get("user_agent"), 500) or None,
        created_at=now,
        updated_at=now,
    )
    db.session.add(conv)
    db.session.flush()
    return conv


def get_conversation_for_user(public_id: str, user_id: str) -> SupportConversation | None:
    return SupportConversation.query.filter_by(public_id=public_id, user_id=user_id).first()


def get_conversation(public_id: str) -> SupportConversation | None:
    return SupportConversation.query.filter_by(public_id=public_id).first()


def check_user_message_rate_limit(user_id: str, conversation_id: int | None = None) -> dict[str, Any]:
    if conversation_id is None:
        return {
            "allowed": True,
            "retry_after": 0,
            "limit": USER_CONSECUTIVE_LIMIT,
            "mode": "until_admin_reply",
            "blocked_until_admin_reply": False,
            "consecutive_user_messages": 0,
        }

    latest_admin = (
        SupportMessage.query
        .filter_by(conversation_id=conversation_id, sender_type="admin")
        .order_by(SupportMessage.created_at.desc(), SupportMessage.id.desc())
        .first()
    )
    q = SupportMessage.query.filter(
        SupportMessage.conversation_id == conversation_id,
        SupportMessage.sender_type == "user",
        SupportMessage.sender_id == user_id,
    )
    if latest_admin is not None:
        q = q.filter(SupportMessage.created_at > latest_admin.created_at)
    consecutive_count = q.count()
    if consecutive_count >= USER_CONSECUTIVE_LIMIT:
        return {
            "allowed": False,
            "retry_after": None,
            "limit": USER_CONSECUTIVE_LIMIT,
            "mode": "until_admin_reply",
            "blocked_until_admin_reply": True,
            "consecutive_user_messages": consecutive_count,
        }

    return {
        "allowed": True,
        "retry_after": 0,
        "limit": USER_CONSECUTIVE_LIMIT,
        "mode": "until_admin_reply",
        "blocked_until_admin_reply": False,
        "consecutive_user_messages": consecutive_count,
    }


def create_message(
    conversation: SupportConversation,
    *,
    sender_type: str,
    sender_id: str | None,
    body: str,
    client_nonce: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[SupportMessage, dict[str, Any]]:
    sender_type = sender_type if sender_type in {"user", "admin", "system"} else "user"
    body = trim(body, MAX_MESSAGE_LEN)
    if not body:
        raise ValueError("message body is required")

    if sender_type == "user":
        rate = check_user_message_rate_limit(sender_id or "", conversation.id)
        if not rate["allowed"]:
            raise RateLimited(rate)

    if client_nonce and sender_id:
        existing = (
            SupportMessage.query
            .filter_by(conversation_id=conversation.id, sender_id=sender_id, client_nonce=trim(client_nonce, 80))
            .first()
        )
        if existing:
            return existing, {"duplicate": True, "rate": check_user_message_rate_limit(sender_id, conversation.id)}

    now = _now()
    msg = SupportMessage(
        conversation_id=conversation.id,
        sender_type=sender_type,
        sender_id=sender_id,
        body=body,
        client_nonce=trim(client_nonce, 80) or None,
        created_at=now,
    )
    db.session.add(msg)

    conversation.last_message_at = now
    conversation.updated_at = now
    conversation.retention_eligible_at = None
    conversation.hard_delete_at = None
    if sender_type == "user":
        conversation.last_user_message_at = now
        conversation.closed_at = None
        conversation.closed_by = None
        conversation.status = "pending_admin"
        if not conversation.subject:
            conversation.subject = body[:120]
    elif sender_type == "admin":
        conversation.last_admin_message_at = now
        conversation.status = "pending_user"

    db.session.flush()
    return msg, {"duplicate": False, "rate": check_user_message_rate_limit(sender_id or "", conversation.id)}


def mark_read(conversation: SupportConversation, *, reader_type: str, reader_id: str) -> SupportConversationReadState:
    reader_type = reader_type if reader_type in {"user", "admin"} else "user"
    now = _now()
    latest = (
        SupportMessage.query
        .filter_by(conversation_id=conversation.id)
        .order_by(SupportMessage.created_at.desc(), SupportMessage.id.desc())
        .first()
    )
    state = (
        SupportConversationReadState.query
        .filter_by(conversation_id=conversation.id, reader_type=reader_type, reader_id=reader_id)
        .first()
    )
    if state is None:
        state = SupportConversationReadState(
            conversation_id=conversation.id,
            reader_type=reader_type,
            reader_id=reader_id,
        )
        db.session.add(state)
    state.last_read_message_id = latest.id if latest else None
    state.last_read_at = now
    apply_retention_state(conversation)
    db.session.flush()
    return state


def apply_retention_state(conversation: SupportConversation) -> None:
    latest_user = (
        SupportMessage.query
        .filter_by(conversation_id=conversation.id, sender_type="user")
        .order_by(SupportMessage.created_at.desc(), SupportMessage.id.desc())
        .first()
    )
    latest_admin = (
        SupportMessage.query
        .filter_by(conversation_id=conversation.id, sender_type="admin")
        .order_by(SupportMessage.created_at.desc(), SupportMessage.id.desc())
        .first()
    )
    admin_seen = True
    user_seen = True

    if latest_user is not None:
        admin_seen = (
            SupportConversationReadState.query
            .filter_by(conversation_id=conversation.id, reader_type="admin")
            .filter(or_(
                SupportConversationReadState.last_read_message_id >= latest_user.id,
                SupportConversationReadState.last_read_at >= latest_user.created_at,
            ))
            .first()
            is not None
        )
    if latest_admin is not None:
        user_seen = (
            SupportConversationReadState.query
            .filter_by(conversation_id=conversation.id, reader_type="user", reader_id=conversation.user_id)
            .filter(or_(
                SupportConversationReadState.last_read_message_id >= latest_admin.id,
                SupportConversationReadState.last_read_at >= latest_admin.created_at,
            ))
            .first()
            is not None
        )

    if admin_seen and user_seen and conversation.last_message_at:
        conversation.retention_eligible_at = _now() + timedelta(days=RETENTION_DAYS_AFTER_SEEN)


def close_conversation(conversation: SupportConversation, admin_id: str | None = None) -> None:
    now = _now()
    conversation.status = "closed"
    conversation.closed_at = now
    conversation.closed_by = admin_id
    conversation.updated_at = now
    apply_retention_state(conversation)


def serialize_message(msg: SupportMessage) -> dict[str, Any]:
    return {
        "id": msg.id,
        "conversation_id": msg.conversation.public_id if msg.conversation else None,
        "sender_type": msg.sender_type,
        "sender_id": msg.sender_id,
        "body": msg.body,
        "message_type": msg.message_type,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
        "deleted": msg.deleted_at is not None,
    }


def serialize_conversation(conversation: SupportConversation, *, viewer_id: str | None = None, viewer_type: str = "user") -> dict[str, Any]:
    last_msg = (
        SupportMessage.query
        .filter_by(conversation_id=conversation.id)
        .order_by(SupportMessage.created_at.desc(), SupportMessage.id.desc())
        .first()
    )
    unread_count = 0
    if viewer_id:
        state = (
            SupportConversationReadState.query
            .filter_by(conversation_id=conversation.id, reader_type=viewer_type, reader_id=viewer_id)
            .first()
        )
        q = SupportMessage.query.filter(SupportMessage.conversation_id == conversation.id)
        if state and state.last_read_message_id:
            q = q.filter(SupportMessage.id > state.last_read_message_id)
        if viewer_type == "user":
            q = q.filter(SupportMessage.sender_type == "admin")
        elif viewer_type == "admin":
            q = q.filter(SupportMessage.sender_type == "user")
        unread_count = q.count()

    user = db.session.get(User, conversation.user_id)
    return {
        "id": conversation.public_id,
        "user_id": conversation.user_id,
        "user_label": (user.display_name or user.id) if user else conversation.user_id,
        "assigned_admin_id": conversation.assigned_admin_id,
        "status": conversation.status,
        "priority": conversation.priority,
        "subject": conversation.subject,
        "source_page_url": conversation.source_page_url,
        "app_version": conversation.app_version,
        "last_message_at": conversation.last_message_at.isoformat() if conversation.last_message_at else None,
        "created_at": conversation.created_at.isoformat() if conversation.created_at else None,
        "closed_at": conversation.closed_at.isoformat() if conversation.closed_at else None,
        "retention_eligible_at": conversation.retention_eligible_at.isoformat() if conversation.retention_eligible_at else None,
        "unread_count": unread_count,
        "last_message": serialize_message(last_msg) if last_msg else None,
    }


class RateLimited(Exception):
    def __init__(self, payload: dict[str, Any]):
        super().__init__("support message rate limited")
        self.payload = payload
