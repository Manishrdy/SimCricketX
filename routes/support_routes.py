"""User-facing support messaging routes."""

from __future__ import annotations

from flask import jsonify, request
from flask_login import current_user, login_required

from database.models import SupportMessage
from services import support_service
from routes.support_realtime import emit_support_message
from utils.exception_tracker import log_exception


def _message_page(conversation, limit: int = 50):
    limit = max(1, min(int(limit or 50), 100))
    rows = (
        SupportMessage.query
        .filter_by(conversation_id=conversation.id)
        .order_by(SupportMessage.created_at.desc(), SupportMessage.id.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()
    return [support_service.serialize_message(row) for row in rows]


def register_support_routes(app, *, db, socketio=None):
    @app.route("/api/support/current", methods=["GET"])
    @login_required
    def support_current():
        conv = support_service.get_user_active_conversation(current_user.id)
        if conv is None:
            conv = support_service.get_user_latest_conversation(current_user.id)
        if conv is None:
            return jsonify({"conversation": None, "messages": []})

        return jsonify({
            "conversation": support_service.serialize_conversation(
                conv,
                viewer_id=current_user.id,
                viewer_type="user",
            ),
            "messages": _message_page(conv),
            "rate": support_service.check_user_message_rate_limit(current_user.id, conv.id),
        })

    @app.route("/api/support/conversations/<public_id>/messages", methods=["GET"])
    @login_required
    def support_messages(public_id):
        conv = support_service.get_conversation_for_user(public_id, current_user.id)
        if conv is None:
            return jsonify({"error": "Conversation not found"}), 404
        limit = request.args.get("limit", 50)
        return jsonify({
            "conversation": support_service.serialize_conversation(
                conv,
                viewer_id=current_user.id,
                viewer_type="user",
            ),
            "messages": _message_page(conv, limit=limit),
            "rate": support_service.check_user_message_rate_limit(current_user.id, conv.id),
        })

    @app.route("/api/support/messages", methods=["POST"])
    @login_required
    def support_send_message_http():
        payload = request.get_json(silent=True) or {}
        try:
            was_new = support_service.get_user_latest_conversation(current_user.id) is None
            conv = support_service.get_or_create_user_conversation(current_user.id, {
                "page_url": payload.get("page_url") or request.referrer,
                "app_version": payload.get("app_version"),
                "user_agent": request.headers.get("User-Agent"),
            })
            msg, meta = support_service.create_message(
                conv,
                sender_type="user",
                sender_id=current_user.id,
                body=payload.get("body"),
                client_nonce=payload.get("client_nonce"),
            )
            db.session.commit()
            if socketio is not None:
                emit_support_message(
                    socketio,
                    conv,
                    msg,
                    rate=meta.get("rate"),
                    is_new_conversation=was_new and not meta.get("duplicate"),
                )
            return jsonify({
                "conversation": support_service.serialize_conversation(
                    conv,
                    viewer_id=current_user.id,
                    viewer_type="user",
                ),
                "message": support_service.serialize_message(msg),
                "rate": meta.get("rate"),
            }), 201
        except support_service.RateLimited as exc:
            db.session.rollback()
            return jsonify({
                "error": "rate_limited",
                "retry_after": exc.payload.get("retry_after", 60),
                "rate": exc.payload,
            }), 429
        except ValueError as exc:
            db.session.rollback()
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="backend", context={"scope": "support_send_message_http"})
            return jsonify({"error": "Failed to send message"}), 500

    @app.route("/api/support/conversations/<public_id>/read", methods=["POST"])
    @login_required
    def support_mark_read(public_id):
        conv = support_service.get_conversation_for_user(public_id, current_user.id)
        if conv is None:
            return jsonify({"error": "Conversation not found"}), 404
        try:
            state = support_service.mark_read(conv, reader_type="user", reader_id=current_user.id)
            db.session.commit()
            return jsonify({
                "status": "ok",
                "last_read_message_id": state.last_read_message_id,
                "last_read_at": state.last_read_at.isoformat() if state.last_read_at else None,
                "conversation": support_service.serialize_conversation(
                    conv,
                    viewer_id=current_user.id,
                    viewer_type="user",
                ),
            })
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="backend", context={"scope": "support_mark_read"})
            return jsonify({"error": "Failed to mark read"}), 500
