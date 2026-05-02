"""Admin exception tracking routes.

Manual user reports have moved to the in-app Support console. This module now
tracks only automatic ExceptionLog rows and their GitHub sync state.
"""

from __future__ import annotations

import json
from datetime import datetime

from flask import jsonify, render_template, request
from flask_login import login_required

from auth.decorators import admin_required
from utils.exception_tracker import log_exception


DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200


def register_admin_issue_routes(app, *, db):
    from database.models import ExceptionLog

    def _coerce_int(value, default, *, lo=None, hi=None):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        if lo is not None and n < lo:
            n = lo
        if hi is not None and n > hi:
            n = hi
        return n

    def _build_exception_query(*, severity=None, source=None, resolved=None, search=None):
        q = ExceptionLog.query
        if severity:
            q = q.filter(ExceptionLog.severity == severity)
        if source:
            q = q.filter(ExceptionLog.source == source)
        if resolved is True:
            q = q.filter(ExceptionLog.resolved.is_(True))
        elif resolved is False:
            q = q.filter(ExceptionLog.resolved.is_(False))
        if search:
            like = f"%{search}%"
            q = q.filter(
                db.or_(
                    ExceptionLog.exception_type.ilike(like),
                    ExceptionLog.exception_message.ilike(like),
                    ExceptionLog.user_email.ilike(like),
                )
            )
        return q.order_by(ExceptionLog.timestamp.desc())

    def _compute_stats():
        total = db.session.query(db.func.count(ExceptionLog.id)).scalar() or 0
        unresolved = db.session.query(db.func.count(ExceptionLog.id)).filter(
            ExceptionLog.resolved.is_(False)
        ).scalar() or 0
        failed_sync = db.session.query(db.func.count(ExceptionLog.id)).filter(
            ExceptionLog.github_sync_status == "failed"
        ).scalar() or 0
        return {
            "exceptions": {
                "total": int(total),
                "unresolved": int(unresolved),
                "resolved": int(total - unresolved),
                "failed_sync": int(failed_sync),
            },
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }

    @app.route("/admin/issues")
    @login_required
    @admin_required
    def admin_issues():
        page = _coerce_int(request.args.get("page"), 1, lo=1)
        page_size = _coerce_int(request.args.get("page_size"), DEFAULT_PAGE_SIZE, lo=1, hi=MAX_PAGE_SIZE)
        search = (request.args.get("q") or "").strip() or None
        severity = (request.args.get("severity") or "").strip() or None
        source = (request.args.get("source") or "").strip() or None
        resolved_filter = None
        resolved_arg = (request.args.get("resolved") or "").strip().lower()
        if resolved_arg == "yes":
            resolved_filter = True
        elif resolved_arg == "no":
            resolved_filter = False

        try:
            stats = _compute_stats()
        except Exception:
            log_exception(source="backend", context={"scope": "admin_exception_stats"})
            stats = {"exceptions": {"total": 0, "unresolved": 0, "resolved": 0, "failed_sync": 0}, "generated_at": None}

        exception_rows = []
        total_rows = 0
        try:
            q = _build_exception_query(severity=severity, source=source, resolved=resolved_filter, search=search)
            total_rows = q.count()
            exception_rows = q.offset((page - 1) * page_size).limit(page_size).all()
        except Exception:
            log_exception(source="backend", context={"scope": "admin_exception_listing"})

        page_count = max(1, (total_rows + page_size - 1) // page_size)
        return render_template(
            "admin/issues.html",
            stats=stats,
            exception_rows=exception_rows,
            page=page,
            page_size=page_size,
            page_count=page_count,
            total_rows=total_rows,
            filters={
                "q": search or "",
                "severity": severity or "",
                "source": source or "",
                "resolved": resolved_arg,
            },
        )

    @app.route("/admin/issues/stats")
    @login_required
    @admin_required
    def admin_issues_stats():
        try:
            return jsonify(_compute_stats())
        except Exception as exc:
            log_exception(exc, source="backend", context={"scope": "admin_exception_stats_json"})
            return jsonify({"error": "stats unavailable"}), 500

    @app.route("/admin/issues/exceptions/<int:exc_id>")
    @login_required
    @admin_required
    def admin_exception_detail(exc_id):
        row = ExceptionLog.query.get(exc_id)
        if row is None:
            return render_template("404.html"), 404

        try:
            context = json.loads(row.context_json) if row.context_json else {}
        except Exception:
            context = {}

        return render_template(
            "admin/exception_detail.html",
            row=row,
            context=context,
        )

    @app.route("/admin/issues/exceptions/<int:row_id>/retry", methods=["POST"])
    @login_required
    @admin_required
    def admin_exception_retry(row_id):
        row = ExceptionLog.query.get(row_id)
        if row is None:
            return jsonify({"error": "not found"}), 404
        if row.github_issue_number:
            return jsonify({"status": "already_synced", "github_issue_number": row.github_issue_number})
        try:
            row.github_sync_status = "pending"
            row.github_sync_error = None
            db.session.commit()
            from services import github_issue_queue
            github_issue_queue.enqueue_exception(row.id)
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="backend", context={"scope": "admin_exception_retry"})
            return jsonify({"error": "retry failed"}), 500
        return jsonify({"status": "queued", "id": row.id})

    @app.route("/admin/issues/sync", methods=["POST"])
    @login_required
    @admin_required
    def admin_issues_sync():
        from services import github_issues

        if not github_issues.is_enabled():
            return jsonify({"error": "GitHub integration is not configured"}), 400

        synced = {"exceptions": 0, "errors": 0}
        try:
            rows = ExceptionLog.query.filter(ExceptionLog.github_issue_number.isnot(None)).all()
            for row in rows:
                try:
                    payload = github_issues.get_issue(row.github_issue_number)
                    if not payload:
                        synced["errors"] += 1
                        continue
                    state = (payload.get("state") or "").lower()
                    if state == "closed":
                        row.resolved = True
                        if not row.resolved_at:
                            row.resolved_at = datetime.utcnow()
                            row.resolved_by = "github-sync"
                    elif state == "open":
                        row.resolved = False
                    row.github_last_synced_at = datetime.utcnow()
                    synced["exceptions"] += 1
                except Exception:
                    synced["errors"] += 1
                    log_exception(source="backend", context={"scope": "admin_sync_exception", "id": row.id})
            db.session.commit()
        except Exception:
            db.session.rollback()
            log_exception(source="backend", context={"scope": "admin_sync_exceptions_loop"})
            return jsonify({"error": "sync failed"}), 500

        return jsonify({"status": "ok", "synced": synced})
