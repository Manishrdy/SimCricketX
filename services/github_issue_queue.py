"""Bounded background queue for outbound GitHub issue creation.

Why this exists
---------------
Originally `utils/exception_tracker.py` made a synchronous urllib call to
GitHub from inside the Flask error handler. On a slow GitHub day that
blocks every 500-response for up to ten seconds. This module moves the
network round-trip onto a daemon worker thread so the request handler
can return immediately.

Job kinds
---------
- `'exception'` : ExceptionLog row -> auto-filed GitHub issue

Manual user support messages are handled in-app and no longer create GitHub
issues automatically.

Public API
----------
- `start_worker(app)` — call once during create_app()
- `enqueue_exception(exception_log_id)` — fire-and-forget enqueue
- `process_one(job, app)` — exposed for tests / manual flush
- `flush_for_tests(app)` — drains the queue synchronously
- `set_synchronous_mode(enabled)` — tests bypass the background thread
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass

logger = logging.getLogger("SimCricketX.github_queue")


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

MAX_QUEUE_SIZE = 500

_queue: "queue.Queue[Job]" = queue.Queue(maxsize=MAX_QUEUE_SIZE)
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()
_app_ref = None  # set by start_worker(app)
_synchronous_mode = False  # set True in tests; bypasses background thread


@dataclass
class Job:
    """A unit of work for the worker."""
    kind: str        # 'exception'
    row_id: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_worker(app) -> None:
    """Start the background worker if it isn't running.

    Idempotent: safe to call from create_app() and re-imported modules.
    In synchronous mode (tests), only the app reference is stored — no
    background thread is spawned.
    """
    global _worker_thread, _app_ref
    with _worker_lock:
        _app_ref = app
        if _synchronous_mode:
            return
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_thread = threading.Thread(
            target=_run_loop,
            name="github-issue-queue-worker",
            daemon=True,
        )
        _worker_thread.start()


def set_synchronous_mode(enabled: bool) -> None:
    """Tests use this to make `enqueue_*` execute inline.

    When enabled, the worker thread is bypassed entirely; jobs run on the
    caller's thread inside the existing app context.
    """
    global _synchronous_mode
    _synchronous_mode = bool(enabled)


def enqueue_exception(exception_log_id: int) -> bool:
    """Schedule a GitHub issue creation for an existing ExceptionLog row."""
    if exception_log_id is None:
        return False
    return _enqueue(Job(kind="exception", row_id=int(exception_log_id)))


def _enqueue(job: Job) -> bool:
    """Common dispatch path for any Job kind.

    Returns True if the job was accepted (or executed inline in test mode),
    False if the queue was full or no app context could be resolved.
    """
    if _synchronous_mode:
        try:
            from flask import current_app, has_app_context  # local import to avoid hard dep
            if has_app_context():
                process_one(job, current_app._get_current_object())
                return True
            if _app_ref is not None:
                process_one(job, _app_ref)
                return True
            return False
        except Exception:
            logger.exception("github_issue_queue: synchronous dispatch failed")
            return False

    try:
        _queue.put_nowait(job)
        return True
    except queue.Full:
        logger.warning(
            "github_issue_queue: queue full (size=%d), dropping job %r",
            MAX_QUEUE_SIZE, job,
        )
        return False


def flush_for_tests(app, timeout: float = 5.0) -> int:
    """Drain pending jobs synchronously. Returns the number processed."""
    processed = 0
    while True:
        try:
            job = _queue.get(timeout=timeout)
        except queue.Empty:
            return processed
        try:
            process_one(job, app)
            processed += 1
        finally:
            _queue.task_done()


# ---------------------------------------------------------------------------
# Worker internals
# ---------------------------------------------------------------------------


def _run_loop() -> None:
    """Daemon worker. On unhandled crash it logs and continues."""
    while True:
        try:
            job = _queue.get()
        except Exception:
            import time
            time.sleep(0.5)
            continue

        try:
            if _app_ref is None:
                logger.error("github_issue_queue: no app reference, dropping job %r", job)
                continue
            process_one(job, _app_ref)
        except Exception:
            logger.exception("github_issue_queue: worker crashed handling %r", job)
        finally:
            try:
                _queue.task_done()
            except Exception:
                pass


def process_one(job: Job, app) -> None:
    """Execute a single job. Always swallows errors — never raises."""
    try:
        if job.kind == "exception":
            _process_exception_job(job.row_id, app)
        else:
            logger.warning("github_issue_queue: unknown job kind %r", job.kind)
    except Exception:
        logger.exception("github_issue_queue: process_one failed for %r", job)


def _process_exception_job(row_id: int, app) -> None:
    """Build a scrubbed GitHub issue body for an ExceptionLog row and POST it."""
    import json as _json
    from database import db
    from database.models import ExceptionLog
    from services import github_issues, issue_scrubber

    if not github_issues.is_enabled():
        return

    with app.app_context():
        entry = db.session.get(ExceptionLog, row_id)
        if entry is None:
            return
        if entry.github_issue_number:
            return

        title_prefix = github_issues.get_title_prefix()
        labels = github_issues.get_default_labels()
        assignees = github_issues.get_default_assignees()

        scrubbed_message = issue_scrubber.scrub_text(entry.exception_message or "")
        scrubbed_traceback = issue_scrubber.scrub_traceback(entry.traceback or "")

        try:
            ctx_obj = _json.loads(entry.context_json) if entry.context_json else {}
            scrubbed_ctx = issue_scrubber.scrub_dict(ctx_obj)
            scrubbed_context_json = _json.dumps(scrubbed_ctx, default=str, indent=2)
        except Exception:
            scrubbed_context_json = issue_scrubber.scrub_text(entry.context_json or "{}")

        scrubbed_user_email = issue_scrubber.scrub_text(entry.user_email or "anonymous")

        title = f"{title_prefix} {entry.exception_type}: {scrubbed_message[:120]}"
        body = (
            "Automated issue created from exception logger.\n\n"
            f"- Type: `{entry.exception_type}`\n"
            f"- Message: `{scrubbed_message[:500]}`\n"
            f"- Severity: `{entry.severity}`\n"
            f"- Source: `{entry.source}`\n"
            f"- User: `{scrubbed_user_email}`\n"
            f"- Request ID: `{entry.request_id or 'n/a'}`\n"
            f"- Timestamp (UTC): `{entry.timestamp}`\n\n"
            "## Context\n"
            f"```json\n{scrubbed_context_json}\n```\n\n"
            "## Traceback\n"
            f"```\n{scrubbed_traceback[:5000]}\n```"
        )

        issue_number, issue_url, error = github_issues.create_issue(
            title=title,
            body=body,
            labels=labels,
            assignees=assignees,
        )

        entry = db.session.get(ExceptionLog, row_id)
        if entry is None:
            return

        from datetime import datetime as _dt

        if issue_number and issue_url:
            entry.github_issue_number = issue_number
            entry.github_issue_url = issue_url
            entry.github_sync_status = "synced"
            entry.github_sync_error = None
            entry.github_last_synced_at = _dt.utcnow()
        else:
            entry.github_sync_status = "failed"
            entry.github_sync_error = (error or "unknown")[:1000]
            entry.github_last_synced_at = _dt.utcnow()

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("github_issue_queue: db commit failed for exception_log id=%s", row_id)
