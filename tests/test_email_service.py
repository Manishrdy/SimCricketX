import pytest
from unittest.mock import patch
import resend
import requests
from utils.email_service import send_email

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
