"""Admin support inbox routes."""

from __future__ import annotations

from flask import jsonify, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import or_

from auth.decorators import admin_required
from database.models import ExceptionLog, SupportConversation, SupportMessage, User
from routes.support_realtime import emit_support_message, emit_support_update
from services import support_service
from utils.exception_tracker import log_exception


def register_admin_support_routes(app, *, db, socketio=None):
    def _conversation_query(status=None, search=None):
        q = SupportConversation.query
        if status:
            if status == "open":
                q = q.filter(SupportConversation.status != "closed")
            else:
                q = q.filter(SupportConversation.status == status)
        if search:
            like = f"%{search}%"
            q = q.outerjoin(User, User.id == SupportConversation.user_id).filter(or_(
                SupportConversation.public_id.ilike(like),
                SupportConversation.subject.ilike(like),
                SupportConversation.user_id.ilike(like),
                User.display_name.ilike(like),
            ))
        return q.order_by(SupportConversation.last_message_at.desc().nullslast(), SupportConversation.created_at.desc())

    def _messages(conversation, limit=80):
        rows = (
            SupportMessage.query
            .filter_by(conversation_id=conversation.id)
            .order_by(SupportMessage.created_at.desc(), SupportMessage.id.desc())
            .limit(max(1, min(int(limit or 80), 200)))
            .all()
        )
        rows.reverse()
        return [support_service.serialize_message(row) for row in rows]

    def _user_context(user_id):
        user = db.session.get(User, user_id)
        recent_exceptions = (
            ExceptionLog.query
            .filter(ExceptionLog.user_email == user_id)
            .order_by(ExceptionLog.timestamp.desc())
            .limit(5)
            .all()
        )
        return {
            "user": {
                "id": user.id if user else user_id,
                "display_name": user.display_name if user else None,
                "created_at": user.created_at.isoformat() if user and user.created_at else None,
                "last_login": user.last_login.isoformat() if user and user.last_login else None,
                "is_admin": bool(user.is_admin) if user else False,
            },
            "recent_exceptions": [
                {
                    "id": row.id,
                    "type": row.exception_type,
                    "message": (row.exception_message or "")[:160],
                    "timestamp": row.timestamp.isoformat() if row.timestamp else None,
                    "resolved": bool(row.resolved),
                }
                for row in recent_exceptions
            ],
        }

    def _broadcast_admin_refresh(conv_public_id=None):
        if socketio is None:
            return
        try:
            socketio.emit(
                "support:admin:refresh",
                {"conversation_id": conv_public_id} if conv_public_id else {},
                to="support:admin",
                namespace="/support",
            )
        except Exception:
            pass

    @app.route("/api/admin/support/unread-count")
    @login_required
    @admin_required
    def admin_support_unread_count():
        try:
            return jsonify({"count": support_service.admin_unread_total(current_user.id)})
        except Exception as exc:
            log_exception(exc, source="backend", context={"scope": "admin_support_unread_count"})
            return jsonify({"count": 0})

    @app.route("/admin/support")
    @login_required
    @admin_required
    def admin_support():
        return render_template("admin/support.html")

    @app.route("/api/admin/support/conversations")
    @login_required
    @admin_required
    def admin_support_conversations():
        status = (request.args.get("status") or "").strip() or None
        search = (request.args.get("q") or "").strip() or None
        try:
            rows = _conversation_query(status=status, search=search).limit(100).all()
            return jsonify({
                "conversations": [
                    support_service.serialize_conversation(row, viewer_id=current_user.id, viewer_type="admin")
                    for row in rows
                ]
            })
        except Exception as exc:
            log_exception(exc, source="backend", context={"scope": "admin_support_conversations"})
            return jsonify({"error": "Failed to load conversations"}), 500

    @app.route("/api/admin/support/conversations/<public_id>")
    @login_required
    @admin_required
    def admin_support_conversation(public_id):
        conv = support_service.get_conversation(public_id)
        if conv is None:
            return jsonify({"error": "Conversation not found"}), 404
        return jsonify({
            "conversation": support_service.serialize_conversation(conv, viewer_id=current_user.id, viewer_type="admin"),
            "messages": _messages(conv),
            "context": _user_context(conv.user_id),
        })

    @app.route("/api/admin/support/conversations/<public_id>/messages")
    @login_required
    @admin_required
    def admin_support_messages(public_id):
        conv = support_service.get_conversation(public_id)
        if conv is None:
            return jsonify({"error": "Conversation not found"}), 404
        return jsonify({"messages": _messages(conv, request.args.get("limit", 80))})

    @app.route("/api/admin/support/conversations/<public_id>/messages", methods=["POST"])
    @login_required
    @admin_required
    def admin_support_send_message(public_id):
        conv = support_service.get_conversation(public_id)
        if conv is None:
            return jsonify({"error": "Conversation not found"}), 404
        payload = request.get_json(silent=True) or {}
        try:
            msg, meta = support_service.create_message(
                conv,
                sender_type="admin",
                sender_id=current_user.id,
                body=payload.get("body"),
                client_nonce=payload.get("client_nonce"),
            )
            db.session.commit()
            if socketio is not None:
                emit_support_message(socketio, conv, msg, admin_viewer_id=current_user.id)
            return jsonify({
                "conversation": support_service.serialize_conversation(conv, viewer_id=current_user.id, viewer_type="admin"),
                "message": support_service.serialize_message(msg),
                "duplicate": meta.get("duplicate", False),
            }), 201
        except ValueError as exc:
            db.session.rollback()
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="backend", context={"scope": "admin_support_send_message"})
            return jsonify({"error": "Failed to send message"}), 500

    @app.route("/api/admin/support/conversations/<public_id>/read", methods=["POST"])
    @login_required
    @admin_required
    def admin_support_mark_read(public_id):
        conv = support_service.get_conversation(public_id)
        if conv is None:
            return jsonify({"error": "Conversation not found"}), 404
        try:
            state = support_service.mark_read(conv, reader_type="admin", reader_id=current_user.id)
            db.session.commit()
            _broadcast_admin_refresh(conv.public_id)
            return jsonify({
                "status": "ok",
                "last_read_message_id": state.last_read_message_id,
                "last_read_at": state.last_read_at.isoformat() if state.last_read_at else None,
                "conversation": support_service.serialize_conversation(conv, viewer_id=current_user.id, viewer_type="admin"),
            })
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="backend", context={"scope": "admin_support_mark_read"})
            return jsonify({"error": "Failed to mark read"}), 500

    @app.route("/api/admin/support/conversations/<public_id>/close", methods=["POST"])
    @login_required
    @admin_required
    def admin_support_close(public_id):
        conv = support_service.get_conversation(public_id)
        if conv is None:
            return jsonify({"error": "Conversation not found"}), 404
        try:
            support_service.close_conversation(conv, current_user.id)
            db.session.commit()
            if socketio is not None:
                emit_support_update(socketio, conv, admin_viewer_id=current_user.id)
            return jsonify({
                "status": "ok",
                "conversation": support_service.serialize_conversation(conv, viewer_id=current_user.id, viewer_type="admin"),
            })
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="backend", context={"scope": "admin_support_close"})
            return jsonify({"error": "Failed to close conversation"}), 500

    @app.route("/api/admin/support/conversations/<public_id>/reopen", methods=["POST"])
    @login_required
    @admin_required
    def admin_support_reopen(public_id):
        conv = support_service.get_conversation(public_id)
        if conv is None:
            return jsonify({"error": "Conversation not found"}), 404
        try:
            conv.status = "open"
            conv.closed_at = None
            conv.closed_by = None
            conv.hard_delete_at = None
            conv.retention_eligible_at = None
            db.session.commit()
            if socketio is not None:
                emit_support_update(socketio, conv, admin_viewer_id=current_user.id)
            return jsonify({
                "status": "ok",
                "conversation": support_service.serialize_conversation(conv, viewer_id=current_user.id, viewer_type="admin"),
            })
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="backend", context={"scope": "admin_support_reopen"})
            return jsonify({"error": "Failed to reopen conversation"}), 500

    @app.route("/api/admin/support/conversations/<public_id>", methods=["DELETE"])
    @login_required
    @admin_required
    def admin_support_delete(public_id):
        conv = support_service.get_conversation(public_id)
        if conv is None:
            return jsonify({"error": "Conversation not found"}), 404
        try:
            db.session.delete(conv)
            db.session.commit()
            _broadcast_admin_refresh(public_id)
            return jsonify({"status": "ok", "conversation_id": public_id})
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="backend", context={"scope": "admin_support_delete"})
            return jsonify({"error": "Failed to delete conversation"}), 500
