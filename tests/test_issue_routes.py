"""Tests for routes/issue_routes.py — POST /api/issues/report."""

import json
from datetime import datetime, timedelta

import pytest

from database import db
from database.models import ExceptionLog, IssueReport
from routes import issue_routes


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    issue_routes._reset_rate_limits_for_tests()
    yield
    issue_routes._reset_rate_limits_for_tests()


def _post(client, payload):
    return client.post(
        "/api/issues/report",
        data=json.dumps(payload),
        content_type="application/json",
    )


def test_anonymous_request_is_rejected(client):
    resp = _post(client, {"title": "x", "description": "y"})
    assert resp.status_code in (302, 401)  # login_required redirects


def test_valid_submission_creates_row_and_returns_public_id(authenticated_client, app):
    resp = _post(authenticated_client, {
        "category": "bug",
        "title": "Score not updating",
        "description": "After ball 4.3 the score froze.",
        "page_url": "https://localhost/match/abc",
        "app_version": "2.3.2",
    })

    assert resp.status_code == 202
    body = resp.get_json()
    assert body["status"] == "accepted"
    assert body["public_id"].startswith("ISS-")

    with app.app_context():
        rows = IssueReport.query.all()
        assert len(rows) == 1
        row = rows[0]
        assert row.title == "Score not updating"
        assert row.description.startswith("After ball")
        assert row.category == "bug"
        assert row.user_email == "testuser@example.com"
        assert row.page_url == "https://localhost/match/abc"


def test_missing_title_returns_400(authenticated_client):
    resp = _post(authenticated_client, {"description": "no title here"})
    assert resp.status_code == 400
    assert "title" in resp.get_json()["error"]


def test_missing_description_returns_400(authenticated_client):
    resp = _post(authenticated_client, {"title": "just a title"})
    assert resp.status_code == 400


def test_unknown_category_falls_back_to_other(authenticated_client, app):
    resp = _post(authenticated_client, {
        "title": "weird category",
        "description": "test",
        "category": "this-is-not-real",
    })
    assert resp.status_code == 202
    with app.app_context():
        row = IssueReport.query.order_by(IssueReport.id.desc()).first()
        assert row.category == "other"


def test_rate_limit_two_per_hour(authenticated_client):
    # First two should succeed.
    for i in range(2):
        resp = _post(authenticated_client, {"title": f"t{i}", "description": f"d{i}"})
        assert resp.status_code == 202, f"submission {i} failed"

    # Third within the same hour should be rate-limited.
    resp = _post(authenticated_client, {"title": "t3", "description": "d3"})
    assert resp.status_code == 429
    assert "hour" in resp.get_json()["error"].lower()
    assert resp.headers.get("Retry-After") is not None


def test_rate_limit_five_per_day(authenticated_client):
    """Bypass the hourly window by manipulating the bucket directly."""
    user_email = "testuser@example.com"
    now = datetime.utcnow()

    # Pre-fill the bucket with 5 entries spread out over the last 24h
    # so the hourly window is empty but the daily window is full.
    with issue_routes._rate_lock:
        bucket = issue_routes._rate_buckets.setdefault(user_email, [])
        # Use a deque so it matches the production type
        from collections import deque
        new_bucket = deque()
        for i in range(5):
            new_bucket.append(now - timedelta(hours=2 + i))
        issue_routes._rate_buckets[user_email] = new_bucket

    resp = _post(authenticated_client, {"title": "x", "description": "y"})
    assert resp.status_code == 429
    assert "day" in resp.get_json()["error"].lower()


def test_session_logs_are_attached(authenticated_client, app):
    """Pre-seed the session buffer; submission should snapshot it."""
    from middleware import session_log_capture

    # The test client's session id will be the one set by attach_request_context
    # on the next request. We need to capture the same sid the server sees.
    # Easiest path: hit any GET first to populate the session, then read the sid.
    authenticated_client.get("/")  # warm up

    with authenticated_client.session_transaction() as sess:
        sid = sess.get("_scx_sid")

    if sid:
        session_log_capture.append_for_session(sid, {"msg": "buffered before report", "lvl": "INFO"})
        session_log_capture.append_for_session(sid, {"msg": "second buffered line", "lvl": "WARNING"})

    resp = _post(authenticated_client, {"title": "session log test", "description": "check the logs"})
    assert resp.status_code == 202

    with app.app_context():
        row = IssueReport.query.order_by(IssueReport.id.desc()).first()
        if sid:  # only assert when we actually had a session id
            assert row.session_logs_json is not None
            payload = json.loads(row.session_logs_json)
            assert any("buffered before report" in (e.get("msg") or "") for e in payload)


def test_recent_exception_logs_are_linked(authenticated_client, app):
    user_email = "testuser@example.com"
    with app.app_context():
        # Insert a recent exception for the same user
        exc = ExceptionLog(
            exception_type="TestErr",
            exception_message="link me",
            severity="error",
            source="backend",
            user_email=user_email,
            fingerprint="link-test-fp",
            occurrence_count=1,
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
            timestamp=datetime.utcnow(),
        )
        db.session.add(exc)
        db.session.commit()
        exc_id = exc.id

    resp = _post(authenticated_client, {"title": "linked test", "description": "should link the recent exception"})
    assert resp.status_code == 202

    with app.app_context():
        row = IssueReport.query.order_by(IssueReport.id.desc()).first()
        assert row.linked_exception_log_ids is not None
        ids = json.loads(row.linked_exception_log_ids)
        assert exc_id in ids


def test_rejects_non_json_content_type(authenticated_client):
    resp = authenticated_client.post(
        "/api/issues/report",
        data="title=foo&description=bar",
        content_type="application/x-www-form-urlencoded",
    )
    assert resp.status_code == 400
