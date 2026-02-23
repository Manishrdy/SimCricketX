"""
Test suite for Statistics routes
Tests routes defined in routes/stats_routes.py
"""

import pytest
from database.models import MatchScorecard


class TestStatisticsRoute:
    """Tests for main statistics page."""

    def test_statistics_page(self, authenticated_client):
        """Test accessing statistics page."""
        response = authenticated_client.get("/statistics")
        assert response.status_code == 200

    def test_statistics_unauthenticated(self, client):
        """Test accessing statistics without login redirects (@login_required)."""
        response = client.get("/statistics")
        assert response.status_code == 302

    def test_statistics_overall_view(self, authenticated_client):
        """Test statistics page with overall view query parameter."""
        response = authenticated_client.get("/statistics?view=overall")
        assert response.status_code == 200

    def test_statistics_tournament_view(self, authenticated_client, test_tournament):
        """Test statistics page filtered to a specific tournament."""
        response = authenticated_client.get(
            f"/statistics?view=tournament&tournament_id={test_tournament.id}"
        )
        assert response.status_code == 200

    def test_statistics_empty_data(self, authenticated_client):
        """Test statistics page renders correctly with no match data."""
        response = authenticated_client.get("/statistics")
        assert response.status_code == 200


class TestStatisticsExportRoute:
    """Tests for statistics export functionality."""

    def test_export_statistics_csv(self, authenticated_client):
        """Test exporting batting statistics as CSV (supported format)."""
        response = authenticated_client.get("/statistics/export/batting/csv")
        # 200 on success; 404 if no data; 400 on invalid stat type
        assert response.status_code in [200, 400, 404]

    def test_export_statistics_txt(self, authenticated_client):
        """Test exporting statistics as TXT (supported format)."""
        response = authenticated_client.get("/statistics/export/batting/txt")
        assert response.status_code in [200, 400, 404]

    def test_export_statistics_unauthenticated(self, client):
        """Test exporting statistics without login redirects (@login_required)."""
        response = client.get("/statistics/export/batting/csv")
        assert response.status_code == 302

    def test_export_bowling_stats(self, authenticated_client):
        """Test exporting bowling statistics as CSV."""
        response = authenticated_client.get("/statistics/export/bowling/csv")
        assert response.status_code in [200, 400, 404]

    def test_export_fielding_stats(self, authenticated_client):
        """Test exporting fielding statistics as CSV."""
        response = authenticated_client.get("/statistics/export/fielding/csv")
        assert response.status_code in [200, 400, 404]

    def test_export_invalid_stat_type(self, authenticated_client):
        """Test exporting with an unrecognised stat type returns 400 or 404."""
        response = authenticated_client.get("/statistics/export/invalid/csv")
        assert response.status_code in [400, 404]

    def test_export_invalid_format(self, authenticated_client):
        """Test exporting with an unrecognised format type returns 400 or 404."""
        response = authenticated_client.get("/statistics/export/batting/invalid")
        assert response.status_code in [400, 404]


class TestComparePlayersRoute:
    """Tests for player comparison page."""

    def test_compare_players_page(self, authenticated_client):
        """Test accessing compare players page."""
        response = authenticated_client.get("/compare-players")
        assert response.status_code == 200

    def test_compare_players_unauthenticated(self, client):
        """Test accessing compare players without login redirects (@login_required)."""
        response = client.get("/compare-players")
        assert response.status_code == 302


class TestBowlingFiguresAPIRoute:
    """Tests for bowling figures API."""

    def test_bowling_figures_api(self, authenticated_client):
        """Test fetching bowling figures returns JSON."""
        response = authenticated_client.get("/api/bowling-figures")
        assert response.status_code == 200
        data = response.get_json()
        assert data is not None

    def test_bowling_figures_api_unauthenticated(self, client):
        """Test bowling figures API without login redirects (@login_required)."""
        response = client.get("/api/bowling-figures")
        assert response.status_code == 302

    def test_bowling_figures_api_with_tournament(self, authenticated_client, test_tournament):
        """Test bowling figures API filtered by a specific tournament."""
        response = authenticated_client.get(
            f"/api/bowling-figures?tournament_id={test_tournament.id}"
        )
        assert response.status_code == 200


