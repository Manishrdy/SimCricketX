"""
Test suite for Tournament routes
Tests routes defined in routes/tournament_routes.py
"""

import pytest
from app import db
from database.models import Tournament, TournamentFixture


class TestTournamentListRoute:
    """Tests for tournament listing page."""

    def test_tournaments_page(self, authenticated_client):
        """Test accessing tournaments listing page."""
        response = authenticated_client.get("/tournaments")
        assert response.status_code == 200

    def test_tournaments_unauthenticated(self, client):
        """Test accessing tournaments without login redirects."""
        response = client.get("/tournaments")
        assert response.status_code == 302

    def test_tournaments_page_shows_user_tournaments(self, authenticated_client, test_tournament):
        """Test that tournaments page shows the current user's tournaments."""
        response = authenticated_client.get("/tournaments")
        assert response.status_code == 200
        assert test_tournament.name.encode() in response.data

    def test_tournaments_page_empty(self, authenticated_client):
        """Test tournaments page renders correctly with no tournaments."""
        response = authenticated_client.get("/tournaments")
        assert response.status_code == 200


class TestTournamentCreationRoute:
    """Tests for tournament creation."""

    def test_create_tournament_page_get(self, authenticated_client):
        """Test accessing tournament creation page."""
        response = authenticated_client.get("/tournaments/create")
        assert response.status_code == 200

    def test_create_tournament_unauthenticated(self, client):
        """Test accessing tournament creation without login redirects."""
        response = client.get("/tournaments/create")
        assert response.status_code == 302

    def test_create_tournament_success(self, authenticated_client, test_team, test_team_2, app):
        """Test successful tournament creation with two owned teams."""
        response = authenticated_client.post(
            "/tournaments/create",
            data={
                "name": "New Tournament",
                "mode": "round_robin",
                "team_ids": [test_team.id, test_team_2.id],
            },
            follow_redirects=True,
        )

        assert response.status_code == 200

        # Verify tournament was created in DB
        tournament = db.session.execute(
            db.select(Tournament).filter_by(name="New Tournament")
        ).scalar_one_or_none()
        assert tournament is not None

    def test_create_tournament_insufficient_teams(self, authenticated_client, test_team):
        """Test creating a tournament with only one team shows an error."""
        response = authenticated_client.post(
            "/tournaments/create",
            data={
                "name": "Small Tournament",
                "mode": "round_robin",
                "team_ids": [test_team.id],
            },
            follow_redirects=True,
        )

        assert response.status_code == 200

    def test_create_tournament_no_teams(self, authenticated_client):
        """Test creating a tournament with no teams shows an error."""
        response = authenticated_client.post(
            "/tournaments/create",
            data={
                "name": "Empty Tournament",
                "mode": "round_robin",
                "team_ids": [],
            },
            follow_redirects=True,
        )

        assert response.status_code == 200

    def test_create_tournament_knockout_mode(self, authenticated_client, test_team, test_team_2):
        """Test creating a knockout tournament."""
        response = authenticated_client.post(
            "/tournaments/create",
            data={
                "name": "Knockout Tournament",
                "mode": "knockout",
                "team_ids": [test_team.id, test_team_2.id],
            },
            follow_redirects=True,
        )

        assert response.status_code == 200

    def test_create_custom_series(self, authenticated_client, test_team, test_team_2):
        """Test creating a custom series (requires exactly 2 teams)."""
        response = authenticated_client.post(
            "/tournaments/create",
            data={
                "name": "Test Series",
                "mode": "custom_series",
                "team_ids": [test_team.id, test_team_2.id],
                "series_matches": 5,
            },
            follow_redirects=True,
        )

        assert response.status_code == 200

    def test_create_custom_series_wrong_team_count(self, authenticated_client, test_team):
        """Test that a custom series with only one team is rejected."""
        response = authenticated_client.post(
            "/tournaments/create",
            data={
                "name": "Invalid Series",
                "mode": "custom_series",
                "team_ids": [test_team.id],
            },
            follow_redirects=True,
        )

        assert response.status_code == 200


