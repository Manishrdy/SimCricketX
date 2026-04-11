"""Tests for routes/webhook_routes.py — PLAN-IR-001 Phase 3."""

import hashlib
import hmac
import json
import uuid
from datetime import datetime

import pytest

from database import db
from database.models import ExceptionLog, IssueReport, IssueWebhookEvent


WEBHOOK_SECRET = "test-webhook-secret-shhh"


def _sign(body_bytes: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), msg=body_bytes, digestmod=hashlib.sha256).hexdigest()


def _post_webhook(client, payload, *, secret=WEBHOOK_SECRET, delivery_id=None, event_type="issues", signature=None):
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event_type,
        "X-GitHub-Delivery": delivery_id or uuid.uuid4().hex,
    }
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    elif secret:
        headers["X-Hub-Signature-256"] = _sign(body, secret)
    return client.post("/webhooks/github/issues", data=body, headers=headers)


def _make_report(app, *, public_id, github_issue_number, status="new"):
    with app.app_context():
        row = IssueReport(
            public_id=public_id,
            user_email="testuser@example.com",
            category="bug",
            title=f"webhook target {public_id}",
            description="placeholder",
            github_issue_number=github_issue_number,
            github_issue_url=f"https://github.com/owner/repo/issues/{github_issue_number}",
            github_sync_status="synced",
            status=status,
        )
        db.session.add(row)
        db.session.commit()
        return row.id


def _make_exception(app, *, github_issue_number, fingerprint):
    with app.app_context():
        row = ExceptionLog(
            exception_type="WebhookErr",
            exception_message="webhook target",
            severity="error",
            source="backend",
            user_email="testuser@example.com",
            fingerprint=fingerprint,
            occurrence_count=1,
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
            timestamp=datetime.utcnow(),
            github_issue_number=github_issue_number,
            github_issue_url=f"https://github.com/owner/repo/issues/{github_issue_number}",
            github_sync_status="synced",
            resolved=False,
        )
        db.session.add(row)
        db.session.commit()
        return row.id


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def test_webhook_rejects_request_with_no_signature(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    body = json.dumps({"action": "opened", "issue": {"number": 1}}).encode("utf-8")
    resp = client.post(
        "/webhooks/github/issues",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "missing-sig-1",
        },
    )
    assert resp.status_code == 401
    assert "signature" in resp.get_json()["error"]


