"""Cloudflare Turnstile server-side token verification."""

import os
import requests as _http

_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def verify_turnstile(token: str, remote_ip: str | None = None) -> tuple[bool, str]:
    """Verify a Turnstile response token with Cloudflare.

    Returns (True, "ok") on success.
    Returns (True, "disabled") when CF_TURNSTILE_SECRET_KEY is not set (dev mode).
    Returns (False, error_code) on failure.
    """
    secret = os.environ.get("CF_TURNSTILE_SECRET_KEY", "")
    if not secret:
        return True, "disabled"
    if not token:
        return False, "missing-input-response"
    data: dict = {"secret": secret, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        resp = _http.post(_VERIFY_URL, data=data, timeout=5)
        resp.raise_for_status()
        j = resp.json()
        if j.get("success"):
            return True, "ok"
        codes = j.get("error-codes") or ["unknown"]
        return False, codes[0]
    except Exception as exc:
        return False, str(exc)
