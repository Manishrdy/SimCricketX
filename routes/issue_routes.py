"""User-facing issue reporting routes.

Phase 1 of PLAN-IR-001: receives reports from the in-app floating widget,
persists them to the `issue_report` table, and enqueues a GitHub issue
creation via services.github_issue_queue.

Endpoints
---------
- POST /api/issues/report  (login-required, rate-limited 2/hour AND 5/day)
"""

from __future__ import annotations

import json
import secrets
import threading
from collections import deque
from datetime import datetime, timedelta

from flask import g, jsonify, request
from flask_login import current_user, login_required

from utils.exception_tracker import log_exception


# ---------------------------------------------------------------------------
# Allowed categories — kept here as the single source of truth
# ---------------------------------------------------------------------------

ALLOWED_CATEGORIES = {
    "bug",
    "gameplay-balance",
    "ui-ux",
    "performance",
    "feature-request",
    "data-accuracy",
    "other",
}

DEFAULT_CATEGORY = "other"

MAX_TITLE_LEN = 200
MAX_DESCRIPTION_LEN = 5000
MAX_PAGE_URL_LEN = 500
MAX_USER_AGENT_LEN = 500


# ---------------------------------------------------------------------------
# In-memory rate limiter (per user_email)
#
# We need a composite limit: 2/hour AND 5/day per user. flask_limiter is
# already loaded but mixing two windows for the same key on a single route
# is awkward; a tiny purpose-built limiter is clearer and gives accurate
# Retry-After headers in error responses.
# ---------------------------------------------------------------------------

_RATE_LIMIT_HOURLY = 2
_RATE_LIMIT_DAILY = 5
_HOURLY_WINDOW = timedelta(hours=1)
_DAILY_WINDOW = timedelta(days=1)

_rate_lock = threading.Lock()
_rate_buckets: dict[str, deque] = {}  # user_email -> deque[datetime]


def _check_rate_limit(user_email: str) -> tuple[bool, int | None, str | None]:
    """Return (allowed, retry_after_seconds, reason).

    Sliding window: stores timestamps for the last 24 hours per user, then
    counts how many fall inside each window. Pruning happens on every
    check, keeping memory bounded.
    """
    if not user_email:
        return False, None, "missing user"

    now = datetime.utcnow()
    day_cutoff = now - _DAILY_WINDOW
    hour_cutoff = now - _HOURLY_WINDOW

    with _rate_lock:
        bucket = _rate_buckets.setdefault(user_email, deque())

        # Prune anything older than the larger window.
        while bucket and bucket[0] < day_cutoff:
            bucket.popleft()

        in_hour = sum(1 for ts in bucket if ts >= hour_cutoff)
        in_day = len(bucket)

        if in_hour >= _RATE_LIMIT_HOURLY:
            # Retry-After until the oldest hourly slot expires.
            oldest_in_hour = next((ts for ts in bucket if ts >= hour_cutoff), None)
            retry_after = int(((oldest_in_hour + _HOURLY_WINDOW) - now).total_seconds()) if oldest_in_hour else 3600
            return False, max(retry_after, 1), f"hourly limit reached ({_RATE_LIMIT_HOURLY}/hour)"

        if in_day >= _RATE_LIMIT_DAILY:
            oldest = bucket[0]
            retry_after = int(((oldest + _DAILY_WINDOW) - now).total_seconds())
            return False, max(retry_after, 1), f"daily limit reached ({_RATE_LIMIT_DAILY}/day)"

        bucket.append(now)
        return True, None, None


def _reset_rate_limits_for_tests() -> None:
    """Test helper — wipe in-memory rate buckets."""
    with _rate_lock:
        _rate_buckets.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_public_id() -> str:
    """Short user-facing reference like ISS-A1B2C3."""
    return "ISS-" + secrets.token_hex(3).upper()


def _normalize_category(value: str | None) -> str:
    if not value:
        return DEFAULT_CATEGORY
    cleaned = value.strip().lower()
    return cleaned if cleaned in ALLOWED_CATEGORIES else DEFAULT_CATEGORY


