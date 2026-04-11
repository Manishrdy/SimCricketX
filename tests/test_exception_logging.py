import json

from flask_login import login_user, logout_user

from database import db
from database.models import ExceptionLog
from engine.stats_service import StatsService
from utils import exception_tracker
from utils.exception_tracker import log_exception


def test_log_exception_persists_extended_metadata(app):
    with app.app_context():
        before_count = ExceptionLog.query.count()

        try:
            raise ValueError("test-metadata-error")
        except Exception as exc:
            log_exception(
                exc,
                severity="critical",
                source="sqlite",
                context={"feature": "unit-test", "step": 1},
                request_id="req-test-123",
                handled=False,
            )

        after_count = ExceptionLog.query.count()
        assert after_count == before_count + 1

        row = ExceptionLog.query.order_by(ExceptionLog.id.desc()).first()
        assert row is not None
        assert row.exception_type == "ValueError"
        assert row.exception_message == "test-metadata-error"
        assert row.severity == "critical"
        assert row.source == "sqlite"
        assert row.request_id == "req-test-123"
        assert row.handled is False
        assert row.resolved is False

        payload = json.loads(row.context_json)
        assert payload["feature"] == "unit-test"
        assert payload["step"] == 1


def test_log_exception_captures_request_context_and_user(app, regular_user):
    with app.app_context():
        with app.test_request_context(
            "/unit/exception-check",
            method="POST",
            headers={"X-Request-ID": "hdr-req-id-42"},
        ):
            login_user(regular_user)
            try:
                raise RuntimeError("request-context-error")
            except Exception as exc:
                log_exception(exc, source="backend")
            finally:
                logout_user()

        row = ExceptionLog.query.order_by(ExceptionLog.id.desc()).first()
        assert row is not None
        assert row.exception_type == "RuntimeError"
        assert row.user_email == regular_user.id
        assert row.request_id == "hdr-req-id-42"

        payload = json.loads(row.context_json)
        assert payload["path"] == "/unit/exception-check"
        assert payload["method"] == "POST"
        assert payload["remote_addr"] is None


def test_log_exception_is_fail_safe_when_db_write_fails(app, monkeypatch):
    with app.app_context():
        rollback_called = {"value": False}

        def _boom():
            raise RuntimeError("forced-commit-failure")

        def _rollback():
            rollback_called["value"] = True

        monkeypatch.setattr(db.session, "commit", _boom)
        monkeypatch.setattr(db.session, "rollback", _rollback)

        # Should never raise, even when DB write fails internally.
        log_exception(Exception("should-not-propagate"), source="backend")
        assert rollback_called["value"] is True


def test_statistics_route_exception_is_logged_to_db(app, authenticated_client, regular_user, monkeypatch):
    with app.app_context():
        before_count = ExceptionLog.query.count()

    def _boom(*args, **kwargs):
        raise RuntimeError("forced-stat-route-error")

    monkeypatch.setattr(StatsService, "get_overall_stats", _boom)

    response = authenticated_client.get("/statistics")
    assert response.status_code == 200

    with app.app_context():
        after_count = ExceptionLog.query.count()
        assert after_count == before_count + 1

        row = ExceptionLog.query.order_by(ExceptionLog.id.desc()).first()
        assert row is not None
        assert row.exception_type == "RuntimeError"
        assert row.exception_message == "forced-stat-route-error"
        assert row.source == "backend"
        assert row.handled is True
        assert row.user_email == regular_user.id

        payload = json.loads(row.context_json)
        assert payload["path"] == "/statistics"
        assert payload["method"] == "GET"
        assert payload["endpoint"] == "statistics"


