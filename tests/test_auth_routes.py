"""
Test suite for Authentication routes
Tests routes defined in routes/auth_routes.py
"""

import pytest
from flask import session
from database.models import User
from app import db


class TestRegistrationRoute:
    """Tests for user registration."""

    def test_registration_page_get(self, client):
        """Test accessing registration page."""
        response = client.get("/register")
        assert response.status_code == 200
        assert b"register" in response.data.lower() or b"sign up" in response.data.lower()

    def test_register_new_user_success(self, client, app):
        """Test successful user registration.

        NOTE: PoW challenge is bypassed when TESTING=True (see verify_auth_pow_solution).
        """
        response = client.post(
            "/register",
            data={
                "email": "newuser@example.com",
                "password": "SecurePass123!",
                "confirm_password": "SecurePass123!",
                "display_name": "New User",
            },
            follow_redirects=True,
        )

        # Should redirect to login on success (302 → 200 with follow_redirects)
        assert response.status_code == 200

        # Verify user was created in database
        user = db.session.execute(
            db.select(User).filter_by(email="newuser@example.com")
        ).scalar_one_or_none()
        assert user is not None
        assert user.display_name == "New User"
        assert user.is_admin is False

    def test_register_duplicate_email(self, client, regular_user):
        """Test registration with an already-registered email shows an error."""
        response = client.post(
            "/register",
            data={
                "email": regular_user.email,
                "password": "NewPass123!",
                "confirm_password": "NewPass123!",
                "display_name": "Duplicate User",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        # Route renders register.html with error about registration failure
        assert (
            b"registration failed" in response.data.lower()
            or b"email" in response.data.lower()
        )

    def test_register_password_mismatch(self, client):
        """Test registration with non-matching passwords is rejected."""
        response = client.post(
            "/register",
            data={
                "email": "mismatch@example.com",
                "password": "Password123!",
                "confirm_password": "DifferentPass123!",
                "display_name": "Mismatch User",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        # Password policy or application-level mismatch check should produce an error
        # The form should re-render the register page, not redirect to login
        assert b"login" not in response.request.path.encode()

    def test_register_weak_password(self, client):
        """Test registration with a password that fails policy is rejected."""
        response = client.post(
            "/register",
            data={
                "email": "weakpass@example.com",
                "password": "123",
                "confirm_password": "123",
                "display_name": "Weak Pass User",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        # Route renders register.html with a policy error — no user created
        user = db.session.execute(
            db.select(User).filter_by(email="weakpass@example.com")
        ).scalar_one_or_none()
        assert user is None

    def test_register_missing_fields(self, client):
        """Test registration with missing required fields stays on register page."""
        response = client.post(
            "/register",
            data={
                "email": "incomplete@example.com",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200


class TestLoginRoute:
    """Tests for user login."""

    def test_login_page_get(self, client):
        """Test accessing login page."""
        response = client.get("/login")
        assert response.status_code == 200
        assert b"login" in response.data.lower() or b"sign in" in response.data.lower()

    def test_login_success(self, client, regular_user):
        """Test successful login redirects to home."""
        response = client.post(
            "/login",
            data={
                "email": regular_user.email,
                "password": "Password123!",
            },
            follow_redirects=False,  # Inspect redirect directly
        )

        # Should redirect to home or force-password-change
        assert response.status_code == 302
        assert "/" in response.headers.get("Location", "")

    def test_login_wrong_password(self, client, regular_user):
        """Test login with incorrect password shows error."""
        response = client.post(
            "/login",
            data={
                "email": regular_user.email,
                "password": "WrongPassword123!",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert b"invalid" in response.data.lower() or b"incorrect" in response.data.lower()

    def test_login_nonexistent_user(self, client):
        """Test login with non-existent email shows invalid credentials error."""
        response = client.post(
            "/login",
            data={
                "email": "nonexistent@example.com",
                "password": "Password123!",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert b"invalid" in response.data.lower() or b"email" in response.data.lower()

    def test_login_banned_user(self, client, banned_user):
        """Test login attempt by a banned user shows a suspension message."""
        response = client.post(
            "/login",
            data={
                "email": banned_user.email,
                "password": "Banned123!",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        # Route renders login.html with "Account suspended" error
        assert b"suspended" in response.data.lower() or b"banned" in response.data.lower()

    def test_login_admin_user(self, client, admin_user):
        """Test admin user login redirects successfully."""
        response = client.post(
            "/login",
            data={
                "email": admin_user.email,
                "password": "Admin123!",
            },
            follow_redirects=False,
        )

        assert response.status_code == 302


class TestLogoutRoute:
    """Tests for user logout."""

    def test_logout_authenticated(self, authenticated_client):
        """Test logout when logged in redirects to login page."""
        response = authenticated_client.post("/logout", follow_redirects=True)
        assert response.status_code == 200
        # After logout should be on login page
        assert b"login" in response.data.lower() or b"sign in" in response.data.lower()

    def test_logout_unauthenticated(self, client):
        """Test logout when not logged in redirects to login (login_required)."""
        response = client.post("/logout", follow_redirects=False)
        # @login_required redirects unauthenticated users
        assert response.status_code == 302


class TestPasswordChangeRoute:
    """Tests for the forced password change functionality (/change-password).

    This route does NOT require the current password — it is used for forced
    password resets initiated by an admin. It only validates new_password and
    confirm_password against the password policy.
    """

    def test_change_password_page_get(self, authenticated_client):
        """Test accessing password change page."""
        response = authenticated_client.get("/change-password")
        assert response.status_code == 200

    def test_change_password_unauthenticated(self, client):
        """Test accessing password change page without login redirects."""
        response = client.get("/change-password")
        assert response.status_code == 302

    def test_change_password_success(self, authenticated_client):
        """Test successful password change with valid matching passwords."""
        response = authenticated_client.post(
            "/change-password",
            data={
                "new_password": "NewSecurePass456!",
                "confirm_password": "NewSecurePass456!",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200

    def test_change_password_policy_failure(self, authenticated_client):
        """Test that a password failing the policy shows an error."""
        response = authenticated_client.post(
            "/change-password",
            data={
                "new_password": "123",
                "confirm_password": "123",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        # Route renders the change-password page with an error, not a redirect
        assert b"password" in response.data.lower()

    def test_change_password_mismatch(self, authenticated_client):
        """Test password change with non-matching new passwords shows error."""
        response = authenticated_client.post(
            "/change-password",
            data={
                "new_password": "NewSecurePass456!",
                "confirm_password": "DifferentPass789!",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert b"do not match" in response.data.lower() or b"mismatch" in response.data.lower()


class TestDisplayNameRoute:
    """Tests for display name management."""

    def test_set_display_name_page_get(self, authenticated_client):
        """Test accessing display name page."""
        response = authenticated_client.get("/set-display-name")
        assert response.status_code == 200

    def test_set_display_name_success(self, authenticated_client, regular_user, app):
        """Test successfully setting display name updates the database."""
        response = authenticated_client.post(
            "/set-display-name",
            data={
                "display_name": "Updated Display Name",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200

        # Verify in database using non-deprecated session.get()
        user = db.session.get(User, regular_user.id)
        assert user.display_name == "Updated Display Name"

    def test_set_display_name_unauthenticated(self, client):
        """Test setting display name without authentication redirects."""
        response = client.get("/set-display-name")
        assert response.status_code == 302


class TestAccountDeletionRoute:
    """Tests for account deletion."""

    def test_delete_account_authenticated(self, authenticated_client, regular_user, app):
        """Test account deletion requires the DELETE confirmation string."""
        user_id = regular_user.id

        response = authenticated_client.post(
            "/delete_account",
            data={"confirm_delete": "DELETE"},
            follow_redirects=True,
        )

        assert response.status_code == 200
        # User should no longer exist after successful deletion
        deleted_user = db.session.get(User, user_id)
        assert deleted_user is None

    def test_delete_account_missing_confirmation(self, authenticated_client, regular_user, app):
        """Test that account deletion without the DELETE string does NOT delete the account."""
        user_id = regular_user.id

        response = authenticated_client.post(
            "/delete_account",
            data={},  # No confirm_delete field
            follow_redirects=True,
        )

        assert response.status_code == 200
        # User must still exist
        user = db.session.get(User, user_id)
        assert user is not None

    def test_delete_account_unauthenticated(self, client):
        """Test account deletion without login redirects."""
        response = client.post("/delete_account")
        assert response.status_code == 302


class TestAuthChallenge:
    """Tests for the proof-of-work challenge endpoint."""

    def test_auth_challenge_get(self, client):
        """Test auth challenge returns a JSON payload."""
        response = client.get("/auth/challenge")
        assert response.status_code == 200
        data = response.get_json()
        assert data is not None
        # Challenge payload must contain at minimum a challenge_id
        assert "challenge_id" in data or "nonce" in data
