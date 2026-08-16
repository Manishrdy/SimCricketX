"""
Test suite for Core routes (home, ground conditions, etc.)
Tests routes defined in routes/core_routes.py
"""

import re

import pytest
from flask import session
from app import db
from database.models import AnnouncementBanner, UserBannerDismissal


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

    @pytest.mark.parametrize("match_format", ["T20", "ListA"])
    def test_ground_conditions_page_per_format(self, authenticated_client, match_format):
        """Each format renders its own editor."""
        response = authenticated_client.get(f"/ground-conditions?format={match_format}")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # The format tabs are always present; the active one reflects the query.
        assert 'class="gc-format-tabs"' in body
        if match_format == "ListA":
            # Phase matrices are List A only; game modes are T20 only.
            assert "Phase Scoring Matrices" in body
            assert 'id="gcModesRow"' not in body
        else:
            assert 'id="gcModesRow"' in body
            assert "Phase Scoring Matrices" not in body

    def test_ground_conditions_page_bad_format_falls_back(self, authenticated_client):
        """An unknown ?format= shows the default editor rather than erroring."""
        response = authenticated_client.get("/ground-conditions?format=Hundred")
        assert response.status_code == 200
        assert 'id="gcModesRow"' in response.get_data(as_text=True)

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
            json={"mode": "aggressive"},
        )
        assert response.status_code == 200

    def test_change_ground_conditions_mode_rejects_unknown(self, authenticated_client):
        """An unrecognised mode name is refused rather than silently stored."""
        response = authenticated_client.post(
            "/ground-conditions/mode",
            json={"mode": "manual"},
        )
        assert response.status_code == 400

    def test_reset_ground_conditions(self, authenticated_client):
        """Test resetting ground conditions to defaults returns JSON."""
        response = authenticated_client.post("/ground-conditions/reset")
        assert response.status_code in [200, 500]

    def test_reset_is_scoped_to_one_format(self, authenticated_client, regular_user):
        """Resetting List A must not clear the user's T20 config."""
        from engine.ground_config import get_effective_config, get_user_config

        user_id = regular_user.id

        # Pin a T20 mode so the user has a stored T20 row.
        assert authenticated_client.post(
            "/ground-conditions/mode",
            json={"mode": "aggressive", "match_format": "T20"},
        ).status_code == 200

        # Save something for List A, then reset only List A.
        lista_cfg = get_effective_config(user_id, "ListA")
        lista_cfg["pitch_profiles"]["Hard"]["run_factor"] = 1.5
        assert authenticated_client.post(
            "/ground-conditions/save",
            json={**lista_cfg, "match_format": "ListA"},
        ).status_code == 200
        assert authenticated_client.post(
            "/ground-conditions/reset",
            json={"match_format": "ListA"},
        ).status_code == 200

        assert get_user_config(user_id, "ListA") is None
        t20 = get_user_config(user_id, "T20")
        assert t20 is not None and t20.get("active_game_mode") == "aggressive"

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


class TestGroundConditionsSimpleUI:
    """The pitch editor leads with plain-language sliders; the raw probability
    grid moved behind an Advanced disclosure.

    The sliders are a *driver* for the raw inputs rather than a parallel store,
    and they are rendered without a `value` attribute so they cannot write
    anything on load. That is what makes "open a pitch, hit Save, nothing
    changed" true — without it, every user's pitches would silently re-tune the
    first time they saved.
    """

    def _page(self, client, match_format="T20"):
        resp = client.get(f"/ground-conditions?format={match_format}")
        assert resp.status_code == 200
        return resp.get_data(as_text=True)

    @pytest.mark.parametrize("match_format", ["T20", "ListA"])
    def test_intent_sliders_render(self, authenticated_client, match_format):
        body = self._page(authenticated_client, match_format)
        assert 'class="gc-intent"' in body
        assert "gc-estimate" in body

    @pytest.mark.parametrize("match_format", ["T20", "ListA"])
    def test_intent_sliders_carry_no_value_attribute(self, authenticated_client, match_format):
        """A server-rendered value would be written into the config on load."""
        body = self._page(authenticated_client, match_format)
        for tag in re.findall(r"<input[^>]*class=\"gc-intent\"[^>]*>", body):
            assert "value=" not in tag, f"intent slider must not ship a value: {tag}"

    @pytest.mark.parametrize("match_format", ["T20", "ListA"])
    def test_advanced_section_present_and_collapsed(self, authenticated_client, match_format):
        """Nothing became unreachable — the raw editor is still there, just closed."""
        body = self._page(authenticated_client, match_format)
        assert "gc-advanced-summary" in body
        for tag in re.findall(r"<details[^>]*class=\"gc-advanced\"[^>]*>", body):
            assert " open" not in tag, "Advanced should start collapsed"

    def test_raw_t20_inputs_still_available(self, authenticated_client):
        body = self._page(authenticated_client, "T20")
        # buildConfig() reads these; they remain the source of truth.
        assert "gc-matrix-input" in body
        assert "gc-wf-input" in body
        assert 'data-field="run_factor"' in body

    def test_raw_lista_inputs_still_available(self, authenticated_client):
        body = self._page(authenticated_client, "ListA")
        assert "gc-la-matrix-input" in body
        assert 'data-la-field="wicket_mult"' in body
        assert 'data-la-ft="Four"' in body
