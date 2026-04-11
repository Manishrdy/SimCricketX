"""PII / secret scrubbing for outbound GitHub issues.

Anything in this module is best-effort defense in depth: tracebacks and
context dictionaries can contain emails, IP addresses, tokens, or other
sensitive material that we never want to leak into a (possibly public)
GitHub repository.

Rules are intentionally simple regex / key-name based — perfect filtering
is impossible, but we want to catch the obvious leaks.
"""

from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
)

# IPv4 — keep loopback / private RFC1918 readable in logs is fine for an
# operator, but to be safe we strip everything that *looks* like an IP.
_IPV4_RE = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
)

# IPv6 — handles both full form (2001:db8:...) and the `::` zero-compression
# shortcut (2001:db8::1, ::1, fe80::, etc.). Allowing 0-length hex groups
# inside the repetition is what lets `::` count as two empty segments.
_IPV6_RE = re.compile(
    r"\b(?:[A-Fa-f0-9]{0,4}:){2,7}[A-Fa-f0-9]{0,4}\b",
)

# GitHub tokens, OpenAI keys, generic bearer-style secrets.
_TOKEN_RES = [
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"ghs_[A-Za-z0-9]{20,}"),
    re.compile(r"ghu_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
]

# Authorization-style header lines that may show up in tracebacks /
# request dumps.
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+",
)

# Keys whose *values* should always be redacted regardless of content.
_SENSITIVE_KEY_NAMES = {
    "password",
    "passwd",
    "pwd",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "client_secret",
    "api_key",
    "apikey",
    "session_id",
    "sessionid",
    "csrf_token",
    "csrf",
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "private_key",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scrub_text(text: str | None) -> str:
    """Redact emails / IPs / tokens from a free-form string.

    Returns an empty string if `text` is None.
    """
    if not text:
        return ""

    cleaned = text

    # Tokens first — they may contain "@" or dots that the email regex
    # would otherwise eat.
    for pattern in _TOKEN_RES:
        cleaned = pattern.sub("<redacted-token>", cleaned)

    cleaned = _AUTH_HEADER_RE.sub(r"\1<redacted-auth>", cleaned)
    cleaned = _EMAIL_RE.sub("<email>", cleaned)
    cleaned = _IPV6_RE.sub("<ip>", cleaned)
    cleaned = _IPV4_RE.sub("<ip>", cleaned)

    return cleaned


def scrub_dict(value: Any) -> Any:
    """Recursively scrub a dict / list / scalar.

    Sensitive *keys* have their values replaced with `<redacted>`.
    Other string values are passed through `scrub_text`.
    """
    if isinstance(value, dict):
        scrubbed = {}
        for k, v in value.items():
            key_str = str(k).strip().lower()
            if key_str in _SENSITIVE_KEY_NAMES:
                scrubbed[k] = "<redacted>"
            else:
                scrubbed[k] = scrub_dict(v)
        return scrubbed

    if isinstance(value, (list, tuple)):
        scrubbed_seq = [scrub_dict(item) for item in value]
        return type(value)(scrubbed_seq) if isinstance(value, tuple) else scrubbed_seq

    if isinstance(value, str):
        return scrub_text(value)

    return value


def scrub_traceback(tb_text: str | None) -> str:
    """Scrub a Python traceback string.

    For now this is the same as `scrub_text`. Kept as a distinct entry point
    so we can tighten the rules later (e.g. drop frame-local repr lines)
    without changing every caller.
    """
    return scrub_text(tb_text)
