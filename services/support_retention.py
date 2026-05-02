"""Retention cleanup for support conversations."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from database import db
from database.models import SupportConversation, SupportMessage
from services import support_service

logger = logging.getLogger("SimCricketX.support_retention")

_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


def cleanup_expired_support_conversations(app, *, now: datetime | None = None, batch_size: int = 200) -> dict[str, int]:
    """Delete old support messages while keeping one conversation per user."""
    now = now or datetime.utcnow()
    deleted_messages = 0
    touched_conversations = 0
    with app.app_context():
        rows = (
            SupportConversation.query
            .filter(SupportConversation.retention_eligible_at.isnot(None))
            .filter(SupportConversation.retention_eligible_at <= now)
            .order_by(SupportConversation.updated_at.asc())
            .limit(batch_size)
            .all()
        )
        cutoff = now - timedelta(days=support_service.RETENTION_DAYS_AFTER_SEEN)
        for conv in rows:
            old_messages = (
                SupportMessage.query
                .filter(SupportMessage.conversation_id == conv.id)
                .filter(SupportMessage.created_at <= cutoff)
                .all()
            )
            if not old_messages:
                conv.retention_eligible_at = None
                touched_conversations += 1
                continue

            for msg in old_messages:
                db.session.delete(msg)
                deleted_messages += 1
            db.session.flush()

            latest = (
                SupportMessage.query
                .filter_by(conversation_id=conv.id)
                .order_by(SupportMessage.created_at.desc(), SupportMessage.id.desc())
                .first()
            )
            conv.last_message_at = latest.created_at if latest else None
            latest_user = (
                SupportMessage.query
                .filter_by(conversation_id=conv.id, sender_type="user")
                .order_by(SupportMessage.created_at.desc(), SupportMessage.id.desc())
                .first()
            )
            conv.last_user_message_at = latest_user.created_at if latest_user else None
            latest_admin = (
                SupportMessage.query
                .filter_by(conversation_id=conv.id, sender_type="admin")
                .order_by(SupportMessage.created_at.desc(), SupportMessage.id.desc())
                .first()
            )
            conv.last_admin_message_at = latest_admin.created_at if latest_admin else None
            if latest is None:
                conv.subject = None
            conv.retention_eligible_at = None
            conv.hard_delete_at = None
            touched_conversations += 1

        if deleted_messages or touched_conversations:
            db.session.commit()
        else:
            db.session.rollback()
    if deleted_messages:
        logger.info(
            "support_retention: deleted %d expired message(s) across %d conversation(s)",
            deleted_messages,
            touched_conversations,
        )
    return {"deleted": deleted_messages, "deleted_messages": deleted_messages, "touched_conversations": touched_conversations}


def start_worker(app, *, interval_seconds: int = 6 * 3600) -> None:
    """Start a lightweight periodic cleanup thread once per process."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return

        def _loop():
            while True:
                try:
                    cleanup_expired_support_conversations(app)
                except Exception:
                    logger.exception("support_retention: cleanup failed")
                time.sleep(interval_seconds)

        _worker_thread = threading.Thread(
            target=_loop,
            name="support-retention-worker",
            daemon=True,
        )
        _worker_thread.start()
