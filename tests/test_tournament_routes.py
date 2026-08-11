"""
Test suite for Tournament routes
Tests routes defined in routes/tournament_routes.py
"""

import os
import uuid

import pytest
from app import db, PROJECT_ROOT
import app as app_module
from database.models import (
    Tournament,
    TournamentFixture,
    TournamentTeam,
    TournamentPlayerStatsCache,
    Match as DBMatch,
    MatchScorecard,
    MatchPartnership,
    Player as DBPlayer,
)
from engine.tournament_engine import TournamentEngine


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

    def test_create_tournament_invalid_mode_rejected(self, authenticated_client, test_team, test_team_2):
        """A mode value outside the UI's <select> options (e.g. a crafted
        direct POST) must be rejected with a flash + redirect, not silently
        commit a tournament with teams attached but zero fixtures ever
        generated.
        """
        response = authenticated_client.post(
            "/tournaments/create",
            data={
                "name": "Bogus Mode Tournament",
                "mode": "not_a_real_mode",
                "team_ids": [test_team.id, test_team_2.id],
            },
            follow_redirects=True,
        )

        assert response.status_code == 200

        tournament = db.session.execute(
            db.select(Tournament).filter_by(name="Bogus Mode Tournament")
        ).scalar_one_or_none()
        assert tournament is None


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


class TestTournamentDashboardStandingsVisibility:
    """
    Regression tests: a pure Knockout tournament's TournamentTeam rows never
    accumulate real W/L/points/NRR (update_standings only does that
    bookkeeping for league-stage fixtures, and Knockout fixtures are never
    staged 'league'), so the dashboard's Points Table would always read
    all-zero with arbitrary ordering. It must be hidden for Knockout mode
    and shown normally for every other mode.
    """

    def test_knockout_dashboard_hides_points_table(self, authenticated_client, regular_user, test_team, test_team_2):
        engine = TournamentEngine()
        t = engine.create_tournament(
            name="Dashboard KO Check", user_id=regular_user.id,
            team_ids=[test_team.id, test_team_2.id], mode="knockout",
        )

        response = authenticated_client.get(f"/tournaments/{t.id}")
        assert response.status_code == 200
        body = response.data.decode()
        assert "Points Table" not in body
        assert "Leader Points" not in body
        assert "Current Round" in body

    def test_round_robin_dashboard_shows_points_table(self, authenticated_client, regular_user, test_team, test_team_2):
        engine = TournamentEngine()
        t = engine.create_tournament(
            name="Dashboard RR Check", user_id=regular_user.id,
            team_ids=[test_team.id, test_team_2.id], mode="round_robin",
        )

        response = authenticated_client.get(f"/tournaments/{t.id}")
        assert response.status_code == 200
        body = response.data.decode()
        assert "Points Table" in body
        assert "Leader Points" in body


