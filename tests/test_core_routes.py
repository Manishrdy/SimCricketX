"""
Test suite for Core routes (home, ground conditions, etc.)
Tests routes defined in routes/core_routes.py
"""

import re
from datetime import datetime, timedelta

import pytest
from flask import session
from werkzeug.security import generate_password_hash
from app import db
from database.models import ActiveSession, AnnouncementBanner, User, UserBannerDismissal


class TestHomeRoute:
    """Tests for the home/landing page route."""

    def test_home_page_unauthenticated(self, client):
        """Test accessing home page without authentication renders landing page.

        Since v2.3.0, anonymous users see the public landing page at `/`
        (for SEO and discovery) instead of being redirected to `/login`.
        """
        response = client.get("/")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "SimCricketX" in body
        assert "Sign In" in body

    def test_home_page_authenticated(self, authenticated_client):
        """Test accessing home page with authentication renders the dashboard."""
        response = authenticated_client.get("/")
        assert response.status_code == 200

    def test_home_page_admin(self, admin_client):
        """Test accessing home page as admin renders the dashboard."""
        response = admin_client.get("/")
        assert response.status_code == 200

    def test_home_labels_presence_as_five_minute_activity(self, authenticated_client):
        response = authenticated_client.get("/")
        body = response.get_data(as_text=True)

        assert "Active (5m)" in body
        assert "Users active in the last 5 minutes" in body

    def test_presence_excludes_sessions_outside_activity_window(
        self, authenticated_client, app
    ):
        """A valid but idle login must not be presented as currently active."""
        with app.app_context():
            recent_user = User(
                id="recent@example.com",
                email="recent@example.com",
                password_hash=generate_password_hash("Password123!"),
                email_verified=True,
            )
            idle_user = User(
                id="idle@example.com",
                email="idle@example.com",
                password_hash=generate_password_hash("Password123!"),
                email_verified=True,
            )
            db.session.add_all([recent_user, idle_user])
            db.session.flush()
            now = datetime.utcnow()
            db.session.add_all([
                ActiveSession(
                    session_token="recent-presence-token",
                    user_id=recent_user.id,
                    last_active=now - timedelta(minutes=4),
                ),
                ActiveSession(
                    session_token="idle-presence-token",
                    user_id=idle_user.id,
                    last_active=now - timedelta(minutes=6),
                ),
            ])
            db.session.commit()

        response = authenticated_client.get("/api/presence")

        assert response.status_code == 200
        assert response.get_json() == {
            "active_users": 2,  # the signed-in test user plus recent_user
            "window_minutes": 5,
        }
        assert response.headers["Cache-Control"] == "no-store"

    def test_presence_requires_authentication(self, client):
        response = client.get("/api/presence")
        assert response.status_code == 302

    def test_home_page_shows_active_announcement_banner(self, authenticated_client, app):
        """Active announcement banner should render on home page for users who did not dismiss it."""
        with app.app_context():
            db.session.add(
                AnnouncementBanner(
                    message="Server maintenance tonight at 10 PM.",
                    is_enabled=True,
                    version=1,
                )
            )
            db.session.commit()

        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert "Server maintenance tonight at 10 PM." in response.get_data(as_text=True)

    def test_home_page_hides_banner_after_dismiss(self, authenticated_client, regular_user, app):
        """After dismissing the active banner, it should not render again for that user/version."""
        with app.app_context():
            db.session.add(
                AnnouncementBanner(
                    message="Important platform notice",
                    is_enabled=True,
                    version=1,
                )
            )
            db.session.commit()

        dismiss_resp = authenticated_client.post("/announcement-banner/dismiss")
        assert dismiss_resp.status_code == 200

        with app.app_context():
            dismissal = UserBannerDismissal.query.filter_by(
                user_id=regular_user.id,
                banner_version=1,
            ).first()
            assert dismissal is not None

        home_resp = authenticated_client.get("/")
        assert home_resp.status_code == 200
        assert "Important platform notice" not in home_resp.get_data(as_text=True)


@pytest.fixture
def ground_qa(app):
    from scripts.dev_test_accounts import seed, logged_in_client
    seed(app)
    return logged_in_client(app, 'user1')


class TestGroundConditionsRoutes:
    """The retired editor preserves bookmarks, auth guards and stored tuning."""

    @pytest.mark.parametrize('match_format', ['T20', 'ListA', 'FC', 'Hundred', 'UnknownFormat'])
    def test_editor_bookmark_redirects(self, ground_qa, match_format):
        response = ground_qa.get('/ground-conditions?format=' + match_format)
        assert response.status_code == 302
        assert response.location.endswith('/stadiums')

    @pytest.mark.parametrize('endpoint', ['save', 'mode', 'reset'])
    def test_writes_retired(self, ground_qa, endpoint):
        response = ground_qa.post('/ground-conditions/' + endpoint, json={'match_format':'T20'})
        assert response.status_code == 410
        assert 'stadium' in response.json['error'].lower()

    @pytest.mark.parametrize('endpoint', ['save', 'mode', 'reset'])
    def test_authentication_still_required(self, client, endpoint):
        assert client.post('/ground-conditions/' + endpoint, json={}).status_code == 302

    def test_guide_bookmark_remains_public(self, client):
        response = client.get('/ground-conditions/guide', follow_redirects=True)
        assert response.status_code == 200
        assert b'Stadium conditions' in response.data


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
