"""
Test suite for Team routes
Tests routes defined in routes/team_routes.py
"""

import pytest
from app import db
from database.models import Team as DBTeam, Player as DBPlayer


# ==================== Helpers ====================

def _valid_team_form(name="New Test Team", short_code="NTT", num_players=12):
    """Return a multidict-compatible dict representing a valid team creation form.

    The production route reads these fields via request.form.getlist():
      player_name, player_role, batting_rating, bowling_rating,
      fielding_rating, batting_hand, bowling_type, bowling_hand

    Roles MUST match the validator strings: Wicketkeeper, Batsman, Bowler, All-rounder.
    Active teams require 12–25 players, ≥1 Wicketkeeper, ≥6 Bowler/All-rounder.
    """
    # Build per-player lists (first player = Wicketkeeper, next 6 = All-rounders, rest = Batsmen)
    player_names = [f"Player {i}" for i in range(1, num_players + 1)]
    roles = ["Wicketkeeper"] + ["All-rounder"] * 6 + ["Batsman"] * (num_players - 7)
    ratings = ["70"] * num_players

    return {
        "team_name": name,
        "short_code": short_code,
        "home_ground": "Test Ground",
        "pitch_preference": "flat",
        "team_color": "#ff0000",
        "captain": "Player 1",
        "wicketkeeper": "Player 1",
        "action": "publish",
        "player_name": player_names,
        "player_role": roles,
        "batting_rating": ratings,
        "bowling_rating": ratings,
        "fielding_rating": ratings,
        "batting_hand": ["right"] * num_players,
        # Wicketkeeper has no bowling type; all others use medium
        "bowling_type": [""] + ["medium"] * (num_players - 1),
        "bowling_hand": ["right"] * num_players,
    }


# ==================== Tests ====================

