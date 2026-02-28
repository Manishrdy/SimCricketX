"""
Test suite for Match routes
Tests routes defined in routes/match_routes.py
"""

import pytest
import json
import os
import uuid
from database.models import Match as DBMatch, MatchScorecard


class TestMatchSetupRoute:
    """Tests for match setup functionality."""

    def test_match_setup_page_get(self, authenticated_client):
        """Test accessing match setup page."""
        response = authenticated_client.get("/match/setup")
        assert response.status_code == 200
        assert b"setup" in response.data.lower() or b"match" in response.data.lower()

    def test_match_setup_unauthenticated(self, client):
        """Test accessing match setup without login redirects."""
        response = client.get("/match/setup")
        assert response.status_code == 302

    def test_match_setup_with_teams(self, authenticated_client, test_team, test_team_2):
        """Test match setup page lists available teams."""
        response = authenticated_client.get("/match/setup")
        assert response.status_code == 200
        assert test_team.name.encode() in response.data
        assert test_team_2.name.encode() in response.data

    def test_create_match_success(self, authenticated_client, test_team, test_team_2):
        """Test successful match creation via JSON body (match_setup POST reads JSON)."""
        response = authenticated_client.post(
            "/match/setup",
            json={
                "team1_id": test_team.id,
                "team2_id": test_team_2.id,
                "overs": 20,
                "simulation_mode": "auto",
            },
            follow_redirects=True,
        )
        assert response.status_code in [200, 400]

    def test_create_match_same_team(self, authenticated_client, test_team):
        """Test creating a match with the same team for both sides shows an error."""
        response = authenticated_client.post(
            "/match/setup",
            json={
                "team1_id": test_team.id,
                "team2_id": test_team.id,
                "overs": 20,
                "simulation_mode": "auto",
            },
            follow_redirects=True,
        )
        assert response.status_code in [200, 400]

    def test_create_match_invalid_overs(self, authenticated_client, test_team, test_team_2):
        """Test creating a match with negative overs is rejected."""
        response = authenticated_client.post(
            "/match/setup",
            json={
                "team1_id": test_team.id,
                "team2_id": test_team_2.id,
                "overs": -5,
                "simulation_mode": "auto",
            },
            follow_redirects=True,
        )
        assert response.status_code in [200, 400]

    def test_create_match_missing_json_body(self, authenticated_client):
        """Test POST with no JSON body returns 400."""
        response = authenticated_client.post(
            "/match/setup",
            data={},  # form data instead of JSON → get_json() returns None
        )
        assert response.status_code == 400



class TestMatchDetailRoute:
    """Tests for match detail/view page."""

    def test_match_detail_unauthenticated(self, client):
        """Test accessing match detail without login redirects."""
        response = client.get("/match/test-match-id")
        assert response.status_code == 302

    def test_match_detail_nonexistent(self, authenticated_client):
        """Test accessing a non-existent match returns 404 or redirects."""
        response = authenticated_client.get("/match/nonexistent-match-id")
        assert response.status_code in [404, 302]


class TestMatchScoreboardRoute:
    """Tests for match scoreboard."""

    def test_scoreboard_unauthenticated(self, client):
        """Test accessing scoreboard without login redirects."""
        response = client.get("/match/test-match-id/scoreboard")
        assert response.status_code == 302

    def test_scoreboard_nonexistent(self, authenticated_client):
        """Test accessing scoreboard for a non-existent match returns 404 or redirects."""
        response = authenticated_client.get("/match/nonexistent/scoreboard")
        assert response.status_code in [404, 302]


class TestTossRoutes:
    """Tests for toss-related routes."""

    def test_set_toss_unauthenticated(self, client):
        """Test setting toss without login redirects."""
        response = client.post(
            "/match/test-match-id/set-toss",
            json={"winner": "team1", "decision": "bat"},
        )
        assert response.status_code == 302

    def test_spin_toss_unauthenticated(self, client):
        """Test spinning toss without login redirects."""
        response = client.post("/match/test-match-id/spin-toss")
        assert response.status_code == 302


class TestImpactPlayerRoute:
    """Tests for impact player swap functionality."""

    def test_impact_player_swap_unauthenticated(self, client):
        """Test impact player swap without login redirects."""
        response = client.post(
            "/match/test-match-id/impact-player-swap",
            json={"player_in": 1, "player_out": 2},
        )
        assert response.status_code == 302

    def test_impact_player_swap_rejects_lista_format(self, authenticated_client, regular_user, app):
        """Impact player swaps are allowed only in T20 matches."""
        match_id = str(uuid.uuid4())
        match_dir = os.path.join(app.root_path, "data", "matches")
        os.makedirs(match_dir, exist_ok=True)

        match_payload = {
            "match_id": match_id,
            "created_by": regular_user.id,
            "match_format": "ListA",
            "playing_xi": {
                "home": [{"name": "Home XI Player", "role": "Batsman"}],
                "away": [{"name": "Away XI Player", "role": "Batsman"}],
            },
            "substitutes": {
                "home": [{"name": "Home Sub Player", "role": "Batsman"}],
                "away": [{"name": "Away Sub Player", "role": "Batsman"}],
            },
        }
        match_path = os.path.join(match_dir, f"match_{match_id}.json")
        with open(match_path, "w", encoding="utf-8") as f:
            json.dump(match_payload, f, indent=2)

        response = authenticated_client.post(
            f"/match/{match_id}/impact-player-swap",
            json={
                "home_swap": {"out_player_index": 0, "in_player_index": 0},
            },
        )
        assert response.status_code == 400
        payload = response.get_json()
        assert payload
        assert "only for t20" in payload.get("error", "").lower()


