"""Shared authentication / authorization decorators.

Extracted from app.py so blueprints (and any service modules) can import them
without going through the create_app() factory.
"""

from functools import wraps
import logging

from flask import jsonify, redirect, url_for
from flask_login import current_user


_logger = logging.getLogger("SimCricketX")


def admin_required(f):
    """Require an authenticated admin user.

    - Anonymous users are redirected to the login page.
    - Authenticated non-admins receive a 403 JSON response.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('login'))
        if not getattr(current_user, 'is_admin', False):
            try:
                _logger.warning(f"[Admin] Unauthorized access attempt by {current_user.id}")
            except Exception:
                pass
            return jsonify({"error": "Forbidden: Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated_function
