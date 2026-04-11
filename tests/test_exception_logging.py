import json

from flask_login import login_user, logout_user

from database import db
from database.models import ExceptionLog
from engine.stats_service import StatsService
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
