"""Tests for routes/admin_issue_routes.py — PLAN-IR-001 Phase 2."""

import json
from datetime import datetime, timedelta

import pytest

from database import db
from database.models import ExceptionLog, IssueReport


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_report(app, **overrides):
    with app.app_context():
        row = IssueReport(
            public_id=overrides.get("public_id", "ISS-AAA111"),
            user_email=overrides.get("user_email", "testuser@example.com"),
            category=overrides.get("category", "bug"),
            title=overrides.get("title", "Sample title"),
            description=overrides.get("description", "Sample description"),
            page_url=overrides.get("page_url", "https://localhost/something"),
            user_agent=overrides.get("user_agent", "pytest-agent"),
            app_version=overrides.get("app_version", "2.3.2"),
            session_logs_json=overrides.get("session_logs_json"),
            linked_exception_log_ids=overrides.get("linked_exception_log_ids"),
            github_issue_number=overrides.get("github_issue_number"),
            github_issue_url=overrides.get("github_issue_url"),
            github_sync_status=overrides.get("github_sync_status", "pending"),
            github_sync_error=overrides.get("github_sync_error"),
            status=overrides.get("status", "new"),
        )
        db.session.add(row)
        db.session.commit()
        return row.id, row.public_id


def _make_exception(app, **overrides):
    with app.app_context():
        row = ExceptionLog(
            exception_type=overrides.get("exception_type", "TestErr"),
            exception_message=overrides.get("exception_message", "boom"),
            traceback=overrides.get("traceback", "Traceback…"),
            module=overrides.get("module", "tests"),
            function=overrides.get("function", "fixture"),
            line_number=overrides.get("line_number", 1),
            filename=overrides.get("filename", __file__),
            user_email=overrides.get("user_email", "testuser@example.com"),
            severity=overrides.get("severity", "error"),
            source=overrides.get("source", "backend"),
            context_json=overrides.get("context_json"),
            request_id=overrides.get("request_id"),
            handled=True,
            fingerprint=overrides.get("fingerprint", f"fp-{datetime.utcnow().timestamp()}"),
            occurrence_count=overrides.get("occurrence_count", 1),
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
            timestamp=datetime.utcnow(),
            github_issue_number=overrides.get("github_issue_number"),
            github_issue_url=overrides.get("github_issue_url"),
            github_sync_status=overrides.get("github_sync_status"),
        )
        db.session.add(row)
        db.session.commit()
        return row.id


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def test_list_requires_admin(authenticated_client):
    """Regular logged-in users get 403 from the listing route."""
    resp = authenticated_client.get("/admin/issues")
    assert resp.status_code == 403


def test_list_anonymous_redirects_to_login(client):
    resp = client.get("/admin/issues")
    assert resp.status_code in (302, 401)


def test_admin_can_load_listing(admin_client):
    resp = admin_client.get("/admin/issues")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Issue Tracker" in body


# ---------------------------------------------------------------------------
# Stats endpoint
# ---------------------------------------------------------------------------


def test_stats_endpoint_returns_buckets(admin_client, app):
    _make_report(app, public_id="ISS-NEW001", status="new")
    _make_report(app, public_id="ISS-NEW002", status="new")
    _make_report(app, public_id="ISS-OPEN01", status="open")
    _make_report(app, public_id="ISS-RES001", status="resolved")
    _make_exception(app, fingerprint="fp-stats-1")

    resp = admin_client.get("/admin/issues/stats")
    assert resp.status_code == 200
    body = resp.get_json()

    assert body["reports"]["total"] == 4
    assert body["reports"]["by_status"]["new"] == 2
    assert body["reports"]["by_status"]["open"] == 1
    assert body["reports"]["by_status"]["resolved"] == 1
    assert body["exceptions"]["total"] >= 1


def test_stats_requires_admin(authenticated_client):
    resp = authenticated_client.get("/admin/issues/stats")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Listing tabs / filters
# ---------------------------------------------------------------------------


def test_list_reports_tab_shows_recent_rows(admin_client, app):
    _make_report(app, public_id="ISS-LIST01", title="findable title")
    resp = admin_client.get("/admin/issues?kind=reports")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ISS-LIST01" in body
    assert "findable title" in body