def test_log_exception_creates_github_issue_when_enabled(app, monkeypatch):
    captured = {"requests": 0, "bodies": []}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"number": 123, "html_url": "https://github.com/owner/repo/issues/123"}).encode("utf-8")

        def getcode(self):
            return 201

    def _fake_urlopen(req, timeout=10):
        captured["requests"] += 1
        try:
            captured["bodies"].append(req.data.decode("utf-8") if req.data else "")
        except Exception:
            captured["bodies"].append("")
        return _Resp()

    monkeypatch.setenv("GITHUB_ISSUE_ON_EXCEPTION_ENABLED", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_ISSUE_LABELS", "bug,auto-exception")
    monkeypatch.setenv("GITHUB_ISSUE_ASSIGNEES", "alice")
    monkeypatch.setattr(exception_tracker.urlrequest, "urlopen", _fake_urlopen)

    def _raise_same_error():
        raise ValueError("github-issue-test")

    with app.app_context():
        try:
            _raise_same_error()
        except Exception as exc:
            log_exception(exc, source="backend")
        try:
            # Same fingerprint -> should update same row without creating another issue
            _raise_same_error()
        except Exception as exc:
            log_exception(exc, source="backend")

    assert captured["requests"] == 1

    with app.app_context():
        row = ExceptionLog.query.filter_by(exception_type="ValueError", exception_message="github-issue-test").first()
        assert row is not None
        assert row.occurrence_count == 2
        assert row.github_issue_number == 123
        assert row.github_issue_url == "https://github.com/owner/repo/issues/123"
        assert row.github_sync_status == "synced"
        assert row.github_sync_error is None
        assert row.github_last_synced_at is not None


def test_log_exception_ignores_github_issue_failures(app, monkeypatch):
    def _raise_url_error(req, timeout=10):
        raise exception_tracker.urlerror.URLError("network-down")

    monkeypatch.setenv("GITHUB_ISSUE_ON_EXCEPTION_ENABLED", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(exception_tracker.urlrequest, "urlopen", _raise_url_error)

    with app.app_context():
        before = ExceptionLog.query.count()
        try:
            raise RuntimeError("issue-fail-safe")
        except Exception as exc:
            # Should not raise despite failing GitHub API call.
            log_exception(exc, source="backend")
        after = ExceptionLog.query.count()

        assert after == before + 1

        row = ExceptionLog.query.filter_by(exception_type="RuntimeError", exception_message="issue-fail-safe").first()
        assert row is not None
        # Queue ran inline but the network call failed -> sync status should be 'failed'.
        assert row.github_sync_status == "failed"
        assert row.github_sync_error is not None
        assert row.github_issue_number is None


def test_log_exception_scrubs_pii_before_sending_to_github(app, monkeypatch):
    """The outbound issue body must not contain raw emails / IPs / tokens."""
    captured = {"bodies": []}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"number": 9, "html_url": "https://github.com/owner/repo/issues/9"}).encode("utf-8")

        def getcode(self):
            return 201

    def _fake_urlopen(req, timeout=10):
        captured["bodies"].append(req.data.decode("utf-8") if req.data else "")
        return _Resp()

    monkeypatch.setenv("GITHUB_ISSUE_ON_EXCEPTION_ENABLED", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(exception_tracker.urlrequest, "urlopen", _fake_urlopen)

    with app.app_context():
        try:
            raise RuntimeError(
                "leak victim@example.com from 10.0.0.5 with token github_pat_AAAAAAAAAAAAAAAAAAAAAAAA"
            )
        except Exception as exc:
            log_exception(
                exc,
                source="backend",
                context={
                    "user_email": "user@example.com",
                    "client_ip": "192.168.1.42",
                    "password": "hunter2",
                    "token": "github_pat_BBBBBBBBBBBBBBBBBBBBBBBB",
                },
            )

    assert len(captured["bodies"]) == 1
    body = captured["bodies"][0]

    # Sensitive substrings must NOT appear anywhere in the outbound JSON.
    assert "victim@example.com" not in body
    assert "user@example.com" not in body
    assert "10.0.0.5" not in body
    assert "192.168.1.42" not in body
    assert "github_pat_AAAA" not in body
    assert "github_pat_BBBB" not in body
    assert "hunter2" not in body

    # Redaction markers should be present.
    assert "<email>" in body or "<redacted>" in body
    assert "<ip>" in body or "<redacted>" in body
    assert "<redacted-token>" in body or "<redacted>" in body


def test_log_exception_deduplicates_same_fingerprint(app):
    with app.app_context():
        before = ExceptionLog.query.count()
        for _ in range(3):
            try:
                raise RuntimeError("dedupe-me")
            except Exception as exc:
                log_exception(exc, source="backend")

        after = ExceptionLog.query.count()
        assert after == before + 1

        row = ExceptionLog.query.filter_by(exception_type="RuntimeError", exception_message="dedupe-me").first()
        assert row is not None
        assert row.occurrence_count == 3
        assert row.first_seen_at is not None
        assert row.last_seen_at is not None
        assert row.fingerprint is not None and len(row.fingerprint) == 64
