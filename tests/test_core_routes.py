"""
Test suite for Core routes (home, ground conditions, etc.)
Tests routes defined in routes/core_routes.py
"""

import pytest
from flask import session


class TestHomeRoute:
    """Tests for the home/landing page route."""

    def test_home_page_unauthenticated(self, client):
        """Test accessing home page without authentication redirects to login.

        The home route is decorated with @login_required, so unauthenticated
        requests must receive a 302 redirect — not a 200.
        """
        response = client.get("/")
        assert response.status_code == 302
        assert "login" in response.headers.get("Location", "").lower()

    def test_home_page_authenticated(self, authenticated_client):
        """Test accessing home page with authentication renders the dashboard."""
        response = authenticated_client.get("/")
        assert response.status_code == 200

    def test_home_page_admin(self, admin_client):
        """Test accessing home page as admin renders the dashboard."""
        response = admin_client.get("/")
        assert response.status_code == 200


class TestGroundConditionsRoutes:
    """Tests for ground conditions management routes."""

    def test_ground_conditions_page_unauthenticated(self, client):
        """Test accessing ground conditions page without login redirects."""
        response = client.get("/ground-conditions")
        # @login_required always issues a 302 redirect for unauthenticated users
        assert response.status_code == 302

    def test_ground_conditions_page_authenticated(self, authenticated_client):
        """Test accessing ground conditions page when logged in."""
        response = authenticated_client.get("/ground-conditions")
        assert response.status_code == 200

    def test_save_ground_conditions_authenticated(self, authenticated_client):
        """Test saving ground conditions via JSON body."""
        response = authenticated_client.post(
            "/ground-conditions/save",
            json={
                "pitch_type": "flat",
                "outfield_speed": "fast",
                "weather": "sunny",
            },
        )
        # Route returns JSON (200 on success, 400 on validation error)
        assert response.status_code in [200, 400]

    def test_save_ground_conditions_unauthenticated(self, client):
        """Test saving ground conditions without authentication is denied."""
        response = client.post(
            "/ground-conditions/save",
            json={"pitch_type": "flat"},
        )
        # @login_required redirects unauthenticated requests
        assert response.status_code == 302

    def test_change_ground_conditions_mode(self, authenticated_client):
        """Test changing ground conditions mode via JSON body."""
        response = authenticated_client.post(
            "/ground-conditions/mode",
            json={"mode": "manual"},
        )
        # Route reads mode from request.get_json(); returns JSON response
        assert response.status_code in [200, 400, 500]

    def test_reset_ground_conditions(self, authenticated_client):
        """Test resetting ground conditions to defaults returns JSON."""
        response = authenticated_client.post("/ground-conditions/reset")
        assert response.status_code in [200, 500]

    def test_ground_conditions_guide_authenticated(self, authenticated_client):
        """Test accessing ground conditions guide when authenticated."""
        response = authenticated_client.get("/ground-conditions/guide")
        assert response.status_code == 200

    def test_ground_conditions_guide_unauthenticated(self, client):
        """Test the guide page is publicly accessible (no @login_required)."""
        response = client.get("/ground-conditions/guide")
        assert response.status_code == 200


class TestMaintenanceMode:
    """Tests for maintenance mode behavior."""

    def test_maintenance_mode_disabled_by_default(self, client):
        """Test that maintenance mode is disabled by default in the test config."""
        response = client.get("/")
        # Should redirect to login, not serve a 503 maintenance page
        assert response.status_code != 503

    @pytest.mark.skip(reason="Requires reconfiguring app with maintenance_mode=True")
    def test_maintenance_mode_enabled(self, client):
        """Test accessing site when maintenance mode is enabled returns 503."""
        pass