def test_list_exceptions_tab_shows_recent_rows(admin_client, app):
    _make_exception(app, exception_type="WeirdError", exception_message="distinctive marker", fingerprint="fp-exc-list")
    resp = admin_client.get("/admin/issues?kind=exceptions")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "WeirdError" in body
    assert "distinctive marker" in body


def test_list_combined_tab_shows_both_kinds(admin_client, app):
    _make_report(app, public_id="ISS-COMB01", title="report-in-combined")
    _make_exception(app, exception_type="ComboErr", exception_message="exc-in-combined", fingerprint="fp-combo")
    resp = admin_client.get("/admin/issues?kind=combined")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ISS-COMB01" in body
    assert "ComboErr" in body


def test_list_search_filters_by_title(admin_client, app):
    _make_report(app, public_id="ISS-SRCH01", title="needle alpha")
    _make_report(app, public_id="ISS-SRCH02", title="haystack beta")
    resp = admin_client.get("/admin/issues?kind=reports&q=needle")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ISS-SRCH01" in body
    assert "ISS-SRCH02" not in body


def test_list_status_filter_works(admin_client, app):
    _make_report(app, public_id="ISS-FILT01", status="resolved")
    _make_report(app, public_id="ISS-FILT02", status="new")
    resp = admin_client.get("/admin/issues?kind=reports&status=resolved")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ISS-FILT01" in body
    assert "ISS-FILT02" not in body


def test_list_invalid_kind_falls_back_to_reports(admin_client, app):
    _make_report(app, public_id="ISS-FALL01", title="should appear")
    resp = admin_client.get("/admin/issues?kind=garbage")
    assert resp.status_code == 200
    assert "ISS-FALL01" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Detail views
# ---------------------------------------------------------------------------


def test_report_detail_renders_with_logs_and_links(admin_client, app):
    exc_id = _make_exception(app, exception_message="linked-exc", fingerprint="fp-detail")
    _make_report(
        app,
        public_id="ISS-DET001",
        title="detail test",
        session_logs_json=json.dumps([{"lvl": "INFO", "msg": "log line one", "logger": "x", "func": "f", "line": 1}]),
        linked_exception_log_ids=json.dumps([exc_id]),
    )
    resp = admin_client.get("/admin/issues/reports/ISS-DET001")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "detail test" in body
    assert "log line one" in body
    assert "linked-exc" in body  # the linked exception's message


def test_report_detail_404_for_unknown_id(admin_client):
    resp = admin_client.get("/admin/issues/reports/ISS-NOPE99")
    assert resp.status_code == 404


def test_exception_detail_renders(admin_client, app):
    exc_id = _make_exception(
        app,
        exception_type="DetailErr",
        exception_message="detail-marker",
        traceback="Traceback (most recent call last):\n  ...",
        context_json=json.dumps({"path": "/x", "method": "GET"}),
        fingerprint="fp-exc-detail",
    )
    resp = admin_client.get(f"/admin/issues/exceptions/{exc_id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "DetailErr" in body
    assert "detail-marker" in body


def test_exception_detail_lists_related_reports(admin_client, app):
    exc_id = _make_exception(app, fingerprint="fp-rel-test")
    _make_report(
        app,
        public_id="ISS-REL001",
        title="references-the-exception",
        linked_exception_log_ids=json.dumps([exc_id]),
    )
    resp = admin_client.get(f"/admin/issues/exceptions/{exc_id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ISS-REL001" in body
    assert "references-the-exception" in body


# ---------------------------------------------------------------------------
# Retry endpoints
# ---------------------------------------------------------------------------


def test_report_retry_requeues_failed_row(admin_client, app, monkeypatch):
    captured = {"calls": 0}
    from utils import exception_tracker

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"number": 42, "html_url": "https://github.com/owner/repo/issues/42"}).encode("utf-8")

        def getcode(self):
            return 201

    def _fake(req, timeout=10):
        captured["calls"] += 1
        return _Resp()

    monkeypatch.setenv("GITHUB_ISSUE_ON_EXCEPTION_ENABLED", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(exception_tracker.urlrequest, "urlopen", _fake)

    row_id, public_id = _make_report(app, public_id="ISS-RETRY1", github_sync_status="failed", github_sync_error="prev error")

    resp = admin_client.post(f"/admin/issues/reports/{row_id}/retry")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] in ("queued", "already_synced")

    with app.app_context():
        row = db.session.get(IssueReport, row_id)
        assert row.github_sync_status == "synced"
        assert row.github_issue_number == 42


