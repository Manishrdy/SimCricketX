"""
email_service.py — Resend-backed transactional email sender for SimCricketX.

Config (config/config.yaml):
  resend:
    token: re_...                          # Resend API key
  email:
    from_address: "SimCricketX <no-reply@simcricketx.app>"
    support_email: support@simcricketx.app
    app_url: https://simcricketx.app

Environment variable fallbacks:
  RESEND_API_KEY   — overrides resend.token
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import resend
from jinja2 import Environment, FileSystemLoader, select_autoescape

log = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent.parent / "email_templates"


# ── Config helpers ─────────────────────────────────────────────────────────────

def _cfg() -> dict:
    try:
        from utils.helpers import load_config
        return load_config()
    except Exception:
        return {}


def _api_key() -> str:
    return (
        os.environ.get("RESEND_API_KEY")
        or _cfg().get("resend", {}).get("token", "")
    )


def _from_addr() -> str:
    return _cfg().get("email", {}).get(
        "from_address", "SimCricketX <no-reply@simcricketx.app>"
    )


def _support_email() -> str:
    return _cfg().get("email", {}).get("support_email", "support@simcricketx.app")


def _app_url() -> str:
    return _cfg().get("email", {}).get("app_url", "https://simcricketx.app")


# ── Template renderer ─────────────────────────────────────────────────────────

def _render(template_name: str, **kwargs) -> str:
    """Render a Jinja2 HTML email template from email_templates/."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    return env.get_template(template_name).render(
        support_email=_support_email(),
        app_url=_app_url(),
        year=datetime.now(timezone.utc).year,
        **kwargs,
    )


# ── Core send ─────────────────────────────────────────────────────────────────

def send_email(to: str, subject: str, html: str) -> bool:
    """Low-level send via Resend. Returns True on success."""
    api_key = _api_key()
    if not api_key:
        log.error("[Email] Resend API key not configured — email not sent to %s", to)
        return False
    try:
        resend.api_key = api_key
        resend.Emails.send({
            "from": _from_addr(),
            "to": [to],
            "subject": subject,
            "html": html,
        })
        log.info("[Email] '%s' sent to %s", subject, to)
        return True
    except Exception as exc:
        log.error("[Email] Failed to send '%s' to %s: %s", subject, to, exc)
        return False


# ── Public helpers ─────────────────────────────────────────────────────────────

def send_verification_email(
    to: str,
    display_name: str,
    verification_link: str,
    ttl_hours: int = 24,
) -> bool:
    """
    Send email address verification after registration.

    Args:
        to:                 recipient email address
        display_name:       user's chosen display name
        verification_link:  full URL with signed token (e.g. /verify-email?token=...)
        ttl_hours:          hours until the link expires (shown to user)
    """
    html = _render(
        "email_verify.html",
        display_name=display_name,
        verification_link=verification_link,
        ttl_hours=ttl_hours,
    )
    return send_email(to, "Verify your SimCricketX email address", html)


def send_password_reset_email(
    to: str,
    display_name: str,
    reset_link: str,
    ttl_minutes: int = 30,
) -> bool:
    """
    Send a password reset link for the forgot-password flow.

    Args:
        to:           recipient email address
        display_name: user's chosen display name
        reset_link:   full URL with signed reset token (e.g. /reset-password?token=...)
        ttl_minutes:  minutes until the link expires (shown to user)
    """
    html = _render(
        "password_reset.html",
        display_name=display_name,
        reset_link=reset_link,
        ttl_minutes=ttl_minutes,
    )
    return send_email(to, "Reset your SimCricketX password", html)


def send_account_deletion_email(
    to: str,
    display_name: str,
    deletion_date: str,
) -> bool:
    """
    Send account deletion confirmation immediately after the account is removed.

    Args:
        to:             email address (captured before deletion)
        display_name:   user's display name (captured before deletion)
        deletion_date:  human-readable date/time string, e.g. "March 2, 2026 at 14:35 UTC"
    """
    html = _render(
        "account_deletion.html",
        display_name=display_name,
        deletion_date=deletion_date,
    )
    return send_email(to, "Your SimCricketX account has been deleted", html)