def _trim(value: str | None, limit: int) -> str:
    if not value:
        return ""
    return str(value)[:limit].strip()


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_issue_routes(app, *, db):
    from database.models import IssueReport, ExceptionLog

    @app.route("/api/issues/report", methods=["POST"])
    @login_required
    def submit_issue_report():
        user_email = getattr(current_user, "id", None)
        if not user_email:
            return jsonify({"error": "Authentication required"}), 401

        # ---- Rate limit -------------------------------------------------
        allowed, retry_after, reason = _check_rate_limit(user_email)
        if not allowed:
            resp = jsonify({"error": reason or "Rate limit exceeded"})
            resp.status_code = 429
            if retry_after:
                resp.headers["Retry-After"] = str(retry_after)
            return resp

        # ---- Parse + validate body --------------------------------------
        if not request.is_json:
            return jsonify({"error": "Content-Type must be application/json"}), 400

        payload = request.get_json(silent=True) or {}

        title = _trim(payload.get("title"), MAX_TITLE_LEN)
        description = _trim(payload.get("description"), MAX_DESCRIPTION_LEN)

        if not title:
            return jsonify({"error": "title is required"}), 400
        if not description:
            return jsonify({"error": "description is required"}), 400

        category = _normalize_category(payload.get("category"))
        page_url = _trim(payload.get("page_url"), MAX_PAGE_URL_LEN)
        if not page_url:
            page_url = _trim(request.referrer, MAX_PAGE_URL_LEN)

        user_agent = _trim(request.headers.get("User-Agent"), MAX_USER_AGENT_LEN)

        # app version is exposed via the existing context processor and
        # also published into the response template; clients send it back
        # so reports filed *during* a deploy still attribute correctly.
        app_version = _trim(payload.get("app_version"), 50)
        if not app_version:
            try:
                with open(__import__("os").path.join(app.root_path, "version.txt"), encoding="utf-8") as fh:
                    app_version = fh.read().strip()[:50]
            except Exception:
                app_version = ""

        # ---- Capture session logs + linked exceptions -------------------
        session_logs = []
        try:
            from middleware import session_log_capture
            sid = getattr(g, "session_id", None)
            if sid:
                session_logs = session_log_capture.snapshot(sid)
        except Exception:
            log_exception(source="backend")

        # Linked exceptions: same user_email, last 30 minutes.
        linked_ids: list[int] = []
        try:
            cutoff = datetime.utcnow() - timedelta(minutes=30)
            recent = (
                ExceptionLog.query
                .filter(ExceptionLog.user_email == user_email)
                .filter(ExceptionLog.timestamp >= cutoff)
                .order_by(ExceptionLog.timestamp.desc())
                .limit(20)
                .all()
            )
            linked_ids = [row.id for row in recent]
        except Exception:
            log_exception(source="backend")

        # Explicit exception_log_id from the 500 crash page prefill —
        # guaranteed to be the right row even if the 30-minute auto-window
        # would have missed it. Merged in, deduped, placed first.
        try:
            explicit_exc_id = payload.get("exception_log_id")
            if explicit_exc_id is not None:
                explicit_exc_id = int(explicit_exc_id)
                if explicit_exc_id > 0:
                    # Verify the id actually exists before storing it.
                    exists = db.session.get(ExceptionLog, explicit_exc_id)
                    if exists is not None:
                        if explicit_exc_id in linked_ids:
                            linked_ids.remove(explicit_exc_id)
                        linked_ids.insert(0, explicit_exc_id)
        except (TypeError, ValueError):
            pass
        except Exception:
            log_exception(source="backend")

        # ---- Persist ----------------------------------------------------
        try:
            row = IssueReport(
                public_id=_generate_public_id(),
                user_email=user_email,
                category=category,
                title=title,
                description=description,
                page_url=page_url or None,
                user_agent=user_agent or None,
                app_version=app_version or None,
                session_logs_json=json.dumps(session_logs, default=str) if session_logs else None,
                linked_exception_log_ids=json.dumps(linked_ids) if linked_ids else None,
                github_sync_status="pending",
                status="new",
            )
            db.session.add(row)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="backend", context={"scope": "submit_issue_report"})
            return jsonify({"error": "Failed to save report"}), 500

        # ---- Enqueue background GitHub creation -------------------------
        try:
            from services import github_issue_queue
            github_issue_queue.enqueue_issue_report(row.id)
        except Exception:
            log_exception(source="backend")

        return jsonify({
            "status": "accepted",
            "public_id": row.public_id,
            "github_sync_status": row.github_sync_status,
        }), 202