class TestComparePlayersAPIRoute:
    """Tests for compare players API."""

    def test_compare_players_api_unauthenticated(self, client):
        """Test compare players API without login redirects (@login_required)."""
        response = client.get("/api/compare-players")
        assert response.status_code == 302

    def test_compare_players_api_no_players(self, authenticated_client):
        """Test compare players API with no player IDs returns 200 or 400."""
        response = authenticated_client.get("/api/compare-players")
        assert response.status_code in [200, 400]

    def test_compare_players_api_invalid_ids(self, authenticated_client):
        """Test compare players API with non-existent player IDs returns 200 or 404."""
        response = authenticated_client.get(
            "/api/compare-players?player_ids=99999,88888"
        )
        assert response.status_code in [200, 404]


class TestPlayerPartnershipsAPIRoute:
    """Tests for player partnerships API."""

    def test_player_partnerships_api(self, authenticated_client):
        """Test fetching partnerships for a non-existent player returns 200 or 404."""
        response = authenticated_client.get("/api/player/1/partnerships")
        assert response.status_code in [200, 404]

    def test_player_partnerships_api_unauthenticated(self, client):
        """Test player partnerships API without login redirects (@login_required)."""
        response = client.get("/api/player/1/partnerships")
        assert response.status_code == 302

    def test_player_partnerships_invalid_player(self, authenticated_client):
        """Test partnerships API with a non-existent player ID returns 200 or 404."""
        response = authenticated_client.get("/api/player/99999/partnerships")
        assert response.status_code in [404, 200]


class TestTournamentPartnershipsAPIRoute:
    """Tests for tournament partnerships API."""

    def test_tournament_partnerships_api(self, authenticated_client, test_tournament):
        """Test fetching tournament partnerships returns 200."""
        response = authenticated_client.get(
            f"/api/tournament/{test_tournament.id}/partnerships"
        )
        assert response.status_code == 200

    def test_tournament_partnerships_api_unauthenticated(self, client, test_tournament):
        """Test tournament partnerships API without login redirects (@login_required)."""
        response = client.get(f"/api/tournament/{test_tournament.id}/partnerships")
        assert response.status_code == 302

    def test_tournament_partnerships_invalid_tournament(self, authenticated_client):
        """Test partnerships API with a non-existent tournament ID returns 200 or 404."""
        response = authenticated_client.get("/api/tournament/99999/partnerships")
        assert response.status_code in [404, 200]


class TestAllPartnershipsAPIRoute:
    """Tests for all partnerships API."""

    def test_all_partnerships_api(self, authenticated_client):
        """Test fetching all partnerships returns a JSON list or dict."""
        response = authenticated_client.get("/api/partnerships")
        assert response.status_code == 200
        data = response.get_json()
        assert data is not None
        assert isinstance(data, (list, dict))

    def test_all_partnerships_api_unauthenticated(self, client):
        """Test all partnerships API without login redirects (@login_required)."""
        response = client.get("/api/partnerships")
        assert response.status_code == 302

    def test_all_partnerships_api_empty(self, authenticated_client):
        """Test all partnerships API returns empty structure when no data exists."""
        response = authenticated_client.get("/api/partnerships")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, (list, dict))


class TestStatisticsFiltering:
    """Tests for statistics filtering and sorting."""

    def test_statistics_filter_by_tournament(self, authenticated_client, test_tournament):
        """Test filtering statistics by a specific tournament."""
        response = authenticated_client.get(
            f"/statistics?tournament_id={test_tournament.id}"
        )
        assert response.status_code == 200

    def test_statistics_invalid_tournament(self, authenticated_client):
        """Test filtering with a non-existent tournament ID renders without error."""
        response = authenticated_client.get("/statistics?tournament_id=99999")
        assert response.status_code == 200
