"""Thin GitHub Issues API client.

Single source of truth for talking to GitHub. Used by:
  - utils/exception_tracker.py (auto exception → issue)
  - services/github_issue_queue.py (background worker)
  - (future) routes/issue_routes.py (user-submitted reports)
  - (future) routes/admin_issue_routes.py (manual sync, retry)
  - (future) /webhooks/github/issues (signature verification)

Uses urllib (stdlib) so we don't add a runtime dep. All network failures
are returned as None / False rather than raised — callers decide whether
that's a hard error.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def is_enabled() -> bool:
    """True when exception → GitHub issue automation is configured."""
    flag = _env("GITHUB_ISSUE_ON_EXCEPTION_ENABLED").lower()
    if flag not in {"1", "true", "yes"}:
        return False
    return bool(_env("GITHUB_TOKEN")) and bool(_env("GITHUB_REPOSITORY"))


def get_repository() -> str:
    """Repository in `owner/repo` format."""
    return _env("GITHUB_REPOSITORY")


def get_default_labels() -> list[str]:
    raw = _env("GITHUB_ISSUE_LABELS")
    return [item.strip() for item in raw.split(",") if item.strip()]


def get_default_assignees() -> list[str]:
    raw = _env("GITHUB_ISSUE_ASSIGNEES")
    return [item.strip() for item in raw.split(",") if item.strip()]


def get_title_prefix() -> str:
    return _env("GITHUB_ISSUE_TITLE_PREFIX", "[Auto Exception]") or "[Auto Exception]"


# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------


def _request(method: str, path: str, body: dict | None = None, timeout: int = 10) -> tuple[int | None, dict | None]:
    """Make an authenticated GitHub REST call.

    Returns `(status_code, parsed_json)` or `(None, None)` on network failure.
    """
    token = _env("GITHUB_TOKEN")
    if not token:
        return None, None

    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SimCricketX-IssueBot",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urlrequest.Request(url, data=data, headers=headers, method=method)

    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            return resp.getcode(), parsed
    except urlerror.HTTPError as e:
        try:
            raw = e.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {"error": str(e)}
        return e.code, parsed
    except (urlerror.URLError, TimeoutError, ValueError):
        return None, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_issue(
    *,
    title: str,
    body: str,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    repository: str | None = None,
) -> tuple[int | None, str | None, str | None]:
    """Create a GitHub issue.

    Returns `(issue_number, html_url, error_message)`. On success the error
    is None. On failure both number and url are None and the error contains
    a short reason for storage in `github_sync_error`.
    """
    repo = (repository or get_repository()).strip()
    if not repo:
        return None, None, "GITHUB_REPOSITORY not configured"

    payload: dict[str, Any] = {
        "title": (title or "")[:256],
        "body": body or "",
    }
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees

    status, parsed = _request("POST", f"/repos/{repo}/issues", body=payload)

    if status is None:
        return None, None, "network error"
    if status >= 300 or not isinstance(parsed, dict):
        msg = "unknown error"
        if isinstance(parsed, dict):
            msg = str(parsed.get("message") or parsed.get("error") or msg)
        return None, None, f"http {status}: {msg}"

    return parsed.get("number"), parsed.get("html_url"), None


def get_issue(number: int, *, repository: str | None = None) -> dict | None:
    """Fetch an issue's current state. Returns None on any failure."""
    repo = (repository or get_repository()).strip()
    if not repo or not number:
        return None
    status, parsed = _request("GET", f"/repos/{repo}/issues/{number}")
    if status is None or status >= 300:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------


def verify_webhook_signature(payload_bytes: bytes, signature_header: str | None) -> bool:
    """Verify an `X-Hub-Signature-256` header from a GitHub webhook.

    Returns True only when a webhook secret is configured AND the supplied
    HMAC matches. Used by the (future) /webhooks/github/issues endpoint.
    """
    secret = _env("GITHUB_WEBHOOK_SECRET")
    if not secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        msg=payload_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
