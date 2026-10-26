from __future__ import annotations

import hashlib
import logging
import re
import secrets
from typing import Optional
from datetime import datetime, timedelta, timezone
from werkzeug.security import check_password_hash, generate_password_hash
from database.models import User
from database import db
import json
from utils.exception_tracker import log_exception
from utils.client_ip import get_client_ip


def _hash_token(raw_token: str) -> str:
    """Return a SHA-256 hex digest of the raw token for safe DB storage."""
    return hashlib.sha256(raw_token.encode()).hexdigest()

EMAIL_VERIFY_TTL_MINUTES = 10
PASSWORD_RESET_TTL_MINUTES = 360  # 6 hours

# --- Helper Functions ---

# C4: Unified password policy
MIN_PASSWORD_LENGTH = 8

def validate_password_policy(password: str) -> tuple[bool, str]:
    """Validate password against the unified security policy."""
    if not password:
        return False, "Password is required"
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter"
    if not re.search(r"[0-9]", password):
        return False, "Password must contain at least one digit"
    return True, ""

def log_admin_action(admin_email: str, action: str, target: str = None, details: str = None, ip_address: str = None):
    """Emit an operational message without storing database audit records."""
    logging.info("[AdminAction] actor=%r action=%r target=%r", admin_email, action, target)

# --- Core Auth Functions ---

def register_user(email: str, password: str, display_name: Optional[str] = None) -> bool:
    """Register a new user in the database."""
    if not email or not password:
        return False

    display_name = display_name.strip() if display_name else None
    from services import identity_signup as signup
    from services import identity_network as network
    from flask import g, request
    if network.enabled() and request.endpoint != 'admin_create_user':
        ok, message = validate_password_policy(password)
        if not ok:
            g.identity_signup_error = message
            return False
        if network.signup_block():
            g.identity_signup_error = 'Turn off your VPN, proxy or Private Relay before registering. You can request a private signup review if you need help.'
            if signup.enabled():
                try:
                    signup.record_attempt(signup.normalize_email(email), signup.Refused(['network_restricted']))
                except Exception:
                    logging.warning('Network-refused signup could not be recorded')
            return False
    if signup.enabled():
        from flask import g
        from utils.identity_rules import InvalidEmail
        ok, message = validate_password_policy(password)
        if not ok:
            g.identity_signup_error = message
            return False
        try:
            g.identity_signup_email = signup.create_user(email, password, display_name)
            return True
        except InvalidEmail as exc:
            g.identity_signup_error = signup.MESSAGES.get(exc.code, 'Enter a valid email address.')
        except signup.Refused as exc:
            g.identity_signup_error = signup.MESSAGES.get(exc.reasons[0], signup.MESSAGES['identity_unavailable'])
        except Exception:
            logging.warning('Signup checks unavailable; registration refused')
            g.identity_signup_error = signup.MESSAGES['unavailable']
        return False


    # C4: Enforce unified password policy
    ok, msg = validate_password_policy(password)
    if not ok:
        logging.warning(f"[Auth] Invalid password for {email}: {msg}")
        return False

    email = email.lower().strip()

    # Check existing
    if db.session.get(User, email):
        logging.warning(f"[Auth] User already exists: {email}")
        return False

    try:
        token = secrets.token_urlsafe(32)
        new_user = User(
            id=email,
            password_hash=generate_password_hash(password),
            ip_address=get_client_ip(),
            display_name=display_name,
            email_verified=False,
            email_verify_token=_hash_token(token),
            email_verify_token_expires=datetime.utcnow() + timedelta(minutes=EMAIL_VERIFY_TTL_MINUTES),
        )

        db.session.add(new_user)
        db.session.commit()
        logging.info(f"[Auth] Registered user: {email}")
        return True
    except Exception as e:
        log_exception(e)
        db.session.rollback()
        logging.error(f"[Auth] Registration failed for {email}: {e}")
        return False

