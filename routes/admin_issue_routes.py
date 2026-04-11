"""Admin-facing issue tracking routes — PLAN-IR-001 Phase 2.

Provides a unified surface over `IssueReport` (user-submitted) and
`ExceptionLog` (auto-captured) so an operator can triage everything in
one place. GitHub remains the source of truth for status — local rows
are read-only for status fields and can only be retried / re-synced.

Endpoints
---------
GET  /admin/issues                          — dashboard + list (kind toggle)
GET  /admin/issues/stats                    — dashboard JSON
GET  /admin/issues/reports/<public_id>      — IssueReport detail
GET  /admin/issues/exceptions/<int:id>      — ExceptionLog detail
POST /admin/issues/reports/<id>/retry       — retry failed GitHub sync
POST /admin/issues/exceptions/<id>/retry    — retry failed GitHub sync
POST /admin/issues/sync                     — manual GitHub reconcile
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from flask import jsonify, render_template, request
from flask_login import current_user, login_required

from auth.decorators import admin_required
from utils.exception_tracker import log_exception


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_KINDS = {"reports", "exceptions", "combined"}
DEFAULT_KIND = "reports"
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200

# Status buckets that the dashboard cards count.
STATUS_LABELS = {
    "new": "New",
    "open": "Open",
    "in_progress": "In Progress",
    "resolved": "Resolved",
    "closed": "Closed",
    "deferred": "Deferred",
    "wont_fix": "Won't Fix",
}

# Maps the local IssueReport.status to a small set of dashboard buckets.
DASHBOARD_BUCKETS = ["new", "open", "in_progress", "resolved", "closed", "deferred"]


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_admin_issue_routes(app, *, db):
    from database.models import IssueReport, ExceptionLog

    # ----- Helpers ----------------------------------------------------------

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

    def _normalize_kind(value):
        v = (value or "").strip().lower()
        return v if v in VALID_KINDS else DEFAULT_KIND

    def _build_report_query(*, status=None, category=None, search=None):
        q = IssueReport.query
        if status:
            q = q.filter(IssueReport.status == status)
        if category:
            q = q.filter(IssueReport.category == category)
        if search:
            like = f"%{search}%"
            q = q.filter(
                db.or_(
                    IssueReport.title.ilike(like),
                    IssueReport.description.ilike(like),
                    IssueReport.user_email.ilike(like),
                    IssueReport.public_id.ilike(like),
                )
            )
        return q.order_by(IssueReport.created_at.desc())

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
        """Aggregate counts for the dashboard cards."""
        report_total = db.session.query(db.func.count(IssueReport.id)).scalar() or 0
        report_status = dict(
            db.session.query(IssueReport.status, db.func.count(IssueReport.id))
            .group_by(IssueReport.status)
            .all()
        )
        report_status_counts = {bucket: int(report_status.get(bucket, 0)) for bucket in DASHBOARD_BUCKETS}
        report_status_counts["wont_fix"] = int(report_status.get("wont_fix", 0))

        exc_total = db.session.query(db.func.count(ExceptionLog.id)).scalar() or 0
        exc_unresolved = db.session.query(db.func.count(ExceptionLog.id)).filter(
            ExceptionLog.resolved.is_(False)
        ).scalar() or 0
        exc_failed_sync = db.session.query(db.func.count(ExceptionLog.id)).filter(
            ExceptionLog.github_sync_status == "failed"
        ).scalar() or 0
        report_failed_sync = db.session.query(db.func.count(IssueReport.id)).filter(
            IssueReport.github_sync_status == "failed"
        ).scalar() or 0

        return {
            "reports": {
                "total": int(report_total),
                "by_status": report_status_counts,
                "failed_sync": int(report_failed_sync),
            },
            "exceptions": {
                "total": int(exc_total),
                "unresolved": int(exc_unresolved),
                "failed_sync": int(exc_failed_sync),
            },
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }

    def _serialize_report(row):
        return {
            "kind": "report",
            "id": row.id,
            "public_id": row.public_id,
            "title": row.title,
            "category": row.category,
            "status": row.status,
            "user_email": row.user_email,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "github_issue_number": row.github_issue_number,
            "github_issue_url": row.github_issue_url,
            "github_sync_status": row.github_sync_status,
        }

    def _serialize_exception(row):
        return {
            "kind": "exception",
            "id": row.id,
            "type": row.exception_type,
            "message": (row.exception_message or "")[:200],
            "severity": row.severity,
            "source": row.source,
            "user_email": row.user_email,
            "timestamp": row.timestamp.isoformat() if row.timestamp else None,
            "occurrence_count": row.occurrence_count,
            "github_issue_number": row.github_issue_number,
            "github_issue_url": row.github_issue_url,
            "github_sync_status": row.github_sync_status,
            "resolved": bool(row.resolved),
        }

    # ----- /admin/issues (list page) ---------------------------------------

    @app.route("/admin/issues")
    @login_required
    @admin_required
    def admin_issues():
        kind = _normalize_kind(request.args.get("kind"))
        page = _coerce_int(request.args.get("page"), 1, lo=1)
        page_size = _coerce_int(request.args.get("page_size"), DEFAULT_PAGE_SIZE, lo=1, hi=MAX_PAGE_SIZE)
        search = (request.args.get("q") or "").strip() or None

        # Filter values are kind-specific
        status = (request.args.get("status") or "").strip() or None
        category = (request.args.get("category") or "").strip() or None
        severity = (request.args.get("severity") or "").strip() or None
        source = (request.args.get("source") or "").strip() or None

        try:
            stats = _compute_stats()
        except Exception:
            log_exception(source="backend", context={"scope": "admin_issues_stats"})
            stats = {
                "reports": {"total": 0, "by_status": {}, "failed_sync": 0},
                "exceptions": {"total": 0, "unresolved": 0, "failed_sync": 0},
                "generated_at": None,
            }

        report_rows: list = []
        exception_rows: list = []
        combined_rows: list = []
        total_rows = 0

        try:
            if kind == "reports":
                q = _build_report_query(status=status, category=category, search=search)
                total_rows = q.count()
                report_rows = q.offset((page - 1) * page_size).limit(page_size).all()
            elif kind == "exceptions":
                resolved_filter = None
                resolved_arg = (request.args.get("resolved") or "").strip().lower()
                if resolved_arg == "yes":
                    resolved_filter = True
                elif resolved_arg == "no":
                    resolved_filter = False
                q = _build_exception_query(severity=severity, source=source, resolved=resolved_filter, search=search)
                total_rows = q.count()
                exception_rows = q.offset((page - 1) * page_size).limit(page_size).all()
            else:  # combined timeline
                report_q = _build_report_query(search=search).limit(page_size * 2)
                exc_q = _build_exception_query(search=search).limit(page_size * 2)
                merged = []
                for r in report_q.all():
                    item = _serialize_report(r)
                    item["sort_ts"] = r.created_at or datetime.min
                    merged.append(item)
                for e in exc_q.all():
                    item = _serialize_exception(e)
                    item["sort_ts"] = e.timestamp or datetime.min
                    merged.append(item)
                merged.sort(key=lambda i: i["sort_ts"], reverse=True)
                total_rows = len(merged)
                start = (page - 1) * page_size
                combined_rows = merged[start:start + page_size]
                # Drop the helper key before rendering.
                for item in combined_rows:
                    item.pop("sort_ts", None)
        except Exception:
            log_exception(source="backend", context={"scope": "admin_issues_listing", "kind": kind})

        page_count = max(1, (total_rows + page_size - 1) // page_size)

        return render_template(
            "admin/issues.html",
            kind=kind,
            stats=stats,
            report_rows=report_rows,
            exception_rows=exception_rows,
            combined_rows=combined_rows,
            page=page,
            page_size=page_size,
            page_count=page_count,
            total_rows=total_rows,
            filters={
                "q": search or "",
                "status": status or "",
                "category": category or "",
                "severity": severity or "",
                "source": source or "",
                "resolved": (request.args.get("resolved") or "").strip(),
            },
            status_labels=STATUS_LABELS,
        )

    # ----- /admin/issues/stats (JSON) ---------------------------------------

    @app.route("/admin/issues/stats")
    @login_required
    @admin_required
    def admin_issues_stats():
        try:
            return jsonify(_compute_stats())
        except Exception as exc:
            log_exception(exc, source="backend", context={"scope": "admin_issues_stats_json"})
            return jsonify({"error": "stats unavailable"}), 500

    # ----- Detail views -----------------------------------------------------

    @app.route("/admin/issues/reports/<public_id>")
    @login_required
    @admin_required
    def admin_issue_report_detail(public_id):
        row = IssueReport.query.filter_by(public_id=public_id).first()
        if row is None:
            return render_template("404.html"), 404

        # Parse JSON blobs once for the template.
        try:
            session_logs = json.loads(row.session_logs_json) if row.session_logs_json else []
        except Exception:
            session_logs = []
        try:
            linked_ids = json.loads(row.linked_exception_log_ids) if row.linked_exception_log_ids else []
        except Exception:
            linked_ids = []

        linked_exceptions = []
        if linked_ids:
            linked_exceptions = (
                ExceptionLog.query
                .filter(ExceptionLog.id.in_(linked_ids))
                .order_by(ExceptionLog.timestamp.desc())
                .all()
            )

        return render_template(
            "admin/issue_report_detail.html",
            row=row,
            session_logs=session_logs,
            linked_exceptions=linked_exceptions,
        )

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

        # Pull any user reports that linked back to this exception in the
        # last 7 days — handy reverse lookup for triage.
        cutoff = datetime.utcnow() - timedelta(days=7)
        related_reports = []
        try:
            recent = (
                IssueReport.query
                .filter(IssueReport.created_at >= cutoff)
                .order_by(IssueReport.created_at.desc())
                .limit(200)
                .all()
            )
            for r in recent:
                if not r.linked_exception_log_ids:
                    continue
                try:
                    if exc_id in json.loads(r.linked_exception_log_ids):
                        related_reports.append(r)
                except Exception:
                    continue
        except Exception:
            log_exception(source="backend", context={"scope": "admin_exception_related_reports"})

        return render_template(
            "admin/exception_detail.html",
            row=row,
            context=context,
            related_reports=related_reports,
        )

    # ----- Retry GitHub sync (single row) -----------------------------------

    @app.route("/admin/issues/reports/<int:row_id>/notes", methods=["POST"])
    @login_required
    @admin_required
    def admin_issue_report_notes(row_id):
        """Save admin_notes on an IssueReport row. Local-only — never touches GitHub."""
        row = IssueReport.query.get(row_id)
        if row is None:
            return jsonify({"error": "not found"}), 404

        if not request.is_json:
            return jsonify({"error": "Content-Type must be application/json"}), 400
        payload = request.get_json(silent=True) or {}
        notes = payload.get("admin_notes")
        if notes is None:
            return jsonify({"error": "admin_notes is required"}), 400

        try:
            row.admin_notes = str(notes)[:10000]
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="backend", context={"scope": "admin_issue_report_notes"})
            return jsonify({"error": "save failed"}), 500
        return jsonify({"status": "saved", "id": row.id})

    @app.route("/admin/issues/reports/<int:row_id>/retry", methods=["POST"])
    @login_required
    @admin_required
    def admin_issue_report_retry(row_id):
        row = IssueReport.query.get(row_id)
        if row is None:
            return jsonify({"error": "not found"}), 404
        if row.github_issue_number:
            return jsonify({"status": "already_synced", "github_issue_number": row.github_issue_number})
        try:
            row.github_sync_status = "pending"
            row.github_sync_error = None
            db.session.commit()
            from services import github_issue_queue
            github_issue_queue.enqueue_issue_report(row.id)
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="backend", context={"scope": "admin_issue_report_retry"})
            return jsonify({"error": "retry failed"}), 500
        return jsonify({"status": "queued", "id": row.id})

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

    # ----- Manual sync (reconcile against GitHub) ---------------------------

    @app.route("/admin/issues/sync", methods=["POST"])
    @login_required
    @admin_required
    def admin_issues_sync():
        """Reconcile local rows against GitHub.

        Walks every IssueReport / ExceptionLog row that already has a
        github_issue_number and refreshes its status from the GitHub API.
        Closed issues become `closed`; reopened ones become `open`.
        """
        from services import github_issues

        if not github_issues.is_enabled():
            return jsonify({"error": "GitHub integration is not configured"}), 400

        synced = {"reports": 0, "exceptions": 0, "errors": 0}

        # IssueReport rows
        try:
            rows = IssueReport.query.filter(IssueReport.github_issue_number.isnot(None)).all()
            for row in rows:
                try:
                    payload = github_issues.get_issue(row.github_issue_number)
                    if not payload:
                        synced["errors"] += 1
                        continue
                    state = (payload.get("state") or "").lower()
                    labels = [(lbl.get("name") or "").lower() for lbl in payload.get("labels", []) if isinstance(lbl, dict)]
                    new_status = _map_github_state_to_status(state, labels)
                    if new_status:
                        row.status = new_status
                    row.github_last_synced_at = datetime.utcnow()
                    synced["reports"] += 1
                except Exception:
                    synced["errors"] += 1
                    log_exception(source="backend", context={"scope": "admin_sync_report", "id": row.id})
        except Exception:
            log_exception(source="backend", context={"scope": "admin_sync_reports_loop"})

        # ExceptionLog rows
        try:
            rows = ExceptionLog.query.filter(ExceptionLog.github_issue_number.isnot(None)).all()
            for row in rows:
                try:
                    payload = github_issues.get_issue(row.github_issue_number)
                    if not payload:
                        synced["errors"] += 1
                        continue
                    state = (payload.get("state") or "").lower()
                    labels = [(lbl.get("name") or "").lower() for lbl in payload.get("labels", []) if isinstance(lbl, dict)]
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
        except Exception:
            log_exception(source="backend", context={"scope": "admin_sync_exceptions_loop"})

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            log_exception(source="backend", context={"scope": "admin_sync_commit"})
            return jsonify({"error": "commit failed"}), 500

        return jsonify({"status": "ok", "synced": synced})


def _map_github_state_to_status(state: str, labels: list[str]) -> str | None:
    """Translate GitHub (state, labels) into a local IssueReport status.

    The label vocabulary is part of PLAN-IR-001 — admins apply
    `status:in-progress`, `status:resolved`, `status:deferred`,
    `status:wont-fix`, etc. on the issue and the webhook (Phase 3) or
    this manual sync mirrors them onto the local row.
    """
    label_map = {
        "status:triaged": "open",
        "status:in-progress": "in_progress",
        "status:resolved": "resolved",
        "status:deferred": "deferred",
        "status:wont-fix": "wont_fix",
        "status:duplicate": "closed",
    }
    for lbl in labels:
        if lbl in label_map:
            return label_map[lbl]
    if state == "closed":
        return "closed"
    if state == "open":
        return "open"
    return None
