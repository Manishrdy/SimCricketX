"""Tests for PLAN-IR-001 Phase 4 polish.

Covers:
  - log_exception() returns the created row id
  - Extended issue endpoint accepts explicit exception_log_id
  - Crash page template receives exception_log_id
  - Admin notes endpoint saves local-only notes
  - Admin notes endpoint enforces access control
  - Status badge stylesheet is present
"""

import json
import os
from datetime import datetime, timedelta

import pytest

from database import db
from database.models import ExceptionLog, IssueReport
from routes import issue_routes
from utils.exception_tracker import log_exception


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    issue_routes._reset_rate_limits_for_tests()
    yield
    issue_routes._reset_rate_limits_for_tests()


# ---------------------------------------------------------------------------
# log_exception returns id
# ---------------------------------------------------------------------------


def test_log_exception_returns_new_row_id(app):
    with app.app_context():
        try:
            raise RuntimeError("phase4-return-id-test")
        except Exception as exc:
            row_id = log_exception(exc, source="backend")

        assert isinstance(row_id, int)
        assert row_id > 0
        row = db.session.get(ExceptionLog, row_id)
        assert row is not None
        assert row.exception_message == "phase4-return-id-test"


def test_log_exception_returns_existing_id_on_dedup(app):
    with app.app_context():
        def _raise():
            raise ValueError("phase4-dedup")

        try:
            _raise()
        except Exception as exc:
            first_id = log_exception(exc, source="backend")
        try:
            _raise()
        except Exception as exc:
            second_id = log_exception(exc, source="backend")

        assert first_id == second_id
        row = db.session.get(ExceptionLog, first_id)
        assert row.occurrence_count == 2


def test_log_exception_returns_none_on_failure(app, monkeypatch):
    """If the DB commit blows up, log_exception must still return None."""
    with app.app_context():
        def _boom():
            raise RuntimeError("forced-commit-fail")

        monkeypatch.setattr(db.session, "commit", _boom)
        monkeypatch.setattr(db.session, "rollback", lambda: None)

        result = log_exception(Exception("should-swallow"), source="backend")
        assert result is None


# ---------------------------------------------------------------------------
# Explicit exception_log_id in the POST payload
# ---------------------------------------------------------------------------


def _post(client, payload):
    return client.post(
        "/api/issues/report",
        data=json.dumps(payload),
        content_type="application/json",
    )


def test_endpoint_accepts_explicit_exception_log_id(authenticated_client, app):
    with app.app_context():
        exc = ExceptionLog(
            exception_type="CrashLinkErr",
            exception_message="from-the-500-page",
            severity="error",
            source="backend",
            user_email="unrelated@example.com",  # different user to defeat the auto-window
            fingerprint="fp-crash-link-test",
            occurrence_count=1,
            first_seen_at=datetime.utcnow() - timedelta(hours=2),
            last_seen_at=datetime.utcnow() - timedelta(hours=2),
            timestamp=datetime.utcnow() - timedelta(hours=2),
        )
        db.session.add(exc)
        db.session.commit()
        exc_id = exc.id

    resp = _post(authenticated_client, {
        "title": "explicit link test",
        "description": "prefilled from the crash page",
        "exception_log_id": exc_id,
    })
    assert resp.status_code == 202

    with app.app_context():
        row = IssueReport.query.order_by(IssueReport.id.desc()).first()
        assert row.linked_exception_log_ids is not None
        linked = json.loads(row.linked_exception_log_ids)
        # Explicit id must be present AND placed at index 0.
        assert exc_id in linked
        assert linked[0] == exc_id


def test_endpoint_ignores_unknown_explicit_exception_log_id(authenticated_client, app):
    resp = _post(authenticated_client, {
        "title": "phantom link",
        "description": "no such exception",
        "exception_log_id": 9999999,
    })
    assert resp.status_code == 202

    with app.app_context():
        row = IssueReport.query.order_by(IssueReport.id.desc()).first()
        if row.linked_exception_log_ids:
            linked = json.loads(row.linked_exception_log_ids)
            assert 9999999 not in linked


def test_endpoint_tolerates_non_integer_exception_log_id(authenticated_client, app):
    resp = _post(authenticated_client, {
        "title": "garbage link",
        "description": "bad type for explicit link",
        "exception_log_id": "not-an-integer",
    })
    assert resp.status_code == 202


def test_endpoint_deduplicates_explicit_and_auto_linked_ids(authenticated_client, app):
    user_email = "testuser@example.com"
    with app.app_context():
        exc = ExceptionLog(
            exception_type="DupLinkErr",
            exception_message="boom",
            severity="error",
            source="backend",
            user_email=user_email,  # matches current user so auto-query picks it up too
            fingerprint="fp-dedupe-explicit",
            occurrence_count=1,
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
            timestamp=datetime.utcnow(),
        )
        db.session.add(exc)
        db.session.commit()
        exc_id = exc.id

    resp = _post(authenticated_client, {
        "title": "dedup test",
        "description": "explicit and auto should merge",
        "exception_log_id": exc_id,
    })
    assert resp.status_code == 202

    with app.app_context():
        row = IssueReport.query.order_by(IssueReport.id.desc()).first()
        linked = json.loads(row.linked_exception_log_ids)
        assert linked.count(exc_id) == 1
        assert linked[0] == exc_id


