"""Community board: user pages, JSON API and image serving.

Registered-users only. All rules live in services/community_service.py.
"""

from __future__ import annotations

import re
from datetime import datetime

from markupsafe import Markup, escape
from flask import abort, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from database import db
from database.models import CommunityComment, CommunityImage, CommunityPost
from services import community_media as media, community_service as cs
from services.community_media import ImageRejected
from utils.exception_tracker import log_exception


def _user_key():
    return f"user:{current_user.get_id()}" if current_user.is_authenticated else request.remote_addr


def _error(exc: cs.CommunityError):
    return jsonify({"error": exc.message, "code": exc.code, "fields": exc.fields}), exc.status


def _json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


_URL_RE = re.compile(r"https?://[^\s<>\"']+[^\s<>\"'.,;:!?)\]]")


def linkify(text) -> Markup:
    """Escape user text, then turn bare http(s) URLs into safe links.
    Line breaks are preserved by CSS (white-space: pre-wrap)."""
    escaped = str(escape(text or ""))
    return Markup(_URL_RE.sub(
        lambda m: f'<a href="{m.group(0)}" rel="nofollow ugc noopener noreferrer" target="_blank">{m.group(0)}</a>',
        escaped))


def time_ago(iso) -> str:
    if not iso:
        return ""
    try:
        then = datetime.fromisoformat(str(iso).rstrip("Z"))
    except ValueError:
        return ""
    secs = max(0, int((datetime.utcnow() - then).total_seconds()))
    for unit, size in (("y", 31536000), ("mo", 2592000), ("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return "just now"


def register_community_routes(app, *, db=db, limiter=None):
    app.jinja_env.filters["cm_linkify"] = linkify
    app.jinja_env.filters["cm_ago"] = time_ago

    def limit(rule):
        if limiter is None:
            return lambda f: f
        return limiter.limit(rule, key_func=_user_key, methods=["POST", "PATCH", "DELETE"])

    # ── Pages ────────────────────────────────────────────────────────────────

    @app.route("/community")
    @login_required
    def community_index():
        args = request.args
        filters = {
            "flair": args.get("flair") or None,
            "status": args.get("status") or None,
            "sort": args.get("sort") or "active",
            "mine": args.get("mine") == "1",
            "q": (args.get("q") or "").strip()[:100] or None,
        }
        pinned, posts, next_cursor = cs.list_posts(current_user, cursor=args.get("cursor"), **filters)
        return render_template(
            "community/index.html",
            pinned=[cs.serialize_post(p, current_user) for p in pinned],
            posts=[cs.serialize_post(p, current_user) for p in posts],
            next_cursor=next_cursor, filters=filters,
            flairs=cs.FLAIRS, statuses=cs.STATUSES,
            block=cs.posting_block_reason(current_user),
        )

    @app.route("/community/new")
    @login_required
    def community_new():
        return render_template(
            "community/new.html", post=None,
            flairs={k: v for k, v in cs.FLAIRS.items() if not v.get("admin_only") or current_user.is_admin},
            formats=cs.BUG_FORMATS, limits=_limits(),
            prefill={"flair": request.args.get("flair") or "", "page_url": (request.args.get("from") or "")[:500]},
            block=cs.posting_block_reason(current_user), new_account=cs.new_account_block(current_user),
        )

    @app.route("/community/p/<public_id>")
    @login_required
    def community_post(public_id):
        try:
            post = cs.get_visible_post(public_id, current_user)
        except cs.CommunityError:
            abort(404)
        threads = [
            (cs.serialize_comment(top, current_user), [cs.serialize_comment(r, current_user) for r in kids])
            for top, kids in cs.visible_comments(post, current_user)
        ]
        similar = []
        if post.status == "open" and post.visibility == "public":
            similar = [cs.serialize_post(p, current_user) for p in cs.similar_posts(current_user, post.title, 4, post.id)]
        return render_template(
            "community/post.html",
            post=cs.serialize_post(post, current_user, detail=True), threads=threads, similar=similar,
            statuses=cs.STATUSES, flairs=cs.FLAIRS, report_reasons=cs.REPORT_REASONS,
            block=cs.posting_block_reason(current_user), new_account=cs.new_account_block(current_user),
            comment_max=cs.COMMENT_MAX,
        )

    @app.route("/community/p/<public_id>/edit")
    @login_required
    def community_edit(public_id):
        try:
            post = cs.get_visible_post(public_id, current_user)
        except cs.CommunityError:
            abort(404)
        if not cs.can_edit_post(post, current_user):
            return redirect(url_for("community_post", public_id=public_id))
        return render_template(
            "community/new.html", post=cs.serialize_post(post, current_user, detail=True),
            flairs={k: v for k, v in cs.FLAIRS.items() if not v.get("admin_only") or current_user.is_admin},
            formats=cs.BUG_FORMATS, limits=_limits(), prefill={},
            block=None if current_user.is_admin else cs.posting_block_reason(current_user), new_account=None,
        )

    @app.route("/community/notifications")
    @login_required
    def community_notifications_page():
        notes = [cs.serialize_notification(n) for n in cs.recent_notifications(current_user, 100)]
        cs.mark_notifications_read(current_user)
        return render_template("community/notifications.html", notes=notes)

    def _limits():
        return {"title": [cs.TITLE_MIN, cs.TITLE_MAX], "body": [cs.BODY_MIN, cs.BODY_MAX],
                "steps": [cs.STEPS_MIN, cs.STEPS_MAX], "step": [cs.STEP_MIN, cs.STEP_MAX],
                "expect": [cs.EXPECT_MIN, cs.EXPECT_MAX], "images": cs.MAX_IMAGES_PER_POST,
                "image_bytes": media.MAX_UPLOAD_BYTES}

    # ── Posts API ────────────────────────────────────────────────────────────

    @app.route("/api/community/posts", methods=["POST"])
    @login_required
    @limit("5 per hour")
    def community_api_create():
        data = _json_body()
        try:
            post = cs.create_post(current_user, data, request_meta={
                "page_url": data.get("page_url"), "app_version": data.get("app_version"),
                "user_agent": request.headers.get("User-Agent")})
        except cs.CommunityError as exc:
            db.session.rollback()
            return _error(exc)
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="backend", context={"scope": "community_create"})
            return jsonify({"error": "Could not create the post."}), 500
        return jsonify({"post": cs.serialize_post(post, current_user),
                        "url": url_for("community_post", public_id=post.public_id)}), 201

    @app.route("/api/community/posts/<public_id>", methods=["PATCH"])
    @login_required
    @limit("30 per hour")
    def community_api_update(public_id):
        try:
            post = cs.get_visible_post(public_id, current_user)
            cs.update_post(post, current_user, _json_body())
            if current_user.is_admin and post.author_id != current_user.id:
                _audit("community_edit_post", post.public_id)
        except cs.CommunityError as exc:
            db.session.rollback()
            return _error(exc)
        return jsonify({"post": cs.serialize_post(post, current_user),
                        "url": url_for("community_post", public_id=post.public_id)})

    @app.route("/api/community/posts/<public_id>", methods=["DELETE"])
    @login_required
    def community_api_delete(public_id):
        try:
            post = cs.get_visible_post(public_id, current_user)
            reason = (_json_body().get("reason") or "").strip()
            cs.soft_delete_post(post, current_user, reason)
            if current_user.is_admin and post.author_id != current_user.id:
                _audit("community_delete_post", post.public_id, reason)
        except cs.CommunityError as exc:
            return _error(exc)
        return jsonify({"ok": True, "url": url_for("community_index")})

    @app.route("/api/community/posts/<public_id>/vote", methods=["POST"])
    @login_required
    @limit("60 per minute")
    def community_api_vote(public_id):
        try:
            post = cs.get_visible_post(public_id, current_user)
            voted, count = cs.toggle_vote(post, current_user)
        except cs.CommunityError as exc:
            db.session.rollback()
            return _error(exc)
        return jsonify({"voted": voted, "vote_count": count})

    @app.route("/api/community/similar")
    @login_required
    def community_api_similar():
        q = (request.args.get("q") or "").strip()[:120]
        if len(q) < 6:
            return jsonify({"posts": []})
        rows = cs.similar_posts(current_user, q, limit=5)
        return jsonify({"posts": [
            {**cs.serialize_post(p, current_user), "url": url_for("community_post", public_id=p.public_id)}
            for p in rows]})

    # ── Comments API ─────────────────────────────────────────────────────────

    @app.route("/api/community/posts/<public_id>/comments", methods=["POST"])
    @login_required
    @limit("30 per hour")
    def community_api_comment(public_id):
        data = _json_body()
        try:
            post = cs.get_visible_post(public_id, current_user)
            comment = cs.add_comment(post, current_user, data.get("body"), data.get("parent_id"))
        except cs.CommunityError as exc:
            db.session.rollback()
            return _error(exc)
        except (TypeError, ValueError):
            return jsonify({"error": "Bad request."}), 400
        return jsonify({"comment": cs.serialize_comment(comment, current_user)}), 201

    def _comment_or_404(comment_id) -> CommunityComment:
        comment = db.session.get(CommunityComment, comment_id)
        if comment is None or not cs.can_view_post(comment.post, current_user):
            raise cs.CommunityError("Comment not found.", code="not_found", status=404)
        return comment

    @app.route("/api/community/comments/<int:comment_id>", methods=["PATCH"])
    @login_required
    @limit("30 per hour")
    def community_api_edit_comment(comment_id):
        try:
            comment = _comment_or_404(comment_id)
            cs.edit_comment(comment, current_user, _json_body().get("body"))
            if current_user.is_admin and comment.author_id != current_user.id:
                _audit("community_edit_comment", f"{comment.post.public_id}#{comment.id}")
        except cs.CommunityError as exc:
            db.session.rollback()
            return _error(exc)
        return jsonify({"comment": cs.serialize_comment(comment, current_user)})

    @app.route("/api/community/comments/<int:comment_id>", methods=["DELETE"])
    @login_required
    def community_api_delete_comment(comment_id):
        try:
            comment = _comment_or_404(comment_id)
            reason = (_json_body().get("reason") or "").strip()
            cs.soft_delete_comment(comment, current_user, reason)
            if current_user.is_admin and comment.author_id != current_user.id:
                _audit("community_delete_comment", f"{comment.post.public_id}#{comment.id}", reason)
        except cs.CommunityError as exc:
            return _error(exc)
        return jsonify({"ok": True})

    # ── Reports ──────────────────────────────────────────────────────────────

    @app.route("/api/community/reports", methods=["POST"])
    @login_required
    @limit("20 per hour")
    def community_api_report():
        data = _json_body()
        try:
            post = cs.get_visible_post(data.get("post_id") or "", current_user)
            comment = None
            if data.get("comment_id"):
                comment = _comment_or_404(int(data["comment_id"]))
                if comment.post_id != post.id:
                    raise cs.CommunityError("Comment not found.", status=404)
            cs.create_report(current_user, post, comment, data.get("reason") or "", data.get("details") or "")
        except cs.CommunityError as exc:
            return _error(exc)
        except (TypeError, ValueError):
            return jsonify({"error": "Bad request."}), 400
        return jsonify({"ok": True})

    # ── Notifications ────────────────────────────────────────────────────────

    @app.route("/api/community/notifications")
    @login_required
    def community_api_notifications():
        return jsonify({"unread": cs.unread_count(current_user),
                        "items": [cs.serialize_notification(n) for n in cs.recent_notifications(current_user, 15)]})

    @app.route("/api/community/notifications/read", methods=["POST"])
    @login_required
    def community_api_notifications_read():
        cs.mark_notifications_read(current_user, _json_body().get("ids"))
        return jsonify({"ok": True, "unread": cs.unread_count(current_user)})

    # ── Images ───────────────────────────────────────────────────────────────

    @app.route("/api/community/images", methods=["POST"])
    @login_required
    @limit("30 per hour")
    def community_api_upload():
        if (request.content_length or 0) > media.MAX_UPLOAD_BYTES + 64 * 1024:
            return jsonify({"error": "Images must be 10 MB or smaller."}), 413
        try:
            cs.require_can_write(current_user)
        except cs.CommunityError as exc:
            return _error(exc)
        upload = request.files.get("image")
        if upload is None:
            return jsonify({"error": "No image received."}), 400
        data = upload.stream.read(media.MAX_UPLOAD_BYTES + 1)
        try:
            img = media.store_upload(current_user, data)
        except ImageRejected as exc:
            return jsonify({"error": exc.message}), exc.status
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="backend", context={"scope": "community_upload"})
            return jsonify({"error": "Upload failed."}), 500
        return jsonify({"image": cs.image_urls(img), "bytes": img.bytes}), 201

    @app.route("/api/community/images/<public_id>", methods=["DELETE"])
    @login_required
    def community_api_delete_image(public_id):
        img = CommunityImage.query.filter_by(public_id=public_id).first()
        if img is None or (img.uploader_id != current_user.id and not current_user.is_admin):
            return jsonify({"error": "Not found."}), 404
        if img.post_id is not None:
            post = db.session.get(CommunityPost, img.post_id)
            if not cs.can_edit_post(post, current_user):
                return jsonify({"error": "Not allowed."}), 403
            img.post_id = None
        media.delete_image_files(img)
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/community/media/<public_id>/<variant>.webp")
    @login_required
    def community_media(public_id, variant):
        if variant not in ("full", "thumb"):
            abort(404)
        img = CommunityImage.query.filter_by(public_id=public_id).first()
        if img is None or img.purged_at is not None:
            abort(404)
        if img.post_id is None:
            # Unattached upload: only its uploader (composer preview) or an admin.
            if img.uploader_id != current_user.id and not current_user.is_admin:
                abort(404)
        elif not cs.can_view_post(img.post, current_user):
            abort(404)
        try:
            path = media.file_path_for(img, variant)
        except ValueError:
            abort(404)
        resp = send_file(path, mimetype="image/webp", conditional=True, max_age=31536000)
        resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    def _audit(action, target, details=None):
        from auth.user_auth import log_admin_action
        log_admin_action(current_user.id, action, target, details)
