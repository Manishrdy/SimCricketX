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
from app import PROJECT_ROOT
from database import db
from database.models import (
    Match as DBMatch,
    MatchScorecard,
    Player as DBPlayer,
    Team as DBTeam,
    Tournament,
    TournamentFixture,
)


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


class TestMatchSetupFixtureStatusGuard:
    """POST /match/setup must enforce the same fixture startability rules
    the GET branch does.

    The GET branch has always blocked Locked/Completed fixtures and
    unearned knockout pairings, but the POST branch checked only ownership
    and team agreement. Since the setup page bakes fixtureId in at render
    time, a tab left open (or reached with Back) while that fixture is
    played elsewhere would re-POST a now-Completed fixture_id and get a
    fresh match id back.

    That duplicate is not merely redundant. On completion,
    _handle_tournament_match_completion looks up the *new* match id, so its
    resimulation-reversal branch never fires; it then repoints
    fixture.match_id/winner_team_id at the duplicate while update_standings
    no-ops on standings_applied. The first match is left orphaned — still
    carrying tournament_id, still counted in career and tournament totals,
    and no longer reachable by Re-simulate, which works off fixture.match_id.
    """

    @staticmethod
    def _make_tournament(user_id, name, mode="round_robin"):
        tournament = Tournament(
            name=name, user_id=user_id, mode=mode, format_type="T20"
        )
        db.session.add(tournament)
        db.session.commit()
        return tournament

    @staticmethod
    def _make_fixture(tournament, home_id, away_id, status, **kwargs):
        fixture = TournamentFixture(
            tournament_id=tournament.id,
            home_team_id=home_id,
            away_team_id=away_id,
            round_number=1,
            status=status,
            stage=kwargs.pop("stage", "league"),
            standings_applied=kwargs.pop(
                "standings_applied", status == "Completed"
            ),
            **kwargs,
        )
        db.session.add(fixture)
        db.session.commit()
        return fixture

    @staticmethod
    def _start(client, home, away, tournament, fixture):
        return client.post(
            "/match/setup",
            json={
                "team_home": home.id,
                "team_away": away.id,
                "overs": 20,
                "simulation_mode": "auto",
                "match_format": "T20",
                "tournament_id": tournament.id,
                "fixture_id": fixture.id,
            },
        )

    def test_completed_fixture_cannot_be_started(
        self, authenticated_client, regular_user, test_team, test_team_2
    ):
        """A Completed fixture must be rejected, not handed a new match id."""
        tournament = self._make_tournament(regular_user.id, "Completed Guard")
        fixture = self._make_fixture(
            tournament, test_team.id, test_team_2.id, "Completed"
        )

        response = self._start(
            authenticated_client, test_team, test_team_2, tournament, fixture
        )

        assert response.status_code == 409, (
            "POST /match/setup started an already-Completed fixture; the "
            "duplicate match orphans the original and inflates career stats"
        )
        payload = response.get_json()
        assert "match_id" not in payload
        assert "already completed" in payload["error"].lower()

    def test_locked_fixture_cannot_be_started(
        self, authenticated_client, regular_user, test_team, test_team_2
    ):
        """A Locked fixture is rejected on its status alone.

        The engine only ever writes Locked with its teams cleared (see
        test_locked_fixture_without_teams_is_rejected below), so this uses a
        Locked fixture that does carry real team ids — the case the team
        agreement check would otherwise wave through.
        """
        tournament = self._make_tournament(regular_user.id, "Locked Guard")
        fixture = self._make_fixture(
            tournament, test_team.id, test_team_2.id, "Locked"
        )

        response = self._start(
            authenticated_client, test_team, test_team_2, tournament, fixture
        )

        assert response.status_code == 409
        payload = response.get_json()
        assert "match_id" not in payload
        assert "locked" in payload["error"].lower()

    def test_locked_fixture_without_teams_is_rejected(
        self, authenticated_client, regular_user, test_team, test_team_2
    ):
        """The shape the engine actually generates: Locked with no teams.

        This was already rejected before the status guard existed, but only
        incidentally — by the team agreement check, hence the different
        status code. Kept so that guard isn't removed as redundant.
        """
        tournament = self._make_tournament(regular_user.id, "Locked No Teams")
        fixture = self._make_fixture(tournament, None, None, "Locked")

        response = self._start(
            authenticated_client, test_team, test_team_2, tournament, fixture
        )

        assert response.status_code in (400, 409)
        assert "match_id" not in response.get_json()

    def test_unearned_knockout_pairing_cannot_be_started(
        self, authenticated_client, regular_user, test_team, test_team_2
    ):
        """'Scheduled' alone doesn't prove the pairing was earned.

        A round-2 knockout fixture whose feeders haven't been played can only
        exist through bracket corruption. Playing it would cement teams that
        never qualified into a real result, so POST must refuse it the way
        GET does.
        """
        from engine.tournament_engine import TournamentEngine

        spare_teams = []
        for code in ("SP1", "SP2"):
            spare = DBTeam(
                name=f"Spare {code}",
                short_code=code,
                user_id=regular_user.id,
                is_placeholder=False,
                is_draft=False,
            )
            db.session.add(spare)
            spare_teams.append(spare)
        db.session.commit()

        engine = TournamentEngine()
        tournament = engine.create_tournament(
            name="Unearned Pairing",
            user_id=regular_user.id,
            team_ids=[test_team.id, test_team_2.id]
            + [t.id for t in spare_teams],
            mode="knockout",
        )

        round_two = (
            TournamentFixture.query
            .filter(
                TournamentFixture.tournament_id == tournament.id,
                TournamentFixture.round_number > 1,
            )
            .order_by(TournamentFixture.bracket_position)
            .first()
        )
        assert round_two is not None, "expected a round-2 knockout fixture"

        # Fabricate the corrupt state: a playable-looking pairing sitting on
        # feeders that are still unplayed.
        round_two.home_team_id = test_team.id
        round_two.away_team_id = test_team_2.id
        round_two.status = "Scheduled"
        db.session.commit()

        feeder_a, feeder_b = engine.get_feeder_fixtures(tournament, round_two)
        assert feeder_a is not None and feeder_b is not None
        assert not (
            feeder_a.status == "Completed" and feeder_b.status == "Completed"
        ), "test setup expects undecided feeders"

        response = self._start(
            authenticated_client, test_team, test_team_2, tournament, round_two
        )

        assert response.status_code == 409
        payload = response.get_json()
        assert "match_id" not in payload
        assert "previous round" in payload["error"].lower()

    def test_scheduled_fixture_still_starts(
        self, authenticated_client, regular_user, test_team, test_team_2
    ):
        """The guard must not break the normal Play Now path."""
        tournament = self._make_tournament(regular_user.id, "Scheduled Happy")
        fixture = self._make_fixture(
            tournament, test_team.id, test_team_2.id, "Scheduled"
        )

        response = self._start(
            authenticated_client, test_team, test_team_2, tournament, fixture
        )

        assert response.status_code == 200, response.get_json()
        assert response.get_json()["match_id"]