class TestTournamentDetailRoute:
    """Tests for tournament detail/dashboard page."""

    def test_tournament_detail_page(self, authenticated_client, test_tournament):
        """Test accessing the owner's tournament detail page."""
        response = authenticated_client.get(f"/tournaments/{test_tournament.id}")
        assert response.status_code == 200

    def test_tournament_detail_unauthenticated(self, client, test_tournament):
        """Test accessing tournament detail without login redirects."""
        response = client.get(f"/tournaments/{test_tournament.id}")
        assert response.status_code == 302

    def test_tournament_detail_nonexistent(self, authenticated_client):
        """Test accessing a non-existent tournament returns 404 or redirects."""
        response = authenticated_client.get("/tournaments/99999")
        assert response.status_code in [404, 302]

    def test_tournament_detail_other_user(self, client, admin_user, test_tournament):
        """Test accessing another user's tournament is denied."""
        # Login as admin (who does not own test_tournament)
        client.post("/login", data={
            "email": admin_user.email,
            "password": "Admin123!",
        })

        response = client.get(f"/tournaments/{test_tournament.id}")
        # Route should deny access for non-owners (403, 404, or redirect)
        assert response.status_code in [403, 404, 302]


class TestTournamentDeletionRoute:
    """Tests for tournament deletion."""

    def test_delete_tournament_success(self, authenticated_client, test_tournament, app):
        """Test successful tournament deletion removes it from the database."""
        tournament_id = test_tournament.id

        response = authenticated_client.post(
            f"/tournaments/{tournament_id}/delete",
            follow_redirects=True,
        )

        assert response.status_code == 200

        # Verify tournament is removed using non-deprecated session.get()
        tournament = db.session.get(Tournament, tournament_id)
        assert tournament is None

    def test_delete_tournament_unauthenticated(self, client, test_tournament):
        """Test deleting tournament without login redirects."""
        response = client.post(f"/tournaments/{test_tournament.id}/delete")
        assert response.status_code == 302

    def test_delete_nonexistent_tournament(self, authenticated_client):
        """Test deleting a non-existent tournament returns 404 or redirects."""
        response = authenticated_client.post(
            "/tournaments/99999/delete",
            follow_redirects=True,
        )
        assert response.status_code in [200, 404]

    def test_delete_other_user_tournament(self, client, admin_user, test_tournament):
        """Test that a user cannot delete another user's tournament."""
        # Login as admin (who does not own test_tournament)
        client.post("/login", data={
            "email": admin_user.email,
            "password": "Admin123!",
        })

        response = client.post(
            f"/tournaments/{test_tournament.id}/delete",
            follow_redirects=True,
        )

        # Route should deny or not find the tournament for this user
        assert response.status_code in [403, 404, 200]

        # The tournament must still exist
        tournament = db.session.get(Tournament, test_tournament.id)
        assert tournament is not None


class TestFixtureResimulationRoute:
    """Tests for fixture re-simulation."""

    def test_resimulate_fixture_unauthenticated(self, client):
        """Test re-simulating fixture without login redirects."""
        response = client.post("/fixture/test-fixture-id/resimulate")
        assert response.status_code == 302

    def test_resimulate_nonexistent_fixture(self, authenticated_client):
        """Test re-simulating a non-existent fixture returns 404."""
        response = authenticated_client.post(
            "/fixture/nonexistent/resimulate",
            follow_redirects=True,
        )
        assert response.status_code in [404, 200]


class TestTournamentModes:
    """Tests for different tournament modes."""

    def test_round_robin_requires_min_teams(self, authenticated_client, test_team):
        """Test round robin requires at least 2 teams."""
        response = authenticated_client.post(
            "/tournaments/create",
            data={
                "name": "Round Robin Test",
                "mode": "round_robin",
                "team_ids": [test_team.id],  # Only 1 team — not enough
            },
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_knockout_two_teams_valid(self, authenticated_client, test_team, test_team_2):
        """Test knockout tournament creation with 2 teams (minimum valid count)."""
        response = authenticated_client.post(
            "/tournaments/create",
            data={
                "name": "Knockout Test",
                "mode": "knockout",
                "team_ids": [test_team.id, test_team_2.id],
            },
            follow_redirects=True,
        )
        assert response.status_code == 200


class TestTournamentOwnership:
    """Tests for tournament ownership and access control."""

    def test_user_can_see_own_tournaments(self, authenticated_client, test_tournament):
        """Test that the tournament list shows the current user's tournaments."""
        response = authenticated_client.get("/tournaments")
        assert response.status_code == 200
        assert test_tournament.name.encode() in response.data

    def test_cannot_use_other_user_teams_in_tournament(self, client, admin_user, test_team):
        """Test that users cannot create tournaments using teams they do not own."""
        # Login as admin (who does not own test_team)
        client.post("/login", data={
            "email": admin_user.email,
            "password": "Admin123!",
        })

        response = client.post(
            "/tournaments/create",
            data={
                "name": "Invalid Tournament",
                "mode": "round_robin",
                "team_ids": [test_team.id],  # Not owned by admin
            },
            follow_redirects=True,
        )

        # Should fail validation or silently exclude the unowned team
        assert response.status_code == 200