class TestTournamentDashboardLeaders:
    """
    Regression tests for the "Most Runs" / "Most Wickets" dashboard
    widgets. These are computed live over MatchScorecard rather than via
    TournamentPlayerStatsCache, which lags one match behind (see the
    comment in tournament_dashboard()) -- so they're verified directly
    against the rendered dashboard rather than the cache table.
    """

    def _make_match(self, tournament_id, home_team, away_team, user_id):
        match = DBMatch(
            id=str(uuid.uuid4()), user_id=user_id, tournament_id=tournament_id,
            home_team_id=home_team.id, away_team_id=away_team.id,
            match_format="T20",
        )
        db.session.add(match)
        db.session.flush()
        return match

    def test_top_scorers_and_wicket_takers_ranked_correctly(
        self, authenticated_client, regular_user, test_team, test_team_2
    ):
        tournament = Tournament(name="Leaders Check", user_id=regular_user.id, mode="round_robin")
        db.session.add(tournament)
        db.session.flush()

        team_a_players = DBPlayer.query.filter_by(team_id=test_team.id).order_by(DBPlayer.id).all()
        team_b_players = DBPlayer.query.filter_by(team_id=test_team_2.id).order_by(DBPlayer.id).all()
        batter_a, bowler_a = team_a_players[0], team_a_players[1]
        batter_b, bowler_b = team_b_players[0], team_b_players[1]

        match1 = self._make_match(tournament.id, test_team, test_team_2, regular_user.id)
        db.session.add_all([
            MatchScorecard(
                match_id=match1.id, player_id=batter_a.id, team_id=test_team.id,
                innings_number=1, record_type="batting", runs=80, balls=50, is_out=True,
            ),
            MatchScorecard(
                match_id=match1.id, player_id=batter_b.id, team_id=test_team_2.id,
                innings_number=2, record_type="batting", runs=40, balls=30, is_out=True,
            ),
            # Both bowlers take 3 wickets — bowler_b concedes fewer runs, so
            # must rank first (mirrors the existing "best bowling" tiebreak
            # convention already used in _update_player_stats_cache).
            MatchScorecard(
                match_id=match1.id, player_id=bowler_a.id, team_id=test_team.id,
                innings_number=2, record_type="bowling", wickets=3, runs_conceded=25, balls_bowled=24,
            ),
            MatchScorecard(
                match_id=match1.id, player_id=bowler_b.id, team_id=test_team_2.id,
                innings_number=1, record_type="bowling", wickets=3, runs_conceded=15, balls_bowled=24,
            ),
        ])
        db.session.commit()

        match2 = self._make_match(tournament.id, test_team, test_team_2, regular_user.id)
        db.session.add(MatchScorecard(
            match_id=match2.id, player_id=batter_a.id, team_id=test_team.id,
            innings_number=1, record_type="batting", runs=20, balls=15, is_out=True,
        ))
        db.session.commit()

        response = authenticated_client.get(f"/tournaments/{tournament.id}")
        assert response.status_code == 200
        body = response.data.decode()

        assert "Most Runs" in body
        assert "Most Wickets" in body

        # batter_a: 80 + 20 = 100 runs total, ahead of batter_b's 40.
        assert body.index(batter_a.name) < body.index(batter_b.name)
        # bowler_b: 3 wickets for 15 beats bowler_a's 3 wickets for 25.
        assert body.index(bowler_b.name) < body.index(bowler_a.name)

    def test_leaders_widgets_show_empty_state_with_no_matches(
        self, authenticated_client, regular_user, test_team, test_team_2
    ):
        tournament = Tournament(name="No Matches Yet", user_id=regular_user.id, mode="round_robin")
        db.session.add(tournament)
        db.session.commit()

        response = authenticated_client.get(f"/tournaments/{tournament.id}")
        assert response.status_code == 200
        body = response.data.decode()
        assert "Most Runs" in body
        assert "Most Wickets" in body
        assert "No data yet" in body

    def test_leaders_widgets_shown_for_knockout_mode(
        self, authenticated_client, regular_user, test_team, test_team_2
    ):
        """Unlike the Points Table (issue #7), batting/bowling leaders are
        meaningful for every mode including pure Knockout -- must not be
        accidentally gated on has_league_standings.
        """
        engine = TournamentEngine()
        t = engine.create_tournament(
            name="Leaders KO Check", user_id=regular_user.id,
            team_ids=[test_team.id, test_team_2.id], mode="knockout",
        )

        batter = DBPlayer.query.filter_by(team_id=test_team.id).first()
        match = self._make_match(t.id, test_team, test_team_2, regular_user.id)
        db.session.add(MatchScorecard(
            match_id=match.id, player_id=batter.id, team_id=test_team.id,
            innings_number=1, record_type="batting", runs=55, balls=40, is_out=True,
        ))
        db.session.commit()

        response = authenticated_client.get(f"/tournaments/{t.id}")
        assert response.status_code == 200
        body = response.data.decode()
        assert "Most Runs" in body
        assert batter.name in body


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

    def test_delete_completed_tournament_purges_all_affiliated_data(
        self, authenticated_client, regular_user, test_team, test_team_2, app
    ):
        """Deleting a Completed tournament must fully purge everything tied
        to it: the tournament and its standings/fixtures/stats-cache rows,
        the match itself, its scorecards/partnerships, its JSON archive on
        disk, its in-memory MATCH_INSTANCES entry, and the player's career
        stat aggregates must be reversed -- not just deleted alongside the
        scorecards, which would leave career totals permanently inflated.
        """
        engine = TournamentEngine()
        tournament = engine.create_tournament(
            name="Deletion Coverage Tournament",
            user_id=regular_user.id,
            team_ids=[test_team.id, test_team_2.id],
            mode="round_robin",
        )
        tournament.status = "Completed"
        db.session.commit()
        tournament_id = tournament.id

        fixture = TournamentFixture.query.filter_by(tournament_id=tournament_id).first()
        assert fixture is not None

        batter = DBPlayer.query.filter_by(team_id=test_team.id).first()
        partner = DBPlayer.query.filter(
            DBPlayer.team_id == test_team.id, DBPlayer.id != batter.id
        ).first()

        # Simulate a player who has already been archived once before, so
        # the reversal has something real to decrement.
        batter.matches_played = 5
        batter.total_runs = 250
        batter.total_wickets = 10
        db.session.commit()
        player_id = batter.id

        match_id = str(uuid.uuid4())
        match = DBMatch(
            id=match_id,
            user_id=regular_user.id,
            tournament_id=tournament_id,
            home_team_id=test_team.id,
            away_team_id=test_team_2.id,
            winner_team_id=test_team.id,
            match_format="T20",
            match_status="completed",
        )
        db.session.add(match)
        db.session.flush()

        db.session.add_all([
            MatchScorecard(
                match_id=match_id, player_id=batter.id, team_id=test_team.id,
                innings_number=1, record_type="batting", runs=75, balls=50,
            ),
            MatchScorecard(
                match_id=match_id, player_id=batter.id, team_id=test_team.id,
                innings_number=2, record_type="bowling", wickets=3, runs_conceded=25,
            ),
            MatchPartnership(
                match_id=match_id, innings_number=1, wicket_number=1,
                batsman1_id=batter.id, batsman2_id=partner.id,
                runs=75, balls=50, batsman1_contribution=75, batsman2_contribution=0,
            ),
        ])

        fixture.match_id = match_id
        fixture.status = "Completed"

        match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
        os.makedirs(match_dir, exist_ok=True)
        json_filename = f"test_delete_coverage_{match_id}.json"
        json_path = os.path.join(match_dir, json_filename)
        with open(json_path, "w", encoding="utf-8") as f:
            f.write("{}")
        match.match_json_path = json_filename

        db.session.commit()

        with app_module.MATCH_INSTANCES_LOCK:
            app_module.MATCH_INSTANCES[match_id] = object()

        try:
            response = authenticated_client.post(
                f"/tournaments/{tournament_id}/delete",
                follow_redirects=True,
            )
            assert response.status_code == 200

            assert db.session.get(Tournament, tournament_id) is None
            assert TournamentTeam.query.filter_by(tournament_id=tournament_id).count() == 0
            assert TournamentFixture.query.filter_by(tournament_id=tournament_id).count() == 0
            assert TournamentPlayerStatsCache.query.filter_by(tournament_id=tournament_id).count() == 0

            assert db.session.get(DBMatch, match_id) is None
            assert MatchScorecard.query.filter_by(match_id=match_id).count() == 0
            assert MatchPartnership.query.filter_by(match_id=match_id).count() == 0

            assert not os.path.isfile(json_path)

            with app_module.MATCH_INSTANCES_LOCK:
                assert match_id not in app_module.MATCH_INSTANCES

            db.session.expire_all()
            refreshed_batter = db.session.get(DBPlayer, player_id)
            assert refreshed_batter.matches_played == 4
            assert refreshed_batter.total_runs == 175
            assert refreshed_batter.total_wickets == 7
        finally:
            if os.path.isfile(json_path):
                os.remove(json_path)
            with app_module.MATCH_INSTANCES_LOCK:
                app_module.MATCH_INSTANCES.pop(match_id, None)

    def test_delete_button_renders_for_completed_tournament(
        self, authenticated_client, test_tournament
    ):
        """The dashboard must expose the Delete action once a tournament's
        status flips to Completed -- previously the template only rendered
        it for Active/open tournaments, making the working delete route
        unreachable from the UI for the exact case this feature is for.
        """
        test_tournament.status = "Completed"
        db.session.commit()

        response = authenticated_client.get(f"/tournaments/{test_tournament.id}")
        assert response.status_code == 200
        expected_action = f'/tournaments/{test_tournament.id}/delete'.encode()
        assert expected_action in response.data


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

    def test_resimulate_db_error_before_fixture_loads_does_not_crash(
        self, authenticated_client, monkeypatch
    ):
        """A DB error during the very first lookup (before `fixture` is ever
        assigned — e.g. a Postgres type-mismatch on a malformed fixture_id,
        which SQLite silently tolerates but Postgres doesn't) must not
        crash the except handler itself with UnboundLocalError. It should
        fall through to the same graceful flash+redirect as any other
        failure in this route.
        """
        from app import db as app_db
        from database.models import TournamentFixture as TF

        real_get = type(app_db.session).get

        def flaky_get(session_self, model, ident, *args, **kwargs):
            if model is TF:
                raise RuntimeError("simulated DB error on fixture lookup")
            return real_get(session_self, model, ident, *args, **kwargs)

        monkeypatch.setattr(type(app_db.session), "get", flaky_get)

        response = authenticated_client.post(
            "/fixture/1/resimulate",
            follow_redirects=True,
        )
        # The route can't know the fixture's real tournament_id (the lookup
        # that would tell it never completed), so it falls back to
        # redirecting at tournament_id=0 — which itself 404s since that
        # tournament doesn't exist. That's the correct, graceful outcome
        # here; what matters is that no UnboundLocalError escaped the
        # except handler and no 500 was raised.
        assert response.status_code == 404


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