def test_report_retry_returns_already_synced(admin_client, app):
    row_id, _ = _make_report(
        app,
        public_id="ISS-DONE01",
        github_sync_status="synced",
        github_issue_number=99,
        github_issue_url="https://github.com/owner/repo/issues/99",
    )
    resp = admin_client.post(f"/admin/issues/reports/{row_id}/retry")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "already_synced"


def test_report_retry_404_for_unknown_id(admin_client):
    resp = admin_client.post("/admin/issues/reports/9999999/retry")
    assert resp.status_code == 404


def test_exception_retry_requeues_failed_row(admin_client, app, monkeypatch):
    from utils import exception_tracker

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"number": 7, "html_url": "https://github.com/owner/repo/issues/7"}).encode("utf-8")

        def getcode(self):
            return 201

    monkeypatch.setenv("GITHUB_ISSUE_ON_EXCEPTION_ENABLED", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(exception_tracker.urlrequest, "urlopen", lambda req, timeout=10: _Resp())

    exc_id = _make_exception(app, fingerprint="fp-retry-exc", github_sync_status="failed")

    resp = admin_client.post(f"/admin/issues/exceptions/{exc_id}/retry")
    assert resp.status_code == 200
    assert resp.get_json()["status"] in ("queued", "already_synced")

    with app.app_context():
        row = db.session.get(ExceptionLog, exc_id)
        assert row.github_issue_number == 7
        assert row.github_sync_status == "synced"


# ---------------------------------------------------------------------------
# Manual sync
# ---------------------------------------------------------------------------


def test_manual_sync_updates_status_from_github_labels(admin_client, app, monkeypatch):
    _make_report(
        app,
        public_id="ISS-SYNC01",
        github_issue_number=10,
        github_issue_url="https://github.com/owner/repo/issues/10",
        github_sync_status="synced",
        status="new",
    )

    monkeypatch.setenv("GITHUB_ISSUE_ON_EXCEPTION_ENABLED", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    def _fake_get_issue(number, repository=None):
        return {
            "state": "open",
            "labels": [{"name": "status:in-progress"}],
        }

    from services import github_issues
    monkeypatch.setattr(github_issues, "get_issue", _fake_get_issue)
    monkeypatch.setattr(github_issues, "is_enabled", lambda: True)

    resp = admin_client.post("/admin/issues/sync")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["synced"]["reports"] >= 1

    with app.app_context():
        row = IssueReport.query.filter_by(public_id="ISS-SYNC01").first()
        assert row.status == "in_progress"
        assert row.github_last_synced_at is not None


def test_manual_sync_returns_400_when_disabled(admin_client, monkeypatch):
    from services import github_issues
    monkeypatch.setattr(github_issues, "is_enabled", lambda: False)
    resp = admin_client.post("/admin/issues/sync")
    assert resp.status_code == 400


def test_manual_sync_handles_closed_state(admin_client, app, monkeypatch):
    exc_id = _make_exception(
        app,
        fingerprint="fp-sync-close",
        github_issue_number=55,
        github_issue_url="https://github.com/owner/repo/issues/55",
        github_sync_status="synced",
    )
    monkeypatch.setenv("GITHUB_ISSUE_ON_EXCEPTION_ENABLED", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    def _fake_get_issue(number, repository=None):
        return {"state": "closed", "labels": []}

    from services import github_issues
    monkeypatch.setattr(github_issues, "get_issue", _fake_get_issue)
    monkeypatch.setattr(github_issues, "is_enabled", lambda: True)

    resp = admin_client.post("/admin/issues/sync")
    assert resp.status_code == 200

    with app.app_context():
        row = db.session.get(ExceptionLog, exc_id)
        assert row.resolved is True
        assert row.resolved_by == "github-sync"


def test_manual_sync_requires_admin(authenticated_client):
    resp = authenticated_client.post("/admin/issues/sync")
    assert resp.status_code == 403
