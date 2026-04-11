"""
Centralised exception logger.

Usage inside any except block:

    from utils.exception_tracker import log_exception

    except Exception as e:
        log_exception(e)            # records to exception_log table

GitHub issue creation runs asynchronously via services.github_issue_queue,
so the call site never blocks on a network round-trip.
"""

import sys
import json
import hashlib
import traceback as tb_module
from datetime import datetime
# Re-exported for backwards compatibility with existing tests that monkeypatch
# `exception_tracker.urlrequest.urlopen` / `exception_tracker.urlerror`.
from urllib import request as urlrequest  # noqa: F401
from urllib import error as urlerror  # noqa: F401

from flask import has_request_context, request, g
from flask_login import current_user

from database import db
from database.models import ExceptionLog


def _build_fingerprint(
    *,
    exception_type: str,
    exception_message: str,
    module: str | None,
    function: str | None,
    line_number: int | None,
    source: str,
) -> str:
    payload = {
        "type": exception_type or "",
        "message": (exception_message or "")[:1000],
        "module": module or "",
        "function": function or "",
        "line": line_number or 0,
        "source": source or "backend",
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def log_exception(
    exc: Exception | None = None,
    *,
    severity: str = "error",
    source: str = "backend",
    context: dict | None = None,
    request_id: str | None = None,
    handled: bool = True,
) -> int | None:
    """Record an exception to the exception_log table.

    Safe to call from anywhere — inside or outside a request context.
    Never raises; if the recording itself fails it is silently swallowed
    so the original error handling is not disrupted.

    Returns the ExceptionLog row id on success (or the id of the
    existing fingerprint-matched row on dedup), or None on failure.
    The 500 error handler uses this to link the crash page to the
    exact log entry so user reports carry the reference automatically.
    """
    try:
        if exc is None:
            exc_type_obj, exc_val, exc_tb = sys.exc_info()
        else:
            exc_type_obj = type(exc)
            exc_val = exc
            exc_tb = exc.__traceback__

        exc_type_name = exc_type_obj.__name__ if exc_type_obj else 'Unknown'
        exc_message = str(exc_val) if exc_val else ''
        tb_text = ''.join(tb_module.format_exception(exc_type_obj, exc_val, exc_tb)) if exc_tb else None

        # Extract source location from the deepest traceback frame
        module_name = None
        func_name = None
        lineno = None
        fname = None
        if exc_tb:
            frame = exc_tb
            while frame.tb_next:
                frame = frame.tb_next
            lineno = frame.tb_lineno
            func_name = frame.tb_frame.f_code.co_name
            fname = frame.tb_frame.f_code.co_filename
            module_name = frame.tb_frame.f_globals.get('__name__', '')

        # Logged-in user email (if inside a request)
        user_email = None
        if has_request_context():
            try:
                if current_user and current_user.is_authenticated:
                    user_email = current_user.id  # id is the email string
            except Exception:
                pass

        # Attach request-level metadata when available. Caller context wins.
        merged_context = {}
        if has_request_context():
            try:
                merged_context.update({
                    "path": request.path,
                    "method": request.method,
                    "endpoint": request.endpoint,
                    "remote_addr": request.remote_addr,
                })
            except Exception:
                pass

        if context:
            merged_context.update(context)

        resolved_request_id = request_id
        if not resolved_request_id and has_request_context():
            try:
                resolved_request_id = getattr(g, "request_id", None) or request.headers.get("X-Request-ID")
            except Exception:
                resolved_request_id = None

        context_json = None
        if merged_context:
            try:
                context_json = json.dumps(merged_context, default=str)
            except Exception:
                context_json = str(merged_context)
        now = datetime.utcnow()
        normalized_source = (source or "backend")[:30]
        fingerprint = _build_fingerprint(
            exception_type=exc_type_name,
            exception_message=exc_message,
            module=module_name,
            function=func_name,
            line_number=lineno,
            source=normalized_source,
        )

        # Idempotency: one canonical DB row per fingerprint.
        # Repeated occurrences increment counters and update last_seen fields.
        entry = ExceptionLog.query.filter_by(fingerprint=fingerprint).first()
        if entry:
            entry.occurrence_count = int(entry.occurrence_count or 1) + 1
            entry.last_seen_at = now
            entry.timestamp = now
            if tb_text:
                entry.traceback = tb_text
            if context_json:
                entry.context_json = context_json
            if resolved_request_id:
                entry.request_id = resolved_request_id[:64]
            if user_email:
                entry.user_email = user_email
            db.session.commit()
            return entry.id

        entry = ExceptionLog(
            exception_type=exc_type_name,
            exception_message=exc_message[:65535] if exc_message else '',
            traceback=tb_text,
            module=module_name,
            function=func_name,
            line_number=lineno,
            filename=fname,
            user_email=user_email,
            severity=(severity or "error")[:10],
            source=normalized_source,
            context_json=context_json,
            request_id=resolved_request_id[:64] if resolved_request_id else None,
            handled=bool(handled),
            fingerprint=fingerprint,
            occurrence_count=1,
            first_seen_at=now,
            last_seen_at=now,
            timestamp=now,
            github_sync_status="pending",
        )
        db.session.add(entry)
        db.session.commit()

        # Hand the GitHub issue creation off to the background queue so we
        # never block the caller on a network round-trip. The worker will
        # update github_issue_number / github_sync_status when it finishes.
        created_id = entry.id
        try:
            from services import github_issue_queue
            github_issue_queue.enqueue_exception(entry.id)
        except Exception:
            # Never let queue failures bubble up — caller's error handling
            # must remain intact.
            db.session.rollback()
        return created_id
    except Exception:
        db.session.rollback()
        return None