def generate_email_verify_token(email: str) -> str | None:
    """Generate a fresh email verification token and persist its hash (10-min TTL).

    Stores SHA-256(token) in the DB; returns the raw token to send to the user.
    Call this for both initial registration and the resend flow.
    """
    email = email.lower().strip()
    user = db.session.get(User, email)
    if not user:
        return None
    token = secrets.token_urlsafe(32)
    user.email_verify_token = _hash_token(token)
    user.email_verify_token_expires = datetime.utcnow() + timedelta(minutes=EMAIL_VERIFY_TTL_MINUTES)
    try:
        db.session.commit()
        return token
    except Exception as e:
        log_exception(e)
        db.session.rollback()
        logging.error(f"[Auth] Failed to generate verify token for {email}: {e}")
        return None


def generate_password_reset_token(email: str) -> str | None:
    """Generate a fresh password-reset token and persist its hash (10-min TTL).

    Stores SHA-256(token) in the DB; returns the raw token to send to the user.
    Always call this regardless of whether the email exists to avoid timing attacks —
    the caller must not reveal whether a user was found.
    """
    email = email.lower().strip()
    user = db.session.get(User, email)
    if not user:
        return None
    token = secrets.token_urlsafe(32)
    user.reset_token = _hash_token(token)
    user.reset_token_expires = datetime.utcnow() + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES)
    try:
        db.session.commit()
        return token
    except Exception as e:
        log_exception(e)
        db.session.rollback()
        logging.error(f"[Auth] Failed to generate reset token for {email}: {e}")
        return None


def verify_user(email: str, password: str) -> bool:
    """Verify credentials against database."""
    if not email or not password:
        return False

    email = email.lower().strip()
    user = db.session.get(User, email)

    if user and user.password_hash:
        if check_password_hash(user.password_hash, password):
            # Update last login
            try:
                user.last_login = datetime.now(timezone.utc)
                db.session.commit()
            except Exception:
                log_exception(source="sqlite")
                db.session.rollback()
            return True

    logging.warning(f"[Auth] Failed login attempt for {email}")
    return False

def delete_user(email: str, requesting_user_email: str = None) -> bool:
    """Delete user from database. Admin can delete other users, users can self-delete."""
    if not email: return False

    email = email.lower().strip()
    user = db.session.get(User, email)

    if not user:
        return False

    # Prevent deleting admin accounts
    if user.is_admin:
        logging.warning(f"[Auth] Attempt to delete admin account: {email}")
        return False

    try:
        from services.identity_signup import mark_deleted
        mark_deleted(db.session, user)
        db.session.delete(user)
        db.session.commit()
        if requesting_user_email:
            log_admin_action(requesting_user_email, 'delete_user', email, 'User account and all data deleted')
        return True
    except Exception as e:
        log_exception(e)
        db.session.rollback()
        logging.error(f"[Auth] Delete failed: {e}")
        return False

# --- Admin Management Functions ---

def update_user_email(old_email: str, new_email: str, admin_email: str = None) -> tuple[bool, str]:
    """Retired immediate writer: all callers must use verified identity changes."""
    return False, "Use Identity → Email changes to verify ownership and preview the change."

def update_user_password(email: str, new_password: str, admin_email: str = None) -> tuple[bool, str]:
    """Reset a user's password (admin function)."""
    if not email or not new_password:
        return False, "Email and new password are required"

    # Enforce unified password policy
    ok, msg = validate_password_policy(new_password)
    if not ok:
        return False, msg

    email = email.lower().strip()
    user = db.session.get(User, email)

    if not user:
        return False, f"User {email} not found"

    try:
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()

        actor = f" by {admin_email}" if admin_email else ""
        logging.info(f"[Admin] Password reset{actor} for: {email}")

        if admin_email:
            log_admin_action(admin_email, 'reset_password', email, 'Password was reset')

        return True, "Password successfully reset"

    except Exception as e:
        log_exception(e)
        db.session.rollback()
        logging.error(f"[Admin] Password reset failed for {email}: {e}")
        return False, f"Failed to reset password: {str(e)}"

# Deprecated / Compatibility shims if needed
def load_credentials():
    """Deprecated: Returns empty dict or raises error. Use DB instead."""
    return {}
