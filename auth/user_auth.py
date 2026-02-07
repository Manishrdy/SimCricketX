
import logging
from datetime import datetime, timezone
from werkzeug.security import check_password_hash, generate_password_hash
from database.models import User
from database import db
import socket

# --- Helper Functions ---

def get_ip_address() -> str:
    try:
        hostname = socket.gethostname()
        return socket.gethostbyname(hostname)
    except Exception:
        return ""

# C4: Minimum password length
MIN_PASSWORD_LENGTH = 6

# --- Core Auth Functions ---

def register_user(email: str, password: str) -> bool:
    """Register a new user in the database."""
    if not email or not password:
        return False

    # C4: Enforce minimum password length
    if len(password) < MIN_PASSWORD_LENGTH:
        logging.warning(f"[Auth] Password too short for {email} (min {MIN_PASSWORD_LENGTH} chars)")
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
            last_login=datetime.now(timezone.utc)
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
    """Delete user from database. C5: Only the user themselves can delete their account."""
    if not email: return False

    # C5: Authorization check - only allow self-deletion
    if requesting_user_email is not None and requesting_user_email != email.lower().strip():
        logging.warning(f"[Auth] Unauthorized delete attempt: {requesting_user_email} tried to delete {email}")
        return False

    email = email.lower().strip()
    user = db.session.get(User, email)

    if user:
        try:
            db.session.delete(user)
            db.session.commit()
            return True
        except Exception as e:
            db.session.rollback()
            logging.error(f"[Auth] Delete failed: {e}")
            return False

    return False

# Deprecated / Compatibility shims if needed
def load_credentials():
    """Deprecated: Returns empty dict or raises error. Use DB instead."""
    return {}
