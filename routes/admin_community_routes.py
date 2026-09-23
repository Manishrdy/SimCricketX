"""Admin moderation for the community board. Every action is audit-logged."""

from __future__ import annotations

from flask import jsonify, render_template, request
from flask_login import current_user, login_required

from auth.decorators import admin_required
from auth.user_auth import log_admin_action
from database import db
from database.models import CommunityComment, CommunityPost, CommunityReport, User
from services import community_service as cs
from utils.exception_tracker import log_exception


def _error(exc: cs.CommunityError):
    return jsonify({"error": exc.message, "code": exc.code, "fields": exc.fields}), exc.status


def register_admin_community_routes(app, *, db=db):

    @app.route("/admin/community")
    @login_required
    @admin_required
    def admin_community():
        queues = cs.admin_queues()
        return render_template(
            "admin/community.html",
            private=[cs.serialize_post(p, current_user) for p in queues["private"]],
            unanswered=[cs.serialize_post(p, current_user) for p in queues["unanswered"]],
            deleted=[cs.serialize_post(p, current_user, detail=True) for p in queues["deleted"]],
            reports=[_serialize_report(r) for r in queues["reports"]],
            muted=[{"email": u.id, "name": cs.author_name(u), "until": u.community_muted_until} for u in queues["muted"]],
            stats=cs.admin_stats(),
            report_reasons=cs.REPORT_REASONS,
        )

    def _serialize_report(r: CommunityReport) -> dict:
        target = r.comment.body if r.comment is not None else r.post.title
        return {
            "id": r.id, "reason": cs.REPORT_REASONS.get(r.reason, r.reason), "details": r.details,
            "created_at": r.created_at, "post_id": r.post.public_id, "post_title": r.post.title,
            "comment_id": r.comment_id, "excerpt": (target or "")[:200],
            "reporter": cs.author_name(db.session.get(User, r.reporter_id)) if r.reporter_id else cs.DELETED_USER_NAME,
        }

    @app.route("/api/admin/community/pending-count")
    @login_required
    @admin_required
    def admin_community_pending_count():
        try:
            return jsonify({"count": cs.admin_pending_count()})
        except Exception as exc:
            log_exception(exc, source="backend", context={"scope": "admin_community_pending_count"})
            return jsonify({"count": 0})

    @app.route("/api/admin/community/posts/<public_id>/moderate", methods=["POST"])
    @login_required
    @admin_required
    def admin_community_moderate_post(public_id):
        post = CommunityPost.query.filter_by(public_id=public_id).first()
        if post is None:
            return jsonify({"error": "Post not found."}), 404
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        value = data.get("value")
        try:
            if action == "status":
                cs.set_status(post, current_user, value)
            elif action in ("lock", "unlock"):
                cs.set_flag(post, "is_locked", action == "lock")
            elif action in ("pin", "unpin"):
                cs.set_flag(post, "is_pinned", action == "pin")
            elif action == "flair":
                cs.set_flair(post, value)
            elif action == "duplicate":
                target = cs.mark_duplicate(post, current_user, value)
                value = target.public_id
            elif action == "restore":
                cs.restore_post(post)
            elif action == "purge":
                if post.deleted_at is None:
                    raise cs.CommunityError("Delete the post before purging it.")
                title = post.title
                cs.hard_delete_post(post)
                log_admin_action(current_user.id, "community_purge_post", public_id, title[:200])
                return jsonify({"ok": True, "purged": True})
            else:
                return jsonify({"error": "Unknown action."}), 400
        except cs.CommunityError as exc:
            db.session.rollback()
            return _error(exc)
        log_admin_action(current_user.id, f"community_{action}", public_id, str(value)[:200] if value else None)
        return jsonify({"ok": True, "post": cs.serialize_post(post, current_user, detail=True)})

    @app.route("/api/admin/community/comments/<int:comment_id>/moderate", methods=["POST"])
    @login_required
    @admin_required
    def admin_community_moderate_comment(comment_id):
        comment = db.session.get(CommunityComment, comment_id)
        if comment is None:
            return jsonify({"error": "Comment not found."}), 404
        action = (request.get_json(silent=True) or {}).get("action")
        try:
            if action in ("official", "unofficial"):
                cs.set_official(comment, action == "official")
            elif action == "restore":
                cs.restore_comment(comment)
            elif action == "purge":
                target = f"{comment.post.public_id}#{comment.id}"
                db.session.delete(comment)
                post = comment.post
                db.session.flush()
                cs._recount_comments(post)
                db.session.commit()
                log_admin_action(current_user.id, "community_purge_comment", target)
                return jsonify({"ok": True, "purged": True})
            else:
                return jsonify({"error": "Unknown action."}), 400
        except cs.CommunityError as exc:
            db.session.rollback()
            return _error(exc)
        log_admin_action(current_user.id, f"community_comment_{action}", f"{comment.post.public_id}#{comment.id}")
        return jsonify({"ok": True, "comment": cs.serialize_comment(comment, current_user)})

    @app.route("/api/admin/community/users/mute", methods=["POST"])
    @login_required
    @admin_required
    def admin_community_mute():
        data = request.get_json(silent=True) or {}
        user = db.session.get(User, (data.get("email") or "").strip().lower())
        if user is None:
            return jsonify({"error": "User not found."}), 404
        if user.is_admin:
            return jsonify({"error": "Admins can't be muted."}), 400
        try:
            hours = int(data.get("hours") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "Hours must be a number."}), 400
        hours = max(0, min(hours, 24 * 365))
        cs.mute_user(user, hours)
        log_admin_action(current_user.id, "community_mute" if hours else "community_unmute", user.id,
                         f"{hours}h" if hours else None)
        return jsonify({"ok": True, "muted_until": user.community_muted_until.isoformat() + "Z" if hours else None})

    @app.route("/api/admin/community/reports/<int:report_id>/resolve", methods=["POST"])
    @login_required
    @admin_required
    def admin_community_resolve_report(report_id):
        report = db.session.get(CommunityReport, report_id)
        if report is None:
            return jsonify({"error": "Report not found."}), 404
        resolution = (request.get_json(silent=True) or {}).get("resolution") or "dismissed"
        cs.resolve_report(report, current_user, resolution)
        log_admin_action(current_user.id, "community_resolve_report", str(report_id), resolution)
        return jsonify({"ok": True})
