"""
Tests for utils/email_service.py.

Regression coverage for the production noise from issue #178: Resend rejects
recipients on known-undeliverable domains (e.g. example.com) with a
`ValidationError: Invalid \\`to\\` field ...` message. That's a garbage
recipient address, not a bug in this app, so it must not be filed as an
actionable GitHub issue via log_exception — it should be recorded as a data
anomaly instead, same as other bad-upstream-data cases.
"""

from unittest.mock import patch

import requests

from database.models import ExceptionLog
from utils.email_service import send_email


class _FakeUndeliverableError(Exception):
    def __str__(self):
        return (
            "Invalid `to` field. Please use our testing email address "
            "instead of domains like `example.com`. See our documentation "
            "for more information."
        )


class _FakeMissingFieldError(Exception):
    def __str__(self):
        return "Missing `subject` field."


def test_undeliverable_recipient_logs_anomaly_not_issue(app, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    with patch("utils.email_service.resend.Emails.send", side_effect=_FakeUndeliverableError()):
        result = send_email("bogus@example.com", "Subject", "<p>hi</p>")

    assert result is False
    rows = ExceptionLog.query.all()
    assert len(rows) == 1
    assert rows[0].exception_type == "undeliverable_email_recipient"
    assert rows[0].github_sync_status == "skipped"


def test_other_send_failures_still_log_exception(app, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    with patch("utils.email_service.resend.Emails.send", side_effect=_FakeMissingFieldError()):
        result = send_email("real@simcricketx.app", "Subject", "<p>hi</p>")

    assert result is False
    rows = ExceptionLog.query.all()
    assert len(rows) == 1
    assert rows[0].exception_type == "_FakeMissingFieldError"
    assert rows[0].github_sync_status == "pending"


def test_missing_api_key_skips_send_without_logging(app, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with patch("utils.email_service.resend.Emails.send") as mock_send:
        result = send_email("real@simcricketx.app", "Subject", "<p>hi</p>")

    assert result is False
    mock_send.assert_not_called()
    assert ExceptionLog.query.count() == 0

def test_send_email_success():
    with patch("utils.email_service._api_key", return_value="fake_key"):
        with patch("resend.Emails.send") as mock_send:
            mock_send.return_value = {"id": "123"}
            result = send_email("test@example.com", "Subject", "<html></html>")
            assert result is True
            assert mock_send.call_count == 1

def test_send_email_retry_on_timeout():
    with patch("utils.email_service._api_key", return_value="fake_key"):
        with patch("resend.Emails.send") as mock_send:
            # First 2 times raise ReadTimeout, then succeed
            mock_send.side_effect = [
                requests.exceptions.ReadTimeout("HTTPSConnectionPool(host='api.resend.com', port=443): Read timed out. (read timeout=30)"),
                requests.exceptions.ReadTimeout("HTTPSConnectionPool(host='api.resend.com', port=443): Read timed out. (read timeout=30)"),
                {"id": "123"}
            ]
            # Patch time.sleep so we don't actually wait
            with patch("time.sleep"):
                result = send_email("test@example.com", "Subject", "<html></html>")
            assert result is True
            assert mock_send.call_count == 3

def test_send_email_fails_after_max_retries():
    with patch("utils.email_service._api_key", return_value="fake_key"):
        with patch("resend.Emails.send") as mock_send:
            mock_send.side_effect = requests.exceptions.ReadTimeout("Timeout")
            with patch("time.sleep"):
                with patch("utils.email_service.log_exception") as mock_log_exc:
                    result = send_email("test@example.com", "Subject", "<html></html>")
            assert result is False
            assert mock_send.call_count == 3
            mock_log_exc.assert_called_once()