class TestTeamCreationRoute:
    """Tests for team creation functionality."""

    def test_team_create_page_get(self, authenticated_client):
        """Test accessing team creation page."""
        response = authenticated_client.get("/team/create")
        assert response.status_code == 200
        assert b"create" in response.data.lower() or b"team" in response.data.lower()

    def test_team_create_unauthenticated(self, client):
        """Test accessing team creation page without login redirects."""
        response = client.get("/team/create")
        assert response.status_code == 302

    def test_create_team_success(self, authenticated_client, regular_user, app):
        """Test successful team creation with all required fields."""
        response = authenticated_client.post(
            "/team/create",
            data=_valid_team_form(),
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_create_team_duplicate_short_code(self, authenticated_client, test_team):
        """Test creating a team with a short code already used by the same user shows an error."""
        form = _valid_team_form(name="Another Team", short_code=test_team.short_code)
        response = authenticated_client.post(
            "/team/create",
            data=form,
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"short code" in response.data.lower() or b"already" in response.data.lower()

    def test_create_team_insufficient_players(self, authenticated_client):
        """Test creating a team with fewer than 12 players shows a validation error."""
        form = _valid_team_form(num_players=5)
        # Ensure there are still 6 All-rounders for the "12+ players" check to trigger first
        form["player_role"] = ["Wicketkeeper"] + ["All-rounder"] * 4
        response = authenticated_client.post(
            "/team/create",
            data=form,
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"12" in response.data or b"player" in response.data.lower()


class TestTeamManagementRoute:
    """Tests for team management/listing."""

    def test_manage_teams_page(self, authenticated_client):
        """Test accessing teams management page."""
        response = authenticated_client.get("/teams/manage")
        assert response.status_code == 200

    def test_manage_teams_unauthenticated(self, client):
        """Test accessing teams management without login redirects."""
        response = client.get("/teams/manage")
        assert response.status_code == 302

    def test_manage_teams_shows_user_teams(self, authenticated_client, test_team):
        """Test that manage page shows the current user's teams."""
        response = authenticated_client.get("/teams/manage")
        assert response.status_code == 200
        assert test_team.name.encode() in response.data

    def test_manage_teams_empty_list(self, authenticated_client):
        """Test manage page renders correctly with no teams."""
        response = authenticated_client.get("/teams/manage")
        assert response.status_code == 200


class TestTeamDeletionRoute:
    """Tests for team deletion."""

    def test_delete_team_success(self, authenticated_client, test_team, app):
        """Test successful team deletion using short_code (the route's lookup key)."""
        team_id = test_team.id
        short_code = test_team.short_code

        response = authenticated_client.post(
            "/team/delete",
            data={"short_code": short_code},
            follow_redirects=True,
        )

        assert response.status_code == 200

        # Verify the team is removed from the database
        team = db.session.get(DBTeam, team_id)
        assert team is None

    def test_delete_team_unauthenticated(self, client, test_team):
        """Test team deletion without authentication redirects."""
        response = client.post(
            "/team/delete",
            data={"short_code": test_team.short_code},
        )
        assert response.status_code == 302

    def test_delete_nonexistent_team(self, authenticated_client):
        """Test deleting a non-existent team redirects with an error flash."""
        response = authenticated_client.post(
            "/team/delete",
            data={"short_code": "NOTEXIST"},
            follow_redirects=True,
        )
        # Route flashes an error and redirects to manage_teams
        assert response.status_code == 200

    def test_delete_other_user_team(self, client, admin_user, test_team):
        """Test that a user cannot delete a team owned by another user."""
        # Login as admin (who does not own test_team)
        client.post("/login", data={
            "email": admin_user.email,
            "password": "Admin123!",
        })

        response = client.post(
            "/team/delete",
            data={"short_code": test_team.short_code},
            follow_redirects=True,
        )

        # Route filters by user_id — admin won't find regular user's team.
        # Should redirect with an error flash (still HTTP 200 after follow_redirects).
        assert response.status_code == 200

        # The team must still exist (not deleted)
        team = db.session.get(DBTeam, test_team.id)
        assert team is not None


class TestTeamEditRoute:
    """Tests for team editing."""

    def test_edit_team_page_get(self, authenticated_client, test_team):
        """Test accessing team edit page."""
        response = authenticated_client.get(f"/team/{test_team.short_code}/edit")
        assert response.status_code == 200

    def test_edit_team_unauthenticated(self, client, test_team):
        """Test accessing team edit page without login redirects."""
        response = client.get(f"/team/{test_team.short_code}/edit")
        assert response.status_code == 302

    def test_edit_team_success(self, authenticated_client, test_team):
        """Test successfully editing a team with valid form data."""
        form = _valid_team_form(name="Updated Team Name", short_code=test_team.short_code)
        response = authenticated_client.post(
            f"/team/{test_team.short_code}/edit",
            data=form,
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_edit_nonexistent_team(self, authenticated_client):
        """Test editing a non-existent team redirects (no such team for this user)."""
        response = authenticated_client.get("/team/NONEXIST/edit")
        # Route redirects to manage_teams when team not found
        assert response.status_code == 302

    def test_edit_other_user_team(self, client, admin_user, test_team):
        """Test editing another user's team is denied (route ownership check)."""
        client.post("/login", data={
            "email": admin_user.email,
            "password": "Admin123!",
        })

        response = client.get(f"/team/{test_team.short_code}/edit")
        # Route redirects to manage_teams when team not found for the admin user
        assert response.status_code == 302


class TestTeamValidation:
    """Tests for team validation rules enforced by the creation route."""

    def test_team_requires_wicketkeeper(self, authenticated_client):
        """Test that creating a team without a Wicketkeeper role is rejected."""
        form = _valid_team_form()
        # Override all roles to Batsman — no Wicketkeeper
        form["player_role"] = ["Batsman"] * 12
        form["bowling_type"] = [""] * 12

        response = authenticated_client.post(
            "/team/create",
            data=form,
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert b"wicketkeeper" in response.data.lower()

    def test_team_requires_six_bowlers(self, authenticated_client):
        """Test that fewer than 6 Bowler/All-rounder roles is rejected."""
        form = _valid_team_form()
        # 1 WK + 5 All-rounders + 6 Batsmen = only 5 bowling roles
        form["player_role"] = ["Wicketkeeper"] + ["All-rounder"] * 5 + ["Batsman"] * 6
        form["bowling_type"] = [""] + ["medium"] * 5 + [""] * 6

        response = authenticated_client.post(
            "/team/create",
            data=form,
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert b"six" in response.data.lower() or b"bowler" in response.data.lower()

    def test_team_requires_captain_and_wicketkeeper_selection(self, authenticated_client):
        """Test that missing captain/wicketkeeper names is rejected for active teams."""
        form = _valid_team_form()
        del form["captain"]
        del form["wicketkeeper"]

        response = authenticated_client.post(
            "/team/create",
            data=form,
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert (
            b"captain" in response.data.lower()
            or b"wicketkeeper" in response.data.lower()
        )