# ---------------------------------------------------------------------------
# Crash page template
# ---------------------------------------------------------------------------


def test_500_template_exists_and_references_prefill(app):
    template_path = os.path.join(app.template_folder or 'templates', '500.html')
    assert os.path.exists(template_path), "500.html should be present for the crash page flow"

    with open(template_path, encoding="utf-8") as fh:
        body = fh.read()

    # The prefill glue relies on these specific identifiers — breaking
    # any of them silently disables the crash-to-report flow.
    assert "scxReportCrash" in body
    assert "scxReportIssuePrefill" in body
    assert "exception_log_id" in body


# ---------------------------------------------------------------------------
# Admin notes endpoint
# ---------------------------------------------------------------------------


def test_admin_notes_saves_on_post(admin_client, app):
    with app.app_context():
        row = IssueReport(
            public_id="ISS-NOTES01",
            user_email="testuser@example.com",
            category="bug",
            title="notes target",
            description="d",
            status="new",
        )
        db.session.add(row)
        db.session.commit()
        row_id = row.id

    resp = admin_client.post(
        f"/admin/issues/reports/{row_id}/notes",
        data=json.dumps({"admin_notes": "triaged 2026-05-23, waiting on repro"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "saved"

    with app.app_context():
        row = db.session.get(IssueReport, row_id)
        assert "triaged 2026-05-23" in row.admin_notes


def test_admin_notes_rejects_missing_field(admin_client, app):
    with app.app_context():
        row = IssueReport(
            public_id="ISS-NOTES02",
            user_email="testuser@example.com",
            category="bug",
            title="t",
            description="d",
        )
        db.session.add(row)
        db.session.commit()
        row_id = row.id

    resp = admin_client.post(
        f"/admin/issues/reports/{row_id}/notes",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_admin_notes_rejects_non_json_content_type(admin_client, app):
    with app.app_context():
        row = IssueReport(
            public_id="ISS-NOTES03",
            user_email="testuser@example.com",
            category="bug",
            title="t",
            description="d",
        )
        db.session.add(row)
        db.session.commit()
        row_id = row.id

    resp = admin_client.post(
        f"/admin/issues/reports/{row_id}/notes",
        data="admin_notes=hi",
        content_type="application/x-www-form-urlencoded",
    )
    assert resp.status_code == 400


def test_admin_notes_404_for_unknown_row(admin_client):
    resp = admin_client.post(
        "/admin/issues/reports/9999999/notes",
        data=json.dumps({"admin_notes": "hello"}),
        content_type="application/json",
    )
    assert resp.status_code == 404


def test_admin_notes_requires_admin(authenticated_client, app):
    with app.app_context():
        row = IssueReport(
            public_id="ISS-NOTES04",
            user_email="testuser@example.com",
            category="bug",
            title="t",
            description="d",
        )
        db.session.add(row)
        db.session.commit()
        row_id = row.id

    resp = authenticated_client.post(
        f"/admin/issues/reports/{row_id}/notes",
        data=json.dumps({"admin_notes": "should not work"}),
        content_type="application/json",
    )
    assert resp.status_code == 403


def test_admin_notes_truncates_very_long_input(admin_client, app):
    with app.app_context():
        row = IssueReport(
            public_id="ISS-NOTES05",
            user_email="testuser@example.com",
            category="bug",
            title="t",
            description="d",
        )
        db.session.add(row)
        db.session.commit()
        row_id = row.id

    giant = "A" * 15000
    resp = admin_client.post(
        f"/admin/issues/reports/{row_id}/notes",
        data=json.dumps({"admin_notes": giant}),
        content_type="application/json",
    )
    assert resp.status_code == 200

    with app.app_context():
        row = db.session.get(IssueReport, row_id)
        assert len(row.admin_notes) == 10000


# ---------------------------------------------------------------------------
# Status badge CSS
# ---------------------------------------------------------------------------


def test_status_badge_stylesheet_exists(app):
    path = os.path.join(app.root_path, "static", "css", "admin_issues.css")
    assert os.path.exists(path), "admin_issues.css should be present for badge colors"
    with open(path, encoding="utf-8") as fh:
        body = fh.read()

    for cls in [
        ".a-tag", ".a-tag-new", ".a-tag-open", ".a-tag-in_progress",
        ".a-tag-resolved", ".a-tag-closed", ".a-tag-deferred", ".a-tag-wont_fix",
        ".a-tag-critical", ".a-tag-error", ".a-tag-warning", ".a-tag-info",
    ]:
        assert cls in body, f"missing CSS rule for {cls}"