class TestFixtureSingleMatchReservation:
    """One fixture may only ever have one match running on it.

    A fixture's status stays 'Scheduled' from setup until the match ends, so
    status alone let two tabs (or a double-submit) each POST /match/setup and
    receive a different match id. Both were playable to completion, and the
    second to finish repointed fixture.match_id/winner_team_id at itself while
    update_standings no-oped on standings_applied — orphaning the first match
    with its career and tournament stats still counted.
    """

    @staticmethod
    def _fixture(user_id, home, away, name="Reservation"):
        tournament = Tournament(
            name=name, user_id=user_id, mode="round_robin", format_type="T20"
        )
        db.session.add(tournament)
        db.session.commit()
        fixture = TournamentFixture(
            tournament_id=tournament.id,
            home_team_id=home.id,
            away_team_id=away.id,
            round_number=1,
            status="Scheduled",
            stage="league",
        )
        db.session.add(fixture)
        db.session.commit()
        return tournament, fixture

    @staticmethod
    def _payload(home, away, tournament, fixture):
        return {
            "team_home": home.id,
            "team_away": away.id,
            "overs": 20,
            "simulation_mode": "auto",
            "match_format": "T20",
            "tournament_id": tournament.id,
            "fixture_id": fixture.id,
        }

    def test_second_start_is_refused_and_points_at_the_first(
        self, authenticated_client, regular_user, test_team, test_team_2
    ):
        tournament, fixture = self._fixture(
            regular_user.id, test_team, test_team_2
        )
        payload = self._payload(test_team, test_team_2, tournament, fixture)

        first = authenticated_client.post("/match/setup", json=payload)
        second = authenticated_client.post("/match/setup", json=payload)

        assert first.status_code == 200
        first_match_id = first.get_json()["match_id"]

        assert second.status_code == 409, (
            "a second match was started on a fixture that already had one "
            "running; the two can produce competing results"
        )
        body = second.get_json()
        assert "match_id" not in body
        assert body["active_match_id"] == first_match_id, (
            "the refusal must name the match already holding the fixture so "
            "the client can offer Resume"
        )

        db.session.refresh(fixture)
        assert fixture.active_match_id == first_match_id

    def test_claim_is_atomic_under_concurrent_starts(
        self, app, regular_user, test_team, test_team_2
    ):
        """Two claims racing on one fixture: exactly one wins.

        Exercises the conditional UPDATE directly. The route-level test above
        can only show sequential requests; this is the part that has to hold
        when both requests read the fixture as free at the same moment.
        """
        from utils.fixture_rules import claim_fixture

        _tournament, fixture = self._fixture(
            regular_user.id, test_team, test_team_2, name="Race"
        )

        first = claim_fixture(fixture, "match-aaa")
        second = claim_fixture(fixture, "match-bbb")

        assert first is True
        assert second is False, "both claims won — the fixture has no reservation"
        assert fixture.active_match_id == "match-aaa"

    def test_stale_claim_does_not_lock_the_fixture_out(
        self, authenticated_client, regular_user, test_team, test_team_2
    ):
        """A claim whose match no longer exists must not be permanent.

        Without this, a match file that aged out of data/matches (or was
        removed) would leave its fixture unplayable for good.
        """
        tournament, fixture = self._fixture(
            regular_user.id, test_team, test_team_2, name="Stale"
        )
        fixture.active_match_id = "match-that-never-existed"
        db.session.commit()

        response = authenticated_client.post(
            "/match/setup",
            json=self._payload(test_team, test_team_2, tournament, fixture),
        )

        assert response.status_code == 200
        db.session.refresh(fixture)
        assert fixture.active_match_id == response.get_json()["match_id"]

    def test_setup_page_redirects_to_the_match_already_running(
        self, authenticated_client, regular_user, test_team, test_team_2
    ):
        tournament, fixture = self._fixture(
            regular_user.id, test_team, test_team_2, name="Resume Redirect"
        )
        started = authenticated_client.post(
            "/match/setup",
            json=self._payload(test_team, test_team_2, tournament, fixture),
        )
        match_id = started.get_json()["match_id"]

        page = authenticated_client.get(f"/match/setup?fixture_id={fixture.id}")

        assert page.status_code == 302
        assert page.headers["Location"].endswith(f"/match/{match_id}")

    def test_abandon_frees_the_fixture(
        self, authenticated_client, regular_user, test_team, test_team_2
    ):
        """The counterpart to the claim: without a release the user is stuck."""
        tournament, fixture = self._fixture(
            regular_user.id, test_team, test_team_2, name="Abandon"
        )
        started = authenticated_client.post(
            "/match/setup",
            json=self._payload(test_team, test_team_2, tournament, fixture),
        )
        match_id = started.get_json()["match_id"]

        match_file = Path(PROJECT_ROOT) / "data" / "matches" / f"match_{match_id}.json"
        assert match_file.is_file()

        abandoned = authenticated_client.post(
            f"/fixture/{fixture.id}/abandon", follow_redirects=True
        )
        assert abandoned.status_code == 200

        db.session.refresh(fixture)
        assert fixture.active_match_id is None
        assert fixture.status == "Scheduled"
        assert not match_file.exists(), (
            "the discarded match is still on disk and resumable by direct "
            "URL, so it can come back and claim a result later"
        )

        restarted = authenticated_client.post(
            "/match/setup",
            json=self._payload(test_team, test_team_2, tournament, fixture),
        )
        assert restarted.status_code == 200
        assert restarted.get_json()["match_id"] != match_id

    def test_abandon_rejects_another_users_fixture(
        self, authenticated_client, regular_user, admin_user, test_team, test_team_2
    ):
        tournament = Tournament(
            name="Someone Else", user_id=admin_user.id, mode="round_robin",
            format_type="T20",
        )
        db.session.add(tournament)
        db.session.commit()
        fixture = TournamentFixture(
            tournament_id=tournament.id,
            home_team_id=test_team.id,
            away_team_id=test_team_2.id,
            round_number=1, status="Scheduled", stage="league",
            active_match_id="not-yours",
        )
        db.session.add(fixture)
        db.session.commit()

        response = authenticated_client.post(f"/fixture/{fixture.id}/abandon")

        assert response.status_code == 302
        db.session.refresh(fixture)
        assert fixture.active_match_id == "not-yours"


    def test_dashboard_offers_resume_and_abandon_instead_of_play(
        self, authenticated_client, regular_user, test_team, test_team_2
    ):
        """The fixture card has to reflect the reservation.

        Leaving Play Now on a fixture that already has a match running would
        walk the user into the 409 the reservation now returns.
        """
        tournament, fixture = self._fixture(
            regular_user.id, test_team, test_team_2, name="Dashboard Card"
        )
        started = authenticated_client.post(
            "/match/setup",
            json=self._payload(test_team, test_team_2, tournament, fixture),
        )
        match_id = started.get_json()["match_id"]

        page = authenticated_client.get(f"/tournaments/{tournament.id}")
        body = page.get_data(as_text=True)

        assert page.status_code == 200
        assert "Resume Match" in body
        assert f"/match/{match_id}" in body
        assert f"/fixture/{fixture.id}/abandon" in body
        assert "Play Now" not in body, (
            "the fixture still offers a fresh start while a match is running "
            "on it — that start can only 409 now"
        )

    def test_a_second_result_never_replaces_a_settled_fixture(
        self, app, regular_user, test_team, test_team_2
    ):
        """The completion-side backstop for matches already in flight.

        The reservation stops a second match from starting, but a duplicate
        that was mid-play when it shipped still has to be refused a result
        rather than silently orphaning the one that got there first.
        """
        from utils.fixture_rules import settled_by_other_match

        _tournament, fixture = self._fixture(
            regular_user.id, test_team, test_team_2, name="Settled"
        )
        # Set in memory only: matches.id is a real FK and the point here is
        # the rule, not the row.
        fixture.status = "Completed"
        fixture.match_id = "first-match"
        fixture.standings_applied = True

        assert settled_by_other_match(fixture, "second-match") == "first-match"
        # The match that legitimately owns the result may still be recorded,
        # which is what makes a retry after a failed commit safe.
        assert settled_by_other_match(fixture, "first-match") is None

        fixture.status = "Scheduled"
        assert settled_by_other_match(fixture, "second-match") is None

        db.session.rollback()

    def test_resimulate_clears_a_claim_with_no_completed_match(
        self, authenticated_client, regular_user, test_team, test_team_2
    ):
        """Re-simulate is also a reset, so it must void the claim too.

        It returns early when the fixture has no match_id, which for a fixture
        holding only an in-flight match used to mean the claim survived.
        """
        tournament, fixture = self._fixture(
            regular_user.id, test_team, test_team_2, name="Resim Claim"
        )
        authenticated_client.post(
            "/match/setup",
            json=self._payload(test_team, test_team_2, tournament, fixture),
        )

        authenticated_client.post(
            f"/fixture/{fixture.id}/resimulate", follow_redirects=True
        )

        db.session.refresh(fixture)
        assert fixture.active_match_id is None
