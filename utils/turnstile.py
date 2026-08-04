"""Cloudflare Turnstile server-side token verification."""

import os
from typing import Optional, Tuple

import requests as _http

_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def turnstile_enabled() -> bool:
    """True only when BOTH the public site key and the private secret key are set.

    These two env vars are a matched pair: the site key tells the browser to
    render the widget, the secret key tells the server to check it. If only one
    is configured, enforcing the check would reject every login/register
    attempt (no widget was ever shown to produce a token) — so a partial
    config is treated as fully disabled rather than half-enabled. This is the
    single source of truth; nothing else should read the two env vars directly.
    """
    return bool(os.environ.get("CF_TURNSTILE_SITE_KEY", "").strip()) and \
        bool(os.environ.get("CF_TURNSTILE_SECRET_KEY", "").strip())


def turnstile_config_warning() -> Optional[str]:
    """Return a diagnostic message if exactly one of the two keys is set, else None.

    Meant to be logged loudly at app startup so a mismatched pair is caught
    immediately instead of discovered via a wave of failed logins.
    """
    has_site = bool(os.environ.get("CF_TURNSTILE_SITE_KEY", "").strip())
    has_secret = bool(os.environ.get("CF_TURNSTILE_SECRET_KEY", "").strip())
    if has_site == has_secret:
        return None
    present = "CF_TURNSTILE_SITE_KEY" if has_site else "CF_TURNSTILE_SECRET_KEY"
    missing = "CF_TURNSTILE_SECRET_KEY" if has_site else "CF_TURNSTILE_SITE_KEY"
    return (
        f"Turnstile misconfigured: {present} is set but {missing} is not. "
        "Both must be set together (or both left unset) — a partial config would "
        "block every login/register attempt. Turnstile has been disabled for this "
        "boot to keep the site usable; fix the missing env var and redeploy."
    )


def verify_turnstile(token: str, remote_ip: Optional[str] = None) -> Tuple[bool, str]:
    """Verify a Turnstile response token with Cloudflare.

    Returns (True, "ok") on success.
    Returns (True, "test-mode") when SIMCRICKETX_TEST_MODE is enabled.
    Returns (True, "disabled") when Turnstile isn't fully configured (dev mode,
    or a mismatched key pair — see turnstile_enabled()).
    Returns (False, error_code) on failure.
    """
    if os.environ.get("SIMCRICKETX_TEST_MODE", "").strip().lower() in {"1", "true"}:
        return True, "test-mode"

    if not turnstile_enabled():
        return True, "disabled"
    if not token:
        return False, "missing-input-response"
    secret = os.environ.get("CF_TURNSTILE_SECRET_KEY", "").strip()
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
