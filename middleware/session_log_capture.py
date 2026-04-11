"""Per-session in-memory log capture for the issue reporting widget.

When a user files a bug report, we want to attach the recent log lines
*they generated* — not the global log of every request from every user.
This module installs a `logging.Handler` that, on every emitted record,
looks at `flask.g.session_id` and appends the record to a per-session
ring buffer guarded by a single module-level lock.

Design choices
--------------
- `collections.deque(maxlen=N)` bounds memory automatically and gives
  O(1) append. The atomic append after acquiring our lock is fully
  thread-safe under the GIL.
- A background sweeper thread runs every `SWEEP_INTERVAL` seconds and
  evicts sessions whose `last_seen` timestamp is older than `SESSION_TTL`.
- The handler is fail-safe: any exception inside `emit()` is swallowed so
  a logging hiccup never breaks the user request that produced it.
- Single-process only. Multi-worker Gunicorn fragments buffers across
  workers — documented in PLAN-IR-001 risk R6, Redis-backed migration
  path noted there.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from flask import g, has_app_context


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MAX_RECORDS_PER_SESSION = 200
SESSION_TTL_SECONDS = 1800   # 30 minutes
SWEEP_INTERVAL_SECONDS = 300  # 5 minutes
MAX_MESSAGE_LENGTH = 1000

# Logger names we never want to capture (avoids feedback loops where the
# capture machinery itself emits records that re-enter the buffer).
_EXCLUDED_LOGGER_PREFIXES = (
    "SimCricketX.github_queue",
    "werkzeug",  # noisy HTTP access logs
)


# ---------------------------------------------------------------------------
# Module state (single instance per process)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_buffers: dict[str, deque] = {}
_last_seen: dict[str, float] = {}
_sweeper_started = False
_sweeper_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append_for_session(session_id: str, record: dict[str, Any]) -> None:
    """Append a single log record to a session's ring buffer.

    Exposed publicly so tests (and the future user-report endpoint) can
    inject synthetic records without going through the logging system.
    """
    if not session_id:
        return
    with _lock:
        buf = _buffers.get(session_id)
        if buf is None:
            buf = deque(maxlen=MAX_RECORDS_PER_SESSION)
            _buffers[session_id] = buf
        buf.append(record)
        _last_seen[session_id] = time.time()


def snapshot(session_id: str) -> list[dict[str, Any]]:
    """Return a *copy* of the session's current log buffer.

    Returns an empty list if the session has no captured records.
    """
    if not session_id:
        return []
    with _lock:
        buf = _buffers.get(session_id)
        if buf is None:
            return []
        return list(buf)


def clear_session(session_id: str) -> None:
    """Drop a single session's buffer (used by tests / logout)."""
    if not session_id:
        return
    with _lock:
        _buffers.pop(session_id, None)
        _last_seen.pop(session_id, None)


def clear_all() -> None:
    """Drop ALL buffers. Used by tests between cases."""
    with _lock:
        _buffers.clear()
        _last_seen.clear()


def buffer_count() -> int:
    """Number of distinct sessions currently held. For diagnostics / tests."""
    with _lock:
        return len(_buffers)


def sweep_stale(now: float | None = None) -> int:
    """Evict sessions whose last_seen is older than SESSION_TTL_SECONDS.

    Returns the number of sessions evicted. Public so tests can call it
    directly without waiting for the background thread.
    """
    cutoff = (now if now is not None else time.time()) - SESSION_TTL_SECONDS
    evicted = 0
    with _lock:
        stale = [sid for sid, ts in _last_seen.items() if ts < cutoff]
        for sid in stale:
            _buffers.pop(sid, None)
            _last_seen.pop(sid, None)
            evicted += 1
    return evicted


# ---------------------------------------------------------------------------
# Logging handler
# ---------------------------------------------------------------------------


class SessionLogHandler(logging.Handler):
    """Routes every log record into the current request's session buffer.

    Records emitted outside of a Flask request context are silently
    ignored — they have no `g.session_id` to attribute them to.
    """

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401 (logging API)
        try:
            # Skip our own internal loggers to prevent feedback loops.
            for prefix in _EXCLUDED_LOGGER_PREFIXES:
                if record.name.startswith(prefix):
                    return

            if not has_app_context():
                return

            sid = getattr(g, "session_id", None)
            if not sid:
                return

            try:
                msg = record.getMessage()
            except Exception:
                msg = str(record.msg)

            entry = {
                "ts": record.created,
                "lvl": record.levelname,
                "msg": (msg or "")[:MAX_MESSAGE_LENGTH],
                "logger": record.name,
                "mod": record.module,
                "func": record.funcName,
                "line": record.lineno,
                "request_id": getattr(g, "request_id", None),
                "path": getattr(g, "request_path", None),
            }
            append_for_session(sid, entry)
        except Exception:
            # Never let logging itself break the request.
            return


# ---------------------------------------------------------------------------
# Background sweeper
# ---------------------------------------------------------------------------


def start_sweeper() -> None:
    """Spawn the background TTL sweeper thread once per process.

    Idempotent: safe to call from create_app() and re-imported modules.
    """
    global _sweeper_started
    with _sweeper_lock:
        if _sweeper_started:
            return
        thread = threading.Thread(
            target=_sweeper_loop,
            name="session-log-sweeper",
            daemon=True,
        )
        thread.start()
        _sweeper_started = True


def _sweeper_loop() -> None:
    while True:
        try:
            time.sleep(SWEEP_INTERVAL_SECONDS)
            sweep_stale()
        except Exception:
            # Sleep briefly before retrying so we never spin on persistent failures.
            time.sleep(5)
