"""
Tests for new authentication features:
  - Account lockout after N failed login attempts + admin unlock
  - Password reset token TTL changed to 6 hours
  - Remember-me checkbox (session permanence)
  - Concurrent session limit (strictly 1 active session)
  - User-visible login history (/account/login-history)
  - Account settings — display name change (/account/settings/display-name)
  - Account settings — email change with verification (/account/settings/request-email-change
    and /account/confirm-email-change)
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

from app import db
from database.models import (
    ActiveSession,
    FailedLoginAttempt,
    LoginHistory,
    Team,
    User,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post_login(client, email, password, remember_me=False):
    """POST to /login with the given credentials."""
    data = {"email": email, "password": password}
    if remember_me:
        data["remember_me"] = "on"
    return client.post("/login", data=data, follow_redirects=False)


def _make_locked_user(app, email="locked@example.com"):
    """Create a user that is currently locked out."""
    user = User(
        id=email,
        password_hash=generate_password_hash("Password123!"),
        display_name="Locked User",
        email_verified=True,
        lockout_until=datetime.utcnow() + timedelta(minutes=30),
        lockout_count=5,
        lockout_window_start=datetime.utcnow() - timedelta(minutes=5),
    )
    with app.app_context():
        db.session.add(user)
        db.session.commit()
    return email


# ===========================================================================
# 1. Account Lockout
# ===========================================================================

class TestAccountLockout:
    """Account locks after N consecutive failed attempts."""

    def test_failed_attempts_increment_lockout_count(self, client, regular_user, app):
        """Each bad-password attempt increments lockout_count on the user."""
        for _ in range(3):
            client.post("/login", data={
                "email": regular_user.email,
                "password": "WrongPass999!",
            })

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.lockout_count >= 3

    def test_lockout_triggered_after_max_attempts(self, client, regular_user, app):
        """After reaching the threshold the account is locked (lockout_until is set)."""
        # 5 failed attempts (default threshold)
        for _ in range(5):
            client.post("/login", data={
                "email": regular_user.email,
                "password": "WrongPass999!",
            })

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.lockout_until is not None
            assert user.lockout_until > datetime.utcnow()

    def test_locked_account_shows_locked_error(self, client, app, regular_user):
        """A user whose account is locked gets a meaningful error on login."""
        # Lock the account directly in DB
        with app.app_context():
            user = db.session.get(User, regular_user.id)
            user.lockout_until = datetime.utcnow() + timedelta(minutes=30)
            user.lockout_count = 5
            db.session.commit()

        response = client.post("/login", data={
            "email": regular_user.email,
            "password": "Password123!",
        }, follow_redirects=True)

        assert response.status_code == 200
        body = response.data.lower()
        assert b"locked" in body or b"too many" in body

    def test_expired_lockout_auto_clears_on_login(self, client, app, regular_user):
        """A lockout whose expiry has passed is silently cleared; correct credentials succeed."""
        with app.app_context():
            user = db.session.get(User, regular_user.id)
            # Set lockout_until in the past
            user.lockout_until = datetime.utcnow() - timedelta(seconds=1)
            user.lockout_count = 5
            db.session.commit()

        # Login with correct password must succeed
        response = _post_login(client, regular_user.email, "Password123!")
        assert response.status_code == 302

        # lockout columns must be cleared
        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.lockout_until is None
            assert user.lockout_count == 0

    def test_successful_login_clears_lockout_count(self, client, app, regular_user):
        """A successful login resets lockout_count and lockout_until even if they were non-zero."""
        with app.app_context():
            user = db.session.get(User, regular_user.id)
            user.lockout_count = 3
            user.lockout_window_start = datetime.utcnow() - timedelta(minutes=5)
            db.session.commit()

        _post_login(client, regular_user.email, "Password123!")

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.lockout_count == 0
            assert user.lockout_until is None

    def test_failed_logins_recorded_in_failed_login_attempts(self, client, regular_user, app):
        """Every bad-credential attempt must create a FailedLoginAttempt row."""
        client.post("/login", data={
            "email": regular_user.email,
            "password": "Wrong!",
        })

        with app.app_context():
            count = FailedLoginAttempt.query.filter_by(email=regular_user.email).count()
            assert count >= 1


class TestAdminUnlockUser:
    """Admin can clear account lockout via POST /admin/users/<email>/unlock."""

    def test_admin_can_unlock_locked_user(self, admin_client, regular_user, app):
        """Admin unlock endpoint clears lockout_until and resets lockout_count."""
        with app.app_context():
            user = db.session.get(User, regular_user.id)
            user.lockout_until = datetime.utcnow() + timedelta(minutes=30)
            user.lockout_count = 5
            user.lockout_window_start = datetime.utcnow()
            db.session.commit()

        response = admin_client.post(
            f"/admin/users/{regular_user.id}/unlock",
            follow_redirects=False,
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data is not None
        assert "message" in data

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.lockout_until is None
            assert user.lockout_count == 0
            assert user.lockout_window_start is None

    def test_non_admin_cannot_unlock(self, authenticated_client, regular_user):
        """A regular user must not be able to access the admin unlock endpoint."""
        response = authenticated_client.post(
            f"/admin/users/{regular_user.id}/unlock",
            follow_redirects=False,
        )
        # Either forbidden or redirect to login/home
        assert response.status_code in (302, 403)

    def test_unlock_nonexistent_user_returns_404(self, admin_client):
        """Unlocking a non-existent email returns a 404 JSON response."""
        response = admin_client.post(
            "/admin/users/nobody@nowhere.invalid/unlock",
        )
        assert response.status_code == 404


# ===========================================================================
# 2. Password Reset Token TTL — 6 hours
# ===========================================================================

class TestPasswordResetTTL:
    """Password reset tokens must expire after 6 hours."""

    def test_reset_token_expires_in_6_hours(self, client, regular_user, app):
        """Submitting /forgot-password sets reset_token_expires ~6 hours in future."""
        client.post("/forgot-password", data={"email": regular_user.email})

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.reset_token is not None
            assert user.reset_token_expires is not None

            delta = user.reset_token_expires - datetime.utcnow()
            # Allow ±60 s tolerance around 6 h (21600 s)
            assert timedelta(hours=5, minutes=59) < delta < timedelta(hours=6, minutes=1)

    def test_reset_token_with_6h_remaining_is_valid(self, client, regular_user, app):
        """A token created near the 6-hour boundary must still be accepted at /reset-password."""
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            user.reset_token = token_hash
            # 5 hours 59 minutes remaining — still valid
            user.reset_token_expires = datetime.utcnow() + timedelta(hours=5, minutes=59)
            db.session.commit()

        response = client.get(f"/reset-password?token={raw_token}")
        # Should show the reset-password form, NOT redirect back to forgot-password
        assert response.status_code == 200
        assert b"reset" in response.data.lower()

    def test_expired_reset_token_rejected(self, client, regular_user, app):
        """A token whose expiry is in the past must be rejected."""
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            user.reset_token = token_hash
            user.reset_token_expires = datetime.utcnow() - timedelta(seconds=1)
            db.session.commit()

        response = client.get(f"/reset-password?token={raw_token}", follow_redirects=True)
        assert response.status_code == 200
        assert b"expired" in response.data.lower()


# ===========================================================================
# 3. Remember Me
# ===========================================================================

class TestRememberMe:
    """Login form exposes a remember-me checkbox that controls session permanence."""

    def test_login_page_has_remember_me_checkbox(self, client):
        """The login page must render a checkbox with name='remember_me'."""
        response = client.get("/login")
        assert response.status_code == 200
        assert b"remember_me" in response.data

    def test_login_without_remember_me_succeeds(self, client, regular_user):
        """Login without the remember_me field checked still redirects correctly."""
        response = _post_login(client, regular_user.email, "Password123!", remember_me=False)
        assert response.status_code == 302

    def test_login_with_remember_me_succeeds(self, client, regular_user):
        """Login with remember_me=on redirects correctly."""
        response = _post_login(client, regular_user.email, "Password123!", remember_me=True)
        assert response.status_code == 302

    def test_login_with_remember_me_creates_active_session(self, client, regular_user, app):
        """Both remember_me variants must create an ActiveSession row."""
        _post_login(client, regular_user.email, "Password123!", remember_me=True)

        with app.app_context():
            sessions = ActiveSession.query.filter_by(user_id=regular_user.id).all()
            assert len(sessions) == 1

    def test_login_without_remember_me_session_not_permanent(self, client, regular_user):
        """Without remember_me the Flask session must not be marked permanent."""
        _post_login(client, regular_user.email, "Password123!", remember_me=False)

        with client.session_transaction() as sess:
            assert not sess.get("_permanent", False)

    def test_login_with_remember_me_session_is_permanent(self, client, regular_user):
        """With remember_me=on the Flask session must be marked permanent."""
        _post_login(client, regular_user.email, "Password123!", remember_me=True)

        with client.session_transaction() as sess:
            assert sess.get("_permanent", False) is True


# ===========================================================================
# 4. Concurrent Session Limit (strictly 1 active session)
# ===========================================================================

class TestConcurrentSessionLimit:
    """Each new successful login must invalidate all previous active sessions."""

    def test_first_login_creates_one_active_session(self, client, regular_user, app):
        """A fresh login results in exactly one ActiveSession row."""
        _post_login(client, regular_user.email, "Password123!")

        with app.app_context():
            count = ActiveSession.query.filter_by(user_id=regular_user.id).count()
            assert count == 1

    def test_second_login_revokes_first_session(self, client, regular_user, app):
        """Logging in twice must result in exactly one ActiveSession at the end."""
        # First login
        _post_login(client, regular_user.email, "Password123!")

        with app.app_context():
            count_after_first = ActiveSession.query.filter_by(user_id=regular_user.id).count()
        assert count_after_first == 1

        # Second login (new client context simulates a different browser)
        client2 = client.application.test_client()
        _post_login(client2, regular_user.email, "Password123!")

        with app.app_context():
            count_after_second = ActiveSession.query.filter_by(user_id=regular_user.id).count()
        assert count_after_second == 1

    def test_pre_existing_session_rows_are_cleared_on_login(self, client, regular_user, app):
        """Even if multiple orphaned ActiveSession rows exist, only one remains after login."""
        with app.app_context():
            # Seed two stale sessions directly in the DB
            for i in range(2):
                db.session.add(ActiveSession(
                    session_token=f"stale_token_{i}",
                    user_id=regular_user.id,
                    ip_address="127.0.0.1",
                ))
            db.session.commit()

        _post_login(client, regular_user.email, "Password123!")

        with app.app_context():
            count = ActiveSession.query.filter_by(user_id=regular_user.id).count()
            assert count == 1


# ===========================================================================
# 5. Login History
# ===========================================================================

class TestLoginHistory:
    """Users can view their sign-in/sign-out history at /account/login-history."""

    def test_login_history_requires_authentication(self, client):
        """/account/login-history must redirect unauthenticated visitors."""
        response = client.get("/account/login-history", follow_redirects=False)
        assert response.status_code == 302

    def test_login_records_login_history_entry(self, client, regular_user, app):
        """A successful login must create a LoginHistory row with event='login'."""
        _post_login(client, regular_user.email, "Password123!")

        with app.app_context():
            entry = LoginHistory.query.filter_by(
                user_id=regular_user.id,
                event="login",
            ).first()
            assert entry is not None
            assert entry.ip_address is not None

    def test_login_history_page_loads(self, authenticated_client):
        """The login history page returns 200 for an authenticated user."""
        response = authenticated_client.get("/account/login-history")
        assert response.status_code == 200

    def test_login_history_page_shows_signin_event(self, authenticated_client):
        """The login history page must contain the 'Sign in' event text."""
        response = authenticated_client.get("/account/login-history")
        assert response.status_code == 200
        body = response.data.lower()
        assert b"sign in" in body or b"login" in body

    def test_login_history_pagination_param_accepted(self, authenticated_client):
        """The ?page= query parameter is accepted without error."""
        response = authenticated_client.get("/account/login-history?page=1")
        assert response.status_code == 200

    def test_login_history_shows_ip_address(self, authenticated_client, regular_user, app):
        """The history page must display the IP address for existing entries."""
        with app.app_context():
            # Seed a known entry
            db.session.add(LoginHistory(
                user_id=regular_user.id,
                ip_address="1.2.3.4",
                event="login",
            ))
            db.session.commit()

        response = authenticated_client.get("/account/login-history")
        assert b"1.2.3.4" in response.data


# ===========================================================================
# 6. Account Settings — Display Name Change
# ===========================================================================

class TestChangeDisplayName:
    """Users can change their display name at /account/settings/display-name."""

    def test_account_settings_page_requires_auth(self, client):
        """/account/settings must redirect when not authenticated."""
        response = client.get("/account/settings", follow_redirects=False)
        assert response.status_code == 302

    def test_account_settings_page_loads(self, authenticated_client):
        """Authenticated users can access the account settings page."""
        response = authenticated_client.get("/account/settings")
        assert response.status_code == 200
        assert b"display" in response.data.lower() or b"name" in response.data.lower()

    def test_change_display_name_success(self, authenticated_client, regular_user, app):
        """A valid display name is saved to the database."""
        response = authenticated_client.post(
            "/account/settings/display-name",
            data={"display_name": "Cricket Legend"},
            follow_redirects=True,
        )
        assert response.status_code == 200

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.display_name == "Cricket Legend"

    def test_change_display_name_unauthenticated(self, client):
        """Unauthenticated POST to display-name endpoint redirects."""
        response = client.post(
            "/account/settings/display-name",
            data={"display_name": "Hacker"},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_change_display_name_too_short_rejected(self, authenticated_client, regular_user, app):
        """A single-character display name must be rejected."""
        original_name = regular_user.display_name

        authenticated_client.post(
            "/account/settings/display-name",
            data={"display_name": "X"},
            follow_redirects=True,
        )

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.display_name == original_name

    def test_change_display_name_too_long_rejected(self, authenticated_client, regular_user, app):
        """A name exceeding 50 characters must be rejected."""
        original_name = regular_user.display_name
        long_name = "A" * 51

        authenticated_client.post(
            "/account/settings/display-name",
            data={"display_name": long_name},
            follow_redirects=True,
        )

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.display_name == original_name

    def test_change_display_name_updates_db_atomically(self, authenticated_client, regular_user, app):
        """Two consecutive name changes each take effect independently."""
        authenticated_client.post(
            "/account/settings/display-name",
            data={"display_name": "First Name"},
            follow_redirects=True,
        )
        authenticated_client.post(
            "/account/settings/display-name",
            data={"display_name": "Second Name"},
            follow_redirects=True,
        )

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.display_name == "Second Name"


# ===========================================================================
# 7. Account Settings — Email Change with Verification
# ===========================================================================

class TestEmailChange:
    """Users can request an email change; it only takes effect after clicking a link
    sent to the new address."""

    def test_request_email_change_requires_auth(self, client):
        """The request-email-change endpoint must redirect unauthenticated users."""
        response = client.post(
            "/account/settings/request-email-change",
            data={"new_email": "x@x.com", "current_password": "p"},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_request_email_change_wrong_password_rejected(self, authenticated_client, regular_user, app):
        """Wrong current password must not set a pending_email."""
        authenticated_client.post(
            "/account/settings/request-email-change",
            data={
                "new_email": "new@example.com",
                "current_password": "WrongPassword!",
            },
            follow_redirects=True,
        )

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.pending_email is None

    def test_request_email_change_same_email_rejected(self, authenticated_client, regular_user, app):
        """Requesting a change to the same email must not set pending_email."""
        authenticated_client.post(
            "/account/settings/request-email-change",
            data={
                "new_email": regular_user.email,
                "current_password": "Password123!",
            },
            follow_redirects=True,
        )

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.pending_email is None

    def test_request_email_change_duplicate_email_rejected(self, authenticated_client, regular_user, app):
        """Requesting a change to an email already used by another account must be rejected."""
        # Create another user
        with app.app_context():
            other = User(
                id="other@example.com",
                password_hash=generate_password_hash("Other123!"),
                email_verified=True,
            )
            db.session.add(other)
            db.session.commit()

        authenticated_client.post(
            "/account/settings/request-email-change",
            data={
                "new_email": "other@example.com",
                "current_password": "Password123!",
            },
            follow_redirects=True,
        )

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.pending_email is None

    def test_request_email_change_success_sets_pending_fields(self, authenticated_client, regular_user, app):
        """A valid request sets pending_email and pending_email_token on the user row."""
        authenticated_client.post(
            "/account/settings/request-email-change",
            data={
                "new_email": "brand-new@example.com",
                "current_password": "Password123!",
            },
            follow_redirects=True,
        )

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.pending_email == "brand-new@example.com"
            assert user.pending_email_token is not None
            assert user.pending_email_token_expires is not None
            # Token should expire ~6 hours from now
            delta = user.pending_email_token_expires - datetime.utcnow()
            assert timedelta(hours=5, minutes=58) < delta < timedelta(hours=6, minutes=2)

    def test_confirm_email_change_invalid_token_rejected(self, client):
        """An invalid token to /account/confirm-email-change must flash an error."""
        response = client.get(
            "/account/confirm-email-change?token=totallyfaketoken",
            follow_redirects=True,
        )
        assert response.status_code == 200
        body = response.data.lower()
        assert b"invalid" in body or b"expired" in body

    def test_confirm_email_change_expired_token_rejected(self, client, app, regular_user):
        """A token whose expiry has passed must be rejected."""
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            user.pending_email = "expired@example.com"
            user.pending_email_token = token_hash
            user.pending_email_token_expires = datetime.utcnow() - timedelta(seconds=1)
            db.session.commit()

        response = client.get(
            f"/account/confirm-email-change?token={raw_token}",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"expired" in response.data.lower()

    def test_confirm_email_change_valid_token_changes_pk(self, client, app, regular_user):
        """Clicking a valid confirmation link changes the user's email (PK) and logs them out."""
        new_email = "confirmed-new@example.com"
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            user.pending_email = new_email
            user.pending_email_token = token_hash
            user.pending_email_token_expires = datetime.utcnow() + timedelta(hours=5)
            db.session.commit()

        response = client.get(
            f"/account/confirm-email-change?token={raw_token}",
            follow_redirects=True,
        )
        assert response.status_code == 200

        with app.app_context():
            # Old email must no longer exist
            old_user = db.session.get(User, regular_user.id)
            assert old_user is None

            # New email must now be the PK
            new_user = db.session.get(User, new_email)
            assert new_user is not None

    def test_confirm_email_change_with_existing_team_succeeds(self, client, app, regular_user):
        """Regression: confirming an email change must not trip the FK on teams.user_id.

        Previously the cascade UPDATEs ran in child-first order while SQLite enforced
        FKs per-statement, so the very first child UPDATE blew up with a FOREIGN KEY
        constraint failure when the user owned any teams. The function now defers FK
        checks to commit time on SQLite. See Issue #159.
        """
        new_email = "team-owner-new@example.com"
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            user.pending_email = new_email
            user.pending_email_token = token_hash
            user.pending_email_token_expires = datetime.utcnow() + timedelta(hours=5)
            db.session.add(Team(user_id=regular_user.id, name="Test XI", short_code="TXI"))
            db.session.commit()

        response = client.get(
            f"/account/confirm-email-change?token={raw_token}",
            follow_redirects=True,
        )
        assert response.status_code == 200

        with app.app_context():
            assert db.session.get(User, regular_user.id) is None
            assert db.session.get(User, new_email) is not None
            team = Team.query.filter_by(name="Test XI").first()
            assert team is not None
            assert team.user_id == new_email

    def test_confirm_email_change_clears_pending_fields(self, client, app, regular_user):
        """After a successful email change the pending_email_* columns are cleared."""
        new_email = "clean-pending@example.com"
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            user.pending_email = new_email
            user.pending_email_token = token_hash
            user.pending_email_token_expires = datetime.utcnow() + timedelta(hours=5)
            db.session.commit()

        client.get(f"/account/confirm-email-change?token={raw_token}", follow_redirects=True)

        with app.app_context():
            new_user = db.session.get(User, new_email)
            assert new_user is not None
            assert new_user.pending_email is None
            assert new_user.pending_email_token is None
            assert new_user.pending_email_token_expires is None

    def test_cancel_email_change_clears_pending_fields(self, authenticated_client, regular_user, app):
        """POSTing to cancel-email-change clears all pending_email_* fields."""
        with app.app_context():
            user = db.session.get(User, regular_user.id)
            user.pending_email = "cancel-me@example.com"
            user.pending_email_token = "somehash"
            user.pending_email_token_expires = datetime.utcnow() + timedelta(hours=5)
            db.session.commit()

        response = authenticated_client.post(
            "/account/settings/cancel-email-change",
            follow_redirects=True,
        )
        assert response.status_code == 200

        with app.app_context():
            user = db.session.get(User, regular_user.id)
            assert user.pending_email is None
            assert user.pending_email_token is None
            assert user.pending_email_token_expires is None

    def test_cancel_email_change_requires_auth(self, client):
        """The cancel endpoint must redirect unauthenticated users."""
        response = client.post(
            "/account/settings/cancel-email-change",
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_account_settings_shows_pending_notice(self, authenticated_client, regular_user, app):
        """When a pending email change exists the settings page shows it."""
        with app.app_context():
            user = db.session.get(User, regular_user.id)
            user.pending_email = "pending@example.com"
            user.pending_email_token = "somehash"
            user.pending_email_token_expires = datetime.utcnow() + timedelta(hours=5)
            db.session.commit()

        response = authenticated_client.get("/account/settings")
        assert response.status_code == 200
        assert b"pending@example.com" in response.data
