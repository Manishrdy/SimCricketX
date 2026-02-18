from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from werkzeug.security import check_password_hash, generate_password_hash
from database.models import User, AdminAuditLog
from database import db
import json

# --- Helper Functions ---

def get_ip_address() -> str:
    try:
        from flask import request
        return (request.remote_addr or "").strip()
    except Exception:
        return ""

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
    """Record an admin action in the persistent audit log."""
    try:
        entry = AdminAuditLog(
            admin_email=admin_email,
            action=action,
            target=target,
            details=details,
            ip_address=ip_address or get_ip_address(),
            timestamp=datetime.now(timezone.utc)
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logging.error(f"[AuditLog] Failed to record action: {e}")

# --- Core Auth Functions ---

def register_user(email: str, password: str, display_name: str | None = None) -> bool:
    """Register a new user in the database."""
    if not email or not password:
        return False

    display_name = display_name.strip() if display_name else None

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
        new_user = User(
            id=email,
            password_hash=generate_password_hash(password),
            ip_address=get_ip_address(),
            last_login=datetime.now(timezone.utc),
            display_name=display_name
        )

        db.session.add(new_user)
        db.session.commit()
        logging.info(f"[Auth] Registered user: {email}")
        return True
    except Exception as e:
        db.session.rollback()
        logging.error(f"[Auth] Registration failed for {email}: {e}")
        return False

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
        db.session.delete(user)
        db.session.commit()
        if requesting_user_email:
            log_admin_action(requesting_user_email, 'delete_user', email, 'User account and all data deleted')
        return True
    except Exception as e:
        db.session.rollback()
        logging.error(f"[Auth] Delete failed: {e}")
        return False

# --- Admin Management Functions ---

def update_user_email(old_email: str, new_email: str, admin_email: str = None) -> tuple[bool, str]:
    """Update a user's email address using transactional UPDATE.

    Updates the user's primary key and all foreign key references
    within a single transaction for data integrity.
    """
    if not old_email or not new_email:
        return False, "Both old and new email are required"

    old_email = old_email.lower().strip()
    new_email = new_email.lower().strip()

    if old_email == new_email:
        return False, "New email is the same as the current email"

    # Check if new email is already taken
    if db.session.get(User, new_email):
        return False, f"Email {new_email} is already registered"

    # Get the user to update
    user = db.session.get(User, old_email)
    if not user:
        return False, f"User {old_email} not found"

    try:
        from sqlalchemy import text

        # Use raw SQL to update the primary key and all FK references in one transaction
        # This is safer than delete+recreate and keeps user-linked records consistent.
        db.session.execute(text("UPDATE teams SET user_id = :new WHERE user_id = :old"), {"new": new_email, "old": old_email})
        db.session.execute(text("UPDATE matches SET user_id = :new WHERE user_id = :old"), {"new": new_email, "old": old_email})
        db.session.execute(text("UPDATE tournaments SET user_id = :new WHERE user_id = :old"), {"new": new_email, "old": old_email})
        db.session.execute(text("UPDATE active_sessions SET user_id = :new WHERE user_id = :old"), {"new": new_email, "old": old_email})
        db.session.execute(text("UPDATE blocked_ips SET blocked_by = :new WHERE blocked_by = :old"), {"new": new_email, "old": old_email})
        db.session.execute(text("UPDATE failed_login_attempts SET email = :new WHERE email = :old"), {"new": new_email, "old": old_email})
        db.session.execute(text("UPDATE admin_audit_log SET admin_email = :new WHERE admin_email = :old"), {"new": new_email, "old": old_email})

        # Update the primary key last
        db.session.execute(text("UPDATE users SET id = :new WHERE id = :old"), {"new": new_email, "old": old_email})

        db.session.commit()

        actor = f" by {admin_email}" if admin_email else ""
        logging.info(f"[Admin] Email updated{actor}: {old_email} -> {new_email}")

        if admin_email:
            log_admin_action(admin_email, 'change_email', old_email, f"Changed to {new_email}")

        return True, f"Email successfully updated to {new_email}"

    except Exception as e:
        db.session.rollback()
        logging.error(f"[Admin] Email update failed: {e}")
        return False, f"Failed to update email: {str(e)}"

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
        db.session.rollback()
        logging.error(f"[Admin] Password reset failed for {email}: {e}")
        return False, f"Failed to reset password: {str(e)}"

# Deprecated / Compatibility shims if needed
def load_credentials():
    """Deprecated: Returns empty dict or raises error. Use DB instead."""
    return {}
