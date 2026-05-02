"""Socket.IO support messaging namespace."""

from __future__ import annotations

from flask import request
from flask_login import current_user

from services import support_service
from utils.exception_tracker import log_exception


def _user_room(user_id: str) -> str:
    return f"support:user:{user_id}"


def _conversation_room(conversation_id: int) -> str:
    return f"support:conversation:{conversation_id}"


def emit_support_update(socketio, conversation, *, admin_viewer_id: str | None = None, event: str = "support:conversation:update") -> None:
    """Broadcast a conversation update to admin inboxes and the owning user."""
    user_payload = support_service.serialize_conversation(
        conversation,
        viewer_id=conversation.user_id,
        viewer_type="user",
    )
    admin_payload = support_service.serialize_conversation(
        conversation,
        viewer_id=admin_viewer_id,
        viewer_type="admin",
    )
    socketio.emit(event, {"conversation": admin_payload}, to="support:admin", namespace="/support")
    socketio.emit("support:conversation:update", {"conversation": user_payload}, to=_user_room(conversation.user_id), namespace="/support")
    socketio.emit("support:conversation:update", {"conversation": admin_payload}, to=_conversation_room(conversation.id), namespace="/support")


def emit_support_message(socketio, conversation, message, *, rate=None, admin_viewer_id: str | None = None, is_new_conversation: bool = False) -> None:
    """Broadcast a newly persisted support message."""
    if is_new_conversation:
        emit_support_update(socketio, conversation, admin_viewer_id=admin_viewer_id, event="support:conversation:new")
    message_payload = support_service.serialize_message(message)
    user_payload = support_service.serialize_conversation(
        conversation,
        viewer_id=conversation.user_id,
        viewer_type="user",
    )
    admin_payload = support_service.serialize_conversation(
        conversation,
        viewer_id=admin_viewer_id,
        viewer_type="admin",
    )
    socketio.emit("support:message:new", {
        "conversation": user_payload,
        "message": message_payload,
        "rate": rate,
    }, to=_user_room(conversation.user_id), namespace="/support")
    socketio.emit("support:message:new", {
        "conversation": admin_payload,
        "message": message_payload,
    }, to=_conversation_room(conversation.id), namespace="/support")
    socketio.emit("support:conversation:update", {
        "conversation": admin_payload,
    }, to="support:admin", namespace="/support")


def register_support_realtime(app, *, socketio, db):
    if socketio is None:
        app.logger.warning("[support_realtime] SocketIO not available - realtime support disabled.")
        return

    from flask_socketio import disconnect, emit, join_room, leave_room

    @socketio.on("connect", namespace="/support")
    def _support_connect():
        if not current_user.is_authenticated:
            disconnect()
            return False

        join_room(_user_room(current_user.id))
        role = "admin" if getattr(current_user, "is_admin", False) else "user"
        if role == "admin":
            join_room("support:admin")
        emit("support:hello", {"role": role, "user_id": current_user.id})

    @socketio.on("support:conversation:join", namespace="/support")
    def _support_join(data):
        public_id = (data or {}).get("conversation_id")
        if not public_id:
            return
        conv = support_service.get_conversation(public_id)
        if conv is None:
            emit("support:error", {"error": "conversation_not_found"})
            return
        if not getattr(current_user, "is_admin", False) and conv.user_id != current_user.id:
            emit("support:error", {"error": "forbidden"})
            return
        join_room(_conversation_room(conv.id))
        emit("support:conversation:joined", {
            "conversation": support_service.serialize_conversation(
                conv,
                viewer_id=current_user.id,
                viewer_type="admin" if getattr(current_user, "is_admin", False) else "user",
            )
        })

    @socketio.on("support:conversation:leave", namespace="/support")
    def _support_leave(data):
        public_id = (data or {}).get("conversation_id")
        conv = support_service.get_conversation(public_id) if public_id else None
        if conv is not None:
            leave_room(_conversation_room(conv.id))

    @socketio.on("support:message:send", namespace="/support")
    def _support_message_send(data):
        data = data or {}
        sender_type = "admin" if getattr(current_user, "is_admin", False) and data.get("conversation_id") else "user"

        try:
            if sender_type == "admin":
                conv = support_service.get_conversation(data.get("conversation_id"))
                if conv is None:
                    emit("support:error", {"error": "conversation_not_found"})
                    return
            else:
                was_new = support_service.get_user_latest_conversation(current_user.id) is None
                conv = support_service.get_or_create_user_conversation(current_user.id, {
                    "page_url": data.get("page_url"),
                    "app_version": data.get("app_version"),
                    "user_agent": request.headers.get("User-Agent"),
                })

            msg, meta = support_service.create_message(
                conv,
                sender_type=sender_type,
                sender_id=current_user.id,
                body=data.get("body"),
                client_nonce=data.get("client_nonce"),
            )
            db.session.commit()

            emit_support_message(
                socketio,
                conv,
                msg,
                rate=meta.get("rate"),
                admin_viewer_id=current_user.id,
                is_new_conversation=(sender_type == "user" and was_new and not meta.get("duplicate")),
            )
        except support_service.RateLimited as exc:
            db.session.rollback()
            emit("support:error", {
                "error": "rate_limited",
                "retry_after": exc.payload.get("retry_after", 60),
                "rate": exc.payload,
            })
        except ValueError as exc:
            db.session.rollback()
            emit("support:error", {"error": "validation_error", "message": str(exc)})
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="support_realtime.message")
            emit("support:error", {"error": "send_failed"})

    @socketio.on("support:read", namespace="/support")
    def _support_read(data):
        public_id = (data or {}).get("conversation_id")
        conv = support_service.get_conversation(public_id) if public_id else None
        if conv is None:
            emit("support:error", {"error": "conversation_not_found"})
            return
        reader_type = "admin" if getattr(current_user, "is_admin", False) else "user"
        if reader_type == "user" and conv.user_id != current_user.id:
            emit("support:error", {"error": "forbidden"})
            return
        try:
            state = support_service.mark_read(conv, reader_type=reader_type, reader_id=current_user.id)
            db.session.commit()
            payload = {
                "conversation_id": conv.public_id,
                "reader_type": reader_type,
                "reader_id": current_user.id,
                "last_read_message_id": state.last_read_message_id,
                "last_read_at": state.last_read_at.isoformat() if state.last_read_at else None,
            }
            socketio.emit("support:read:update", payload, to=_conversation_room(conv.id), namespace="/support")
            if reader_type == "admin":
                socketio.emit(
                    "support:admin:refresh",
                    {"conversation_id": conv.public_id},
                    to="support:admin",
                    namespace="/support",
                )
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="support_realtime.read")
            emit("support:error", {"error": "read_failed"})

    @socketio.on("support:typing", namespace="/support")
    def _support_typing(data):
        public_id = (data or {}).get("conversation_id")
        conv = support_service.get_conversation(public_id) if public_id else None
        if conv is None:
            return
        is_admin = getattr(current_user, "is_admin", False)
        if not is_admin and conv.user_id != current_user.id:
            return
        payload = {
            "conversation_id": conv.public_id,
            "sender_type": "admin" if is_admin else "user",
            "sender_id": current_user.id,
            "typing": bool((data or {}).get("typing")),
        }
        target = _user_room(conv.user_id) if is_admin else "support:admin"
        socketio.emit("support:typing", payload, to=target, namespace="/support")

    app.logger.info("[support_realtime] namespace /support handlers registered")