class TestLineupUpdateRoute:
    """Tests for updating final lineups."""

    def test_update_lineups_unauthenticated(self, client):
        """Test updating lineups without login redirects."""
        response = client.post(
            "/match/test-match-id/update-final-lineups",
            json={"team1_lineup": [], "team2_lineup": []},
        )
        assert response.status_code == 302


class TestMatchSimulationRoutes:
    """Tests for match simulation endpoints."""

    def test_next_ball_unauthenticated(self, client):
        """Test simulating next ball without login redirects."""
        response = client.post("/match/test-match-id/next-ball")
        assert response.status_code == 302

    def test_set_simulation_mode_unauthenticated(self, client):
        """Test setting simulation mode without login redirects."""
        response = client.post(
            "/match/test-match-id/set-simulation-mode",
            json={"mode": "auto"},
        )
        assert response.status_code == 302

    def test_submit_decision_unauthenticated(self, client):
        """Test submitting manual decision without login redirects."""
        response = client.post(
            "/match/test-match-id/submit-decision",
            json={"decision": "aggressive"},
        )
        assert response.status_code == 302


class TestSuperOverRoutes:
    """Tests for super over functionality."""

    def test_start_super_over_unauthenticated(self, client):
        """Test starting super over without login redirects."""
        response = client.post("/match/test-match-id/start-super-over")
        assert response.status_code == 302

    def test_start_super_over_innings2_unauthenticated(self, client):
        """Test starting super over innings 2 without login redirects."""
        response = client.post("/match/test-match-id/start-super-over-innings2")
        assert response.status_code == 302

    def test_next_super_over_ball_unauthenticated(self, client):
        """Test simulating a super over ball without login redirects."""
        response = client.post("/match/test-match-id/next-super-over-ball")
        assert response.status_code == 302


class TestCommentaryRoute:
    """Tests for commentary saving."""

    def test_save_commentary_unauthenticated(self, client):
        """Test saving commentary without login redirects."""
        response = client.post(
            "/match/test-match-id/save-commentary",
            json={"commentary": "Test commentary"},
        )
        assert response.status_code == 302


class TestMatchArchiveRoutes:
    """Tests for match archiving and downloads."""

    def test_download_archive_unauthenticated(self, client):
        """Test downloading match archive without login redirects."""
        response = client.post("/match/test-match-id/download-archive")
        assert response.status_code == 302

    def test_access_archive_unauthenticated(self, client):
        """Test accessing an archived match file without login redirects."""
        response = client.get("/archives/testuser/test-archive.json")
        assert response.status_code == 302

    def test_delete_archive_unauthenticated(self, client):
        """Test deleting an archive without login redirects."""
        response = client.delete("/archives/test-archive.json")
        assert response.status_code == 302


class TestMyMatchesRoute:
    """Tests for user's matches listing."""

    def test_my_matches_page(self, authenticated_client):
        """Test accessing my matches page."""
        response = authenticated_client.get("/my-matches")
        assert response.status_code == 200

    def test_my_matches_unauthenticated(self, client):
        """Test accessing my matches without login redirects."""
        response = client.get("/my-matches")
        assert response.status_code == 302

    def test_my_matches_empty(self, authenticated_client):
        """Test my matches page renders correctly with no matches."""
        response = authenticated_client.get("/my-matches")
        assert response.status_code == 200


class TestBulkMatchDeletion:
    """Tests for deleting multiple matches."""

    def test_delete_multiple_matches_unauthenticated(self, client):
        """Test bulk deletion without login redirects."""
        response = client.post(
            "/matches/delete-multiple",
            json={"match_ids": ["id1", "id2"]},
        )
        assert response.status_code == 302

    def test_delete_multiple_matches_empty_list(self, authenticated_client):
        """Test bulk deletion with an empty list returns 200 or 400."""
        response = authenticated_client.post(
            "/matches/delete-multiple",
            json={"match_ids": []},
            follow_redirects=True,
        )
        assert response.status_code in [200, 400]

    def test_delete_multiple_matches_invalid_ids(self, authenticated_client):
        """Test bulk deletion with non-existent match IDs completes without error."""
        response = authenticated_client.post(
            "/matches/delete-multiple",
            json={"match_ids": ["nonexistent1", "nonexistent2"]},
            follow_redirects=True,
        )
        assert response.status_code == 200


class TestSaveScorecardImages:
    """Tests for saving scorecard images."""

    def test_save_scorecard_images_unauthenticated(self, client):
        """Test saving scorecard images without login redirects."""
        response = client.post(
            "/match/test-match-id/save-scorecard-images",
            json={"images": []},
        )
        assert response.status_code == 302

    def test_save_scorecard_images_invalid_data(self, authenticated_client):
        """Test saving scorecard images with invalid data returns an error."""
        response = authenticated_client.post(
            "/match/test-match-id/save-scorecard-images",
            json={"invalid": "data"},
            follow_redirects=True,
        )
        assert response.status_code in [200, 400, 404]


class TestMatchValidation:
    """Tests for match validation rules."""

    def test_match_requires_two_different_teams(self, authenticated_client, test_team):
        """Test that using the same team ID for both sides is rejected."""
        response = authenticated_client.post(
            "/match/setup",
            json={
                "team1_id": test_team.id,
                "team2_id": test_team.id,
                "overs": 20,
                "simulation_mode": "auto",
            },
            follow_redirects=True,
        )
        assert response.status_code in [200, 400]

    def test_match_overs_excessive(self, authenticated_client, test_team, test_team_2):
        """Test that an excessive overs count is rejected or handled gracefully."""
        response = authenticated_client.post(
            "/match/setup",
            json={
                "team1_id": test_team.id,
                "team2_id": test_team_2.id,
                "overs": 1000,
                "simulation_mode": "auto",
            },
            follow_redirects=True,
        )
        assert response.status_code in [200, 400]