def test_webhook_rejects_bad_signature(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    resp = _post_webhook(
        client,
        {"action": "opened", "issue": {"number": 1}},
        signature="sha256=" + ("0" * 64),
        delivery_id="bad-sig-1",
    )
    assert resp.status_code == 401


def test_webhook_rejects_when_secret_not_configured(client, monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    resp = _post_webhook(
        client,
        {"action": "opened", "issue": {"number": 1}},
        delivery_id="no-secret-1",
    )
    # verify_webhook_signature returns False when no secret is set, so 401.
    assert resp.status_code == 401


def test_webhook_records_failed_signature_in_audit_table(client, app, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    resp = _post_webhook(
        client,
        {"action": "opened", "issue": {"number": 1}},
        signature="sha256=deadbeef",
        delivery_id="audit-bad-sig",
    )
    assert resp.status_code == 401

    with app.app_context():
        rec = IssueWebhookEvent.query.filter_by(delivery_id="audit-bad-sig").first()
        assert rec is not None
        assert rec.signature_valid is False
        assert rec.processed is False
        assert "signature" in (rec.processing_error or "")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_webhook_dedupes_repeat_delivery(client, app, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    payload = {"action": "opened", "issue": {"number": 99, "state": "open", "labels": []}}
    delivery_id = "dedupe-test-1"

    first = _post_webhook(client, payload, delivery_id=delivery_id)
    assert first.status_code == 200
    assert first.get_json()["status"] == "ok"

    second = _post_webhook(client, payload, delivery_id=delivery_id)
    assert second.status_code == 200
    assert second.get_json()["status"] == "duplicate"

    with app.app_context():
        rows = IssueWebhookEvent.query.filter_by(delivery_id=delivery_id).all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


def test_webhook_closed_action_marks_report_closed(client, app, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    _make_report(app, public_id="ISS-WHK001", github_issue_number=42, status="open")

    payload = {
        "action": "closed",
        "issue": {"number": 42, "state": "closed", "labels": []},
    }
    resp = _post_webhook(client, payload, delivery_id="whk-closed-1")
    assert resp.status_code == 200

    with app.app_context():
        row = IssueReport.query.filter_by(public_id="ISS-WHK001").first()
        assert row.status == "closed"
        assert row.github_last_synced_at is not None


def test_webhook_reopened_action_marks_report_open(client, app, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    _make_report(app, public_id="ISS-WHK002", github_issue_number=43, status="closed")

    payload = {
        "action": "reopened",
        "issue": {"number": 43, "state": "open", "labels": []},
    }
    resp = _post_webhook(client, payload, delivery_id="whk-reopen-1")
    assert resp.status_code == 200

    with app.app_context():
        row = IssueReport.query.filter_by(public_id="ISS-WHK002").first()
        assert row.status == "open"


def test_webhook_label_in_progress_overrides_state(client, app, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    _make_report(app, public_id="ISS-WHK003", github_issue_number=44, status="new")

    payload = {
        "action": "labeled",
        "issue": {
            "number": 44,
            "state": "open",
            "labels": [{"name": "status:in-progress"}],
        },
    }
    resp = _post_webhook(client, payload, delivery_id="whk-label-1")
    assert resp.status_code == 200

    with app.app_context():
        row = IssueReport.query.filter_by(public_id="ISS-WHK003").first()
        assert row.status == "in_progress"


def test_webhook_label_deferred_overrides_state(client, app, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    _make_report(app, public_id="ISS-WHK004", github_issue_number=45, status="new")

    payload = {
        "action": "labeled",
        "issue": {
            "number": 45,
            "state": "open",
            "labels": [{"name": "status:deferred"}, {"name": "type:bug"}],
        },
    }
    resp = _post_webhook(client, payload, delivery_id="whk-label-2")
    assert resp.status_code == 200

    with app.app_context():
        row = IssueReport.query.filter_by(public_id="ISS-WHK004").first()
        assert row.status == "deferred"


def test_webhook_label_resolved_marks_report_resolved(client, app, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    _make_report(app, public_id="ISS-WHK005", github_issue_number=46, status="new")

    payload = {
        "action": "labeled",
        "issue": {
            "number": 46,
            "state": "open",
            "labels": [{"name": "status:resolved"}],
        },
    }
    resp = _post_webhook(client, payload, delivery_id="whk-label-3")
    assert resp.status_code == 200

    with app.app_context():
        row = IssueReport.query.filter_by(public_id="ISS-WHK005").first()
        assert row.status == "resolved"


# ---------------------------------------------------------------------------
# ExceptionLog mirror
# ---------------------------------------------------------------------------


def test_webhook_closed_marks_exception_resolved(client, app, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    exc_id = _make_exception(app, github_issue_number=77, fingerprint="fp-webhook-close")

    payload = {
        "action": "closed",
        "issue": {"number": 77, "state": "closed", "labels": []},
    }
    resp = _post_webhook(client, payload, delivery_id="whk-exc-close-1")
    assert resp.status_code == 200

    with app.app_context():
        row = db.session.get(ExceptionLog, exc_id)
        assert row.resolved is True
        assert row.resolved_by == "github-webhook"
        assert row.resolved_at is not None


def test_webhook_reopened_marks_exception_unresolved(client, app, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    exc_id = _make_exception(app, github_issue_number=78, fingerprint="fp-webhook-reopen")
    with app.app_context():
        row = db.session.get(ExceptionLog, exc_id)
        row.resolved = True
        row.resolved_at = datetime.utcnow()
        row.resolved_by = "previous"
        db.session.commit()

    payload = {
        "action": "reopened",
        "issue": {"number": 78, "state": "open", "labels": []},
    }
    resp = _post_webhook(client, payload, delivery_id="whk-exc-reopen-1")
    assert resp.status_code == 200

    with app.app_context():
        row = db.session.get(ExceptionLog, exc_id)
        assert row.resolved is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_webhook_ignores_non_issues_event(client, app, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    payload = {"action": "opened", "pull_request": {"number": 99}}
    resp = _post_webhook(client, payload, event_type="pull_request", delivery_id="non-issue-1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["processed"] is False

    with app.app_context():
        rec = IssueWebhookEvent.query.filter_by(delivery_id="non-issue-1").first()
        assert rec is not None
        assert rec.signature_valid is True
        assert "ignored" in (rec.processing_error or "")


def test_webhook_handles_unknown_issue_number_gracefully(client, app, monkeypatch):
    """Webhook for an issue number not present locally should still ack 200."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    payload = {
        "action": "closed",
        "issue": {"number": 999999, "state": "closed", "labels": []},
    }
    resp = _post_webhook(client, payload, delivery_id="unknown-issue-1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["processed"] is True

    with app.app_context():
        rec = IssueWebhookEvent.query.filter_by(delivery_id="unknown-issue-1").first()
        assert rec is not None
        assert rec.processed is True


def test_webhook_missing_delivery_id_returns_400(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    body = json.dumps({"action": "opened", "issue": {"number": 1}}).encode("utf-8")
    resp = client.post(
        "/webhooks/github/issues",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert resp.status_code == 400
    assert "Delivery" in resp.get_json()["error"]


def test_webhook_persists_audit_row_on_success(client, app, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    payload = {
        "action": "labeled",
        "issue": {
            "number": 200,
            "state": "open",
            "labels": [{"name": "status:in-progress"}],
            "title": "ext title",
            "html_url": "https://github.com/owner/repo/issues/200",
        },
        "sender": {"login": "octocat"},
    }
    resp = _post_webhook(client, payload, delivery_id="audit-success-1")
    assert resp.status_code == 200

    with app.app_context():
        rec = IssueWebhookEvent.query.filter_by(delivery_id="audit-success-1").first()
        assert rec is not None
        assert rec.signature_valid is True
        assert rec.processed is True
        assert rec.event_type == "issues"
        assert rec.action == "labeled"
        assert rec.github_issue_number == 200
        assert rec.payload_json is not None
        # Trimmed payload should still contain the essentials.
        trimmed = json.loads(rec.payload_json)
        assert trimmed["issue"]["number"] == 200
        assert "status:in-progress" in trimmed["issue"]["labels"]
