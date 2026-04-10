"""
Centralised exception logger.

Usage inside any except block:

    from utils.exception_tracker import log_exception

    except Exception as e:
        log_exception(e)            # records to exception_log table
        # ... existing handling ...
"""

import sys
import json
import os
import hashlib
import traceback as tb_module
from datetime import datetime
from urllib import request as urlrequest
from urllib import error as urlerror

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


def _create_github_issue_for_exception(entry: ExceptionLog) -> tuple[int | None, str | None]:
    """Best-effort GitHub issue creation for a logged exception."""
    enabled = os.getenv("GITHUB_ISSUE_ON_EXCEPTION_ENABLED", "").strip().lower() in {"1", "true", "yes"}
    if not enabled:
        return None, None

    token = os.getenv("GITHUB_TOKEN", "").strip()
    repository = os.getenv("GITHUB_REPOSITORY", "").strip()  # owner/repo
    if not token or not repository:
        return None, None

    issue_title_prefix = os.getenv("GITHUB_ISSUE_TITLE_PREFIX", "[Auto Exception]").strip() or "[Auto Exception]"
    labels_raw = os.getenv("GITHUB_ISSUE_LABELS", "").strip()
    assignees_raw = os.getenv("GITHUB_ISSUE_ASSIGNEES", "").strip()

    labels = [item.strip() for item in labels_raw.split(",") if item.strip()]
    assignees = [item.strip() for item in assignees_raw.split(",") if item.strip()]

    title = f"{issue_title_prefix} {entry.exception_type}: {entry.exception_message[:120]}"
    context_pretty = entry.context_json or "{}"
    body = (
        "Automated issue created from exception logger.\n\n"
        f"- Type: `{entry.exception_type}`\n"
        f"- Message: `{entry.exception_message[:500]}`\n"
        f"- Severity: `{entry.severity}`\n"
        f"- Source: `{entry.source}`\n"
        f"- User: `{entry.user_email or 'anonymous'}`\n"
        f"- Request ID: `{entry.request_id or 'n/a'}`\n"
        f"- Timestamp (UTC): `{entry.timestamp}`\n\n"
        "## Context\n"
        f"```json\n{context_pretty}\n```\n\n"
        "## Traceback\n"
        f"```\n{(entry.traceback or '')[:5000]}\n```"
    )

    payload = {
        "title": title[:256],
        "body": body,
    }
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees

    api_url = f"https://api.github.com/repos/{repository}/issues"
    req = urlrequest.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("number"), body.get("html_url")
    except (urlerror.URLError, TimeoutError, ValueError):
        # Never break caller flow when GitHub issue creation fails.
        return None, None


def log_exception(
    exc: Exception | None = None,
    *,
    severity: str = "error",
    source: str = "backend",
    context: dict | None = None,
    request_id: str | None = None,
    handled: bool = True,
) -> None:
    """Record an exception to the exception_log table.

    Safe to call from anywhere — inside or outside a request context.
    Never raises; if the recording itself fails it is silently swallowed
    so the original error handling is not disrupted.
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
            return

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
        )
        db.session.add(entry)
        db.session.commit()

        issue_number, issue_url = _create_github_issue_for_exception(entry)
        if issue_number and issue_url:
            entry.github_issue_number = issue_number
            entry.github_issue_url = issue_url
            db.session.commit()
    except Exception:
        db.session.rollback()
