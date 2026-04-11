"""Tests for middleware.session_log_capture.

The handler is installed by app.create_app() in test mode, but these
tests exercise the buffer / handler / sweeper directly so they don't
depend on a request lifecycle.
"""

import logging
import threading
import time

import pytest

from middleware import session_log_capture


@pytest.fixture(autouse=True)
def _clear_buffers():
    session_log_capture.clear_all()
    yield
    session_log_capture.clear_all()


def test_append_and_snapshot_returns_independent_copy():
    sid = "session-A"
    session_log_capture.append_for_session(sid, {"msg": "first"})
    session_log_capture.append_for_session(sid, {"msg": "second"})

    snap = session_log_capture.snapshot(sid)
    assert [e["msg"] for e in snap] == ["first", "second"]

    # Mutating the snapshot must not corrupt the underlying buffer.
    snap.append({"msg": "third"})
    assert len(session_log_capture.snapshot(sid)) == 2


def test_ring_buffer_caps_at_max_records():
    sid = "session-cap"
    over = session_log_capture.MAX_RECORDS_PER_SESSION + 50
    for i in range(over):
        session_log_capture.append_for_session(sid, {"i": i})

    snap = session_log_capture.snapshot(sid)
    assert len(snap) == session_log_capture.MAX_RECORDS_PER_SESSION
    # Oldest entries should have been evicted.
    assert snap[0]["i"] == over - session_log_capture.MAX_RECORDS_PER_SESSION
    assert snap[-1]["i"] == over - 1


def test_distinct_sessions_are_isolated():
    session_log_capture.append_for_session("alice", {"msg": "from-alice"})
    session_log_capture.append_for_session("bob", {"msg": "from-bob"})

    assert [e["msg"] for e in session_log_capture.snapshot("alice")] == ["from-alice"]
    assert [e["msg"] for e in session_log_capture.snapshot("bob")] == ["from-bob"]
    assert session_log_capture.snapshot("nobody") == []


def test_sweep_stale_evicts_old_sessions():
    session_log_capture.append_for_session("recent", {"msg": "ok"})
    session_log_capture.append_for_session("old", {"msg": "ok"})

    # Force the "old" session's last_seen well into the past.
    with session_log_capture._lock:  # type: ignore[attr-defined]
        session_log_capture._last_seen["old"] = time.time() - (session_log_capture.SESSION_TTL_SECONDS + 60)

    evicted = session_log_capture.sweep_stale()
    assert evicted == 1
    assert session_log_capture.snapshot("old") == []
    assert len(session_log_capture.snapshot("recent")) == 1


def test_concurrent_writers_do_not_lose_records():
    """10 threads x 50 records = 500 records, all attributed to the same session."""
    sid = "concurrent-test"
    threads = []
    per_thread = 50
    workers = 10

    def writer(worker_id: int) -> None:
        for j in range(per_thread):
            session_log_capture.append_for_session(sid, {"worker": worker_id, "j": j})

    for w in range(workers):
        t = threading.Thread(target=writer, args=(w,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    snap = session_log_capture.snapshot(sid)
    expected = min(per_thread * workers, session_log_capture.MAX_RECORDS_PER_SESSION)
    assert len(snap) == expected

    # Every record should look well-formed (no torn writes).
    for record in snap:
        assert "worker" in record
        assert "j" in record


def test_concurrent_writers_across_distinct_sessions():
    """Each thread writes to its own session id; no cross-talk allowed."""
    workers = 8
    per_thread = 30
    threads = []

    def writer(worker_id: int) -> None:
        sid = f"sess-{worker_id}"
        for j in range(per_thread):
            session_log_capture.append_for_session(sid, {"j": j})

    for w in range(workers):
        t = threading.Thread(target=writer, args=(w,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    for w in range(workers):
        snap = session_log_capture.snapshot(f"sess-{w}")
        assert len(snap) == per_thread
        assert [e["j"] for e in snap] == list(range(per_thread))


def test_handler_attributes_records_to_g_session_id(app):
    """A real Flask request: SessionLogHandler should pick up g.session_id."""
    captured_sid = "handler-test-session"

    with app.test_request_context("/some/path"):
        from flask import g
        g.session_id = captured_sid
        g.request_id = "req-1"
        g.request_path = "/some/path"

        log = logging.getLogger("scx.test.handler")
        log.setLevel(logging.DEBUG)
        log.warning("hello from handler test")

    snap = session_log_capture.snapshot(captured_sid)
    assert any("hello from handler test" in (e.get("msg") or "") for e in snap)
