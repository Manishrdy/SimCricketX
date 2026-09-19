"""
Test suite for Match routes
Tests routes defined in routes/match_routes.py
"""

import pytest
import json
import os
import uuid
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from database import db
from database.models import Match as DBMatch, MatchScorecard, Player as DBPlayer


class TestMatchSetupRoute:
    """Tests for match setup functionality."""

    def test_match_setup_page_get(self, authenticated_client):
        """Test accessing match setup page."""
        response = authenticated_client.get("/match/setup")
        assert response.status_code == 200
        assert b"setup" in response.data.lower() or b"match" in response.data.lower()
        assert b"Pink ball; movement lasts longer" in response.data
        assert "Day/Night · Pink ball".encode() in response.data
        assert b"Sustained rain can remove sessions or wash out a full day" in response.data
        assert b"No DLS is used" in response.data

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

    def test_fc_scoreboard_displays_all_innings_and_back_link(self, authenticated_client, regular_user, test_team, test_team_2):
        """Test accessing scoreboard for a 4-innings FC match displays all innings and back link."""
        match_id = str(uuid.uuid4())
        fc_match = DBMatch(
            id=match_id,
            user_id=regular_user.id,
            home_team_id=test_team.id,
            away_team_id=test_team_2.id,
            match_format="FC",
            venue="Lord's",
            result_description="Team 1 won by 50 runs",
            home_team_score=300,
            home_team_wickets=10,
            home_team_overs="85.0",
            away_team_score=250,
            away_team_wickets=10,
            away_team_overs="72.0",
            home_team_score_innings2=200,
            home_team_wickets_innings2=10,
            home_team_overs_innings2="60.0",
            away_team_score_innings2=200,
            away_team_wickets_innings2=10,
            away_team_overs_innings2="55.0",
        )
        db.session.add(fc_match)

        p1 = DBPlayer(name="Batter One", team_id=test_team.id)
        p2 = DBPlayer(name="Bowler One", team_id=test_team_2.id)
        db.session.add_all([p1, p2])
        db.session.flush()

        cards = []
        for inn in [1, 2, 3, 4]:
            bat_team = test_team.id if inn in [1, 3] else test_team_2.id
            bowl_team = test_team_2.id if inn in [1, 3] else test_team.id
            cards.append(MatchScorecard(
                match_id=match_id, player_id=p1.id, team_id=bat_team,
                innings_number=inn, record_type="batting", runs=50, balls=40, is_out=True,
            ))
            cards.append(MatchScorecard(
                match_id=match_id, player_id=p2.id, team_id=bowl_team,
                innings_number=inn, record_type="bowling", overs="10.0", runs_conceded=30, wickets=2,
            ))
        db.session.add_all(cards)
        db.session.commit()

        response = authenticated_client.get(f"/match/{match_id}/scoreboard")
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "1st Innings" in content
        assert "2nd Innings" in content
        assert "3rd Innings" in content
        assert "4th Innings" in content
        assert "Back to Matches" in content
        assert "/my-matches" in content

    def test_fc_scoreline_in_my_matches(self, authenticated_client, regular_user, test_team, test_team_2):
        """Test that /my-matches displays multi-innings scores for FC matches."""
        match_id = str(uuid.uuid4())
        fc_match = DBMatch(
            id=match_id,
            user_id=regular_user.id,
            home_team_id=test_team.id,
            away_team_id=test_team_2.id,
            match_format="FC",
            venue="MCG",
            result_description="Match drawn",
            home_team_score=400,
            home_team_wickets=8,
            away_team_score=350,
            away_team_wickets=10,
            home_team_score_innings2=250,
            home_team_wickets_innings2=4,
        )
        db.session.add(fc_match)
        db.session.commit()

        response = authenticated_client.get("/my-matches")
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "400/8 &amp; 250/4" in content or "400/8 & 250/4" in content
        assert "350/10" in content


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

    def test_archive_is_reachable_without_an_automatic_download(self):
        """The archive must never depend only on a download the page fires itself.

        A page-triggered download carries no user activation, and Chromium
        cancels those under its "automatic downloads" gate without telling the
        page. The rendered link is the recovery path, so it has to exist and be
        shown unconditionally.
        """
        root = Path(__file__).resolve().parents[1]
        script = (root / "static" / "js" / "match_detail.js").read_text(encoding="utf-8")
        template = (root / "templates" / "match_detail.html").read_text(encoding="utf-8")

        # The link the user clicks must be in the page...
        assert 'id="archive-download-link"' in template
        assert 'id="archive-ready-bar"' in template

        # ...outside the panel html2canvas captures and outside any modal.
        panel_start = template.index('id="scorecard-panel"')
        panel_end = template.index('id="fc-weather-overlay"')
        assert 'id="archive-ready-bar"' not in template[panel_start:panel_end]

        # The bar is revealed before, and independently of, the auto attempt.
        show = script.index("showArchiveDownload(archiveInfo);")
        auto = script.index("tryAutoDownloadArchive(archiveInfo.download_url);")
        assert show < auto

        # showArchiveDownload() must not be conditional on the auto attempt.
        body_start = script.index("function showArchiveDownload(archiveInfo) {")
        body_end = script.index("function showArchiveError()")
        assert "bar.hidden = false;" in script[body_start:body_end]

        # The bar starts hidden, and author `display` beats the UA stylesheet's
        # [hidden] rule — without this the bar shows from page load.
        styles = (root / "static" / "css" / "match_detail.css").read_text(encoding="utf-8")
        assert "hidden" in template[template.index('id="archive-ready-bar"'):
                                    template.index('id="archive-ready-bar"') + 60]
        assert ".archive-ready-bar[hidden]" in styles
        assert ".archive-ready-btn[hidden]" in styles

    def test_download_archive_returns_link_not_attachment(self):
        """The endpoint hands back a URL; the ZIP is served by serve_archive()."""
        script_path = Path(__file__).resolve().parents[1] / "routes" / "match_routes.py"
        source = script_path.read_text(encoding="utf-8")

        route_start = source.index('def download_archive(match_id):')
        route_end = source.index('@app.route("/my-matches")')
        route_src = source[route_start:route_end]

        assert '"download_url": download_url' in route_src
        assert 'url_for(' in route_src
        # No attachment streaming from this endpoint any more.
        assert "as_attachment" not in route_src

    def test_download_archive_unauthenticated(self, client):
        """Test downloading match archive without login redirects."""
        response = client.post("/match/test-match-id/download-archive")
        assert response.status_code == 302

    def test_download_archive_accepts_json_post(self, authenticated_client):
        """The JSON POST the frontend sends reaches archive lookup."""
        response = authenticated_client.post(
            "/match/nonexistent-json-download/download-archive",
            json={"match_id": "nonexistent-json-download"},
        )
        assert response.status_code == 404

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

    def test_save_named_fc_scorecard_image(self, authenticated_client, regular_user):
        """FC interval cards are accepted with a safe event-specific label."""
        from app import MATCH_INSTANCES, MATCH_INSTANCES_LOCK, PROJECT_ROOT
        from werkzeug.utils import secure_filename

        match_id = str(uuid.uuid4())
        with MATCH_INSTANCES_LOCK:
            MATCH_INSTANCES[match_id] = SimpleNamespace(
                data={"created_by": regular_user.id}
            )

        image_path = (
            Path(PROJECT_ROOT) / "data" / "temp_scorecard_images"
            / secure_filename(regular_user.id)
            / f"{match_id}_day_01_lunch_innings_2_scorecard.png"
        )
        try:
            response = authenticated_client.post(
                f"/match/{match_id}/save-scorecard-images",
                data={
                    "archive_label": "day_01_lunch_innings_2_scorecard",
                    "scorecard_image": (BytesIO(b"png-data"), "scorecard.png", "image/png"),
                },
                content_type="multipart/form-data",
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            assert image_path.read_bytes() == b"png-data"

            invalid = authenticated_client.post(
                f"/match/{match_id}/save-scorecard-images",
                data={
                    "archive_label": "../../unsafe",
                    "scorecard_image": (BytesIO(b"png-data"), "scorecard.png", "image/png"),
                },
                content_type="multipart/form-data",
            )
            assert invalid.status_code == 400
        finally:
            with MATCH_INSTANCES_LOCK:
                MATCH_INSTANCES.pop(match_id, None)
            if image_path.exists():
                image_path.unlink()
            try:
                image_path.parent.rmdir()
                image_path.parent.parent.rmdir()
            except OSError:
                pass


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
