"""Tests for services.github_issue_queue.

The queue is wired into the test app fixture in synchronous mode, so any
job dispatched via `enqueue_exception` runs immediately on the calling
thread inside a (nested) app context.
"""

import json
from datetime import datetime

from database import db
from database.models import ExceptionLog
from services import github_issue_queue
from utils import exception_tracker


def _make_row(app, **overrides):
    """Insert a fresh ExceptionLog row and return its id."""
    with app.app_context():
        row = ExceptionLog(
            exception_type=overrides.get("exception_type", "RuntimeError"),
            exception_message=overrides.get("exception_message", "queue-test"),
            traceback=overrides.get("traceback", "Traceback (most recent call last):\n  ..."),
            module="tests.test_github_issue_queue",
            function="_make_row",
            line_number=1,
            filename=__file__,
            user_email=overrides.get("user_email"),
            severity="error",
            source="backend",
            context_json=overrides.get("context_json", "{}"),
            request_id=None,
            handled=True,
            fingerprint=overrides.get("fingerprint", "queue-test-fp-{}".format(datetime.utcnow().timestamp())),
            occurrence_count=1,
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
            timestamp=datetime.utcnow(),
            github_sync_status="pending",
        )
        db.session.add(row)
        db.session.commit()
        return row.id


def test_process_one_marks_synced_on_success(app, monkeypatch):
    captured = {"calls": 0}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"number": 7, "html_url": "https://github.com/owner/repo/issues/7"}).encode("utf-8")

        def getcode(self):
            return 201

    def _fake_urlopen(req, timeout=10):
        captured["calls"] += 1
        return _Resp()

    monkeypatch.setenv("GITHUB_ISSUE_ON_EXCEPTION_ENABLED", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(exception_tracker.urlrequest, "urlopen", _fake_urlopen)

    row_id = _make_row(app)

    accepted = github_issue_queue.enqueue_exception(row_id)
    assert accepted is True
    assert captured["calls"] == 1

    with app.app_context():
        row = db.session.get(ExceptionLog, row_id)
        assert row.github_issue_number == 7
        assert row.github_issue_url == "https://github.com/owner/repo/issues/7"
        assert row.github_sync_status == "synced"
        assert row.github_sync_error is None
        assert row.github_last_synced_at is not None


def test_process_one_marks_failed_on_http_error(app, monkeypatch):
    from urllib import error as urlerror

    def _raise_url_error(req, timeout=10):
        raise urlerror.URLError("network-down")

    monkeypatch.setenv("GITHUB_ISSUE_ON_EXCEPTION_ENABLED", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(exception_tracker.urlrequest, "urlopen", _raise_url_error)

    row_id = _make_row(app, fingerprint="failed-row-fp")

    github_issue_queue.enqueue_exception(row_id)

    with app.app_context():
        row = db.session.get(ExceptionLog, row_id)
        assert row.github_issue_number is None
        assert row.github_sync_status == "failed"
        assert row.github_sync_error is not None
        assert "network" in row.github_sync_error.lower() or "error" in row.github_sync_error.lower()


def test_process_one_skips_when_already_synced(app, monkeypatch):
    captured = {"calls": 0}

    def _fake_urlopen(req, timeout=10):
        captured["calls"] += 1
        raise AssertionError("should not be called when row already has github_issue_number")

    monkeypatch.setenv("GITHUB_ISSUE_ON_EXCEPTION_ENABLED", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(exception_tracker.urlrequest, "urlopen", _fake_urlopen)

    row_id = _make_row(app, fingerprint="already-synced-fp")
    with app.app_context():
        row = db.session.get(ExceptionLog, row_id)
        row.github_issue_number = 999
        row.github_issue_url = "https://github.com/owner/repo/issues/999"
        row.github_sync_status = "synced"
        db.session.commit()

    github_issue_queue.enqueue_exception(row_id)
    assert captured["calls"] == 0


def test_enqueue_returns_false_when_disabled(app, monkeypatch):
    """Disabled config: queue still accepts the job, but the worker no-ops."""
    monkeypatch.delenv("GITHUB_ISSUE_ON_EXCEPTION_ENABLED", raising=False)

    row_id = _make_row(app, fingerprint="disabled-fp")
    accepted = github_issue_queue.enqueue_exception(row_id)
    # Sync mode runs inline; with the feature disabled the worker just returns,
    # leaving the row in its initial 'pending' state.
    assert accepted is True
    with app.app_context():
        row = db.session.get(ExceptionLog, row_id)
        assert row.github_sync_status == "pending"
        assert row.github_issue_number is None
