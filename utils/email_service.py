"""
email_service.py — Resend-backed transactional email sender for SimCricketX.

Config (config/config.yaml):
  resend:
    token: re_...                          # Resend API key
  email:
    from_address: "SimCricketX <no-reply@simcricketx.app>"
    support_email: d6mr07@gmail.com
    app_url: https://simcricketx.app

Environment variable fallbacks:
  RESEND_API_KEY   — overrides resend.token
"""

import logging
import os
from datetime import datetime, timezone

import resend

log = logging.getLogger(__name__)

# Resend template IDs (created from email_templates/ HTML files)
_TMPL_EMAIL_VERIFY  = "cf85ee2c-a4bb-41be-aefb-161d993b1b69"
_TMPL_PASSWORD_RESET = "e82787f5-1ff5-4e4d-a690-d4f5bef91395"
_TMPL_ACCOUNT_DELETION = "d081688a-5cab-41fe-a715-10dd65ceec5a"


# ── Config helpers ─────────────────────────────────────────────────────────────

def _cfg() -> dict:
    try:
        from utils.helpers import load_config
        return load_config()
    except Exception:
        return {}


def _api_key() -> str:
    return os.environ.get("RESEND_API_KEY", "")


def _from_addr() -> str:
    return _cfg().get("email", {}).get(
        "from_address", "SimCricketX <no-reply@simcricketx.app>"
    )


def _support_email() -> str:
    return _cfg().get("email", {}).get("support_email", "d6mr07@gmail.com")


def _app_url() -> str:
    return _cfg().get("email", {}).get("app_url", "https://simcricketx.app")


# ── Core send functions ────────────────────────────────────────────────────────

def send_email_via_template(to: str, template_id: str, variables: dict) -> bool:
    """Send a transactional email using a Resend template ID.

    Args:
        to:          recipient email address
        template_id: Resend template ID (e.g. 'cf85ee2c-...')
        variables:   dict of variable substitutions for the template
    """
    api_key = _api_key()
    if not api_key:
        log.error("[Email] Resend API key not configured — template email not sent to %s", to)
        return False
    try:
        resend.api_key = api_key
        resend.Emails.send({
            "from": _from_addr(),
            "to": [to],
            "template_id": template_id,
            "variables": variables,
        })
        log.info("[Email] Template %s sent to %s", template_id, to)
        return True
    except Exception as exc:
        log.error("[Email] Failed to send template %s to %s: %s", template_id, to, exc)
        return False


def send_email(to: str, subject: str, html: str) -> bool:
    """Low-level send via Resend with inline HTML. Returns True on success."""
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
    ttl_minutes: int = 10,
) -> bool:
    """
    Send email address verification after registration.

    Args:
        to:                 recipient email address
        display_name:       user's chosen display name
        verification_link:  full URL with signed token (e.g. /verify-email?token=...)
        ttl_minutes:        minutes until the link expires (shown to user)
    """
    return send_email_via_template(
        to=to,
        template_id=_TMPL_EMAIL_VERIFY,
        variables={
            "display_name": display_name,
            "verification_link": verification_link,
            "ttl_minutes": str(ttl_minutes),
            "year": str(datetime.now(timezone.utc).year),
            "app_url": _app_url(),
            "support_email": _support_email(),
        },
    )


def send_password_reset_email(
    to: str,
    display_name: str,
    reset_link: str,
    ttl_minutes: int = 10,
) -> bool:
    """
    Send a password reset link for the forgot-password flow.

    Args:
        to:           recipient email address
        display_name: user's chosen display name
        reset_link:   full URL with signed reset token (e.g. /reset-password?token=...)
        ttl_minutes:  minutes until the link expires (shown to user)
    """
    return send_email_via_template(
        to=to,
        template_id=_TMPL_PASSWORD_RESET,
        variables={
            "display_name": display_name,
            "reset_link": reset_link,
            "ttl_minutes": str(ttl_minutes),
            "year": str(datetime.now(timezone.utc).year),
            "app_url": _app_url(),
            "support_email": _support_email(),
        },
    )


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
    return send_email_via_template(
        to=to,
        template_id=_TMPL_ACCOUNT_DELETION,
        variables={
            "display_name": display_name,
            "deletion_date": deletion_date,
            "year": str(datetime.now(timezone.utc).year),
            "app_url": _app_url(),
            "support_email": _support_email(),
        },
    )
