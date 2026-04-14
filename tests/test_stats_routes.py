"""
Test suite for Statistics routes
Tests routes defined in routes/stats_routes.py
"""

import logging
import uuid
from datetime import datetime

import pytest

from app import db
from database.models import (
    Match as DBMatch,
    MatchScorecard,
    Player as DBPlayer,
    Team as DBTeam,
    TeamProfile as DBTeamProfile,
)
from engine.stats_service import StatsService
from engine.tournament_engine import TournamentEngine
from match_archiver import MatchArchiver
from database.models import (
    Tournament,
    TournamentFixture,
    TournamentPlayerStatsCache,
)


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


class TestStumpingsFieldingStats:
    """Regression: stumpings must be recorded by the archiver and surfaced
    through the stats service. Before the fix, `_save_fielding_stats` silently
    dropped wickets with `wicket_type == 'Stumped'` because the lookup only
    matched 'caught' / 'run out'.
    """

    def _build_archiver_stub(self, match_id):
        """Build a MatchArchiver instance without running __init__ — we only
        need the three attributes used by `_save_fielding_stats`."""
        archiver = MatchArchiver.__new__(MatchArchiver)
        archiver.match_data = {"match_format": "T20"}
        archiver.match_id = match_id
        archiver.logger = logging.getLogger(__name__)
        return archiver

    def test_save_fielding_stats_records_stumping(self, regular_user):
        """A `Stumped` dismissal should increment the keeper's stumpings on
        their MatchScorecard, not silently disappear."""
        team = DBTeam(user_id=regular_user.id, name="Stumpers XI", short_code="STM")
        db.session.add(team)
        db.session.flush()

        profile = DBTeamProfile(team_id=team.id, format_type="T20")
        db.session.add(profile)
        db.session.flush()

        keeper = DBPlayer(
            team_id=team.id, profile_id=profile.id,
            name="Test Keeper", role="Wicketkeeper", is_wicketkeeper=True,
        )
        db.session.add(keeper)
        db.session.flush()

        match_id = str(uuid.uuid4())
        match = DBMatch(
            id=match_id, user_id=regular_user.id,
            home_team_id=team.id, away_team_id=team.id,
            match_format="T20", date=datetime.utcnow(),
        )
        db.session.add(match)
        db.session.commit()

        # Simulate the engine's batting_stats output: one batter stumped by
        # the keeper, one caught by the keeper. Run via real archiver method.
        batting_stats = {
            "batter_a": {"wicket_type": "Stumped", "fielder_out": "Test Keeper"},
            "batter_b": {"wicket_type": "Caught", "fielder_out": "Test Keeper"},
        }
        archiver = self._build_archiver_stub(match_id)
        archiver._save_fielding_stats(batting_stats, team.id, innings_number=1)
        db.session.commit()

        card = MatchScorecard.query.filter_by(
            match_id=match_id, player_id=keeper.id, innings_number=1
        ).first()
        assert card is not None
        assert card.stumpings == 1
        assert card.catches == 1
        assert card.run_outs == 0

    def test_save_fielding_stats_does_not_bleed_across_format_profiles(self, regular_user):
        """A T20 stumping must attach to the T20 keeper, not the ListA keeper
        of the same team that happens to share the name. Before the fix, the
        fallback at `_save_fielding_stats` matched any team-wide row and
        could silently assign stats to the wrong format profile.
        """
        team = DBTeam(user_id=regular_user.id, name="Cross XI", short_code="CRX")
        db.session.add(team)
        db.session.flush()

        t20_profile = DBTeamProfile(team_id=team.id, format_type="T20")
        list_a_profile = DBTeamProfile(team_id=team.id, format_type="ListA")
        db.session.add_all([t20_profile, list_a_profile])
        db.session.flush()

        # Same name, different profile rows — the realistic post-migration shape.
        t20_keeper = DBPlayer(
            team_id=team.id, profile_id=t20_profile.id,
            name="Same Name Keeper", role="Wicketkeeper", is_wicketkeeper=True,
        )
        list_a_keeper = DBPlayer(
            team_id=team.id, profile_id=list_a_profile.id,
            name="Same Name Keeper", role="Wicketkeeper", is_wicketkeeper=True,
        )
        db.session.add_all([t20_keeper, list_a_keeper])
        db.session.flush()

        match_id = str(uuid.uuid4())
        match = DBMatch(
            id=match_id, user_id=regular_user.id,
            home_team_id=team.id, away_team_id=team.id,
            match_format="T20", date=datetime.utcnow(),
        )
        db.session.add(match)
        db.session.commit()

        batting_stats = {
            "batter": {"wicket_type": "Stumped", "fielder_out": "Same Name Keeper"},
        }
        archiver = self._build_archiver_stub(match_id)
        archiver._save_fielding_stats(batting_stats, team.id, innings_number=1)
        db.session.commit()

        t20_card = MatchScorecard.query.filter_by(
            match_id=match_id, player_id=t20_keeper.id
        ).first()
        list_a_card = MatchScorecard.query.filter_by(
            match_id=match_id, player_id=list_a_keeper.id
        ).first()

        assert t20_card is not None and t20_card.stumpings == 1
        assert list_a_card is None, (
            "Stumping leaked into the ListA profile keeper — "
            "fielder fallback is matching cross-format rows."
        )

    def test_undefined_batting_average_is_none_not_runs(self, regular_user):
        """Players with zero dismissals must report avg=None, not their total
        runs. Before the fix, a 50 not-out in a single innings showed avg=50,
        wrongly placing tail-enders at the top of the Best Average leaderboard.
        """
        team = DBTeam(user_id=regular_user.id, name="NotOut XI", short_code="NOX")
        db.session.add(team)
        db.session.flush()
        profile = DBTeamProfile(team_id=team.id, format_type="T20")
        db.session.add(profile)
        db.session.flush()
        batter = DBPlayer(team_id=team.id, profile_id=profile.id, name="Tail Ender", role="Bowler")
        db.session.add(batter)
        db.session.flush()

        match_id = str(uuid.uuid4())
        match = DBMatch(
            id=match_id, user_id=regular_user.id,
            home_team_id=team.id, away_team_id=team.id,
            match_format="T20", date=datetime.utcnow(),
        )
        db.session.add(match)
        db.session.add(MatchScorecard(
            match_id=match_id, player_id=batter.id, team_id=team.id,
            innings_number=1, record_type="batting",
            runs=50, balls=20, is_out=False,
        ))
        db.session.commit()

        svc = StatsService(logging.getLogger(__name__))
        result = svc.get_player_profile(batter.id, regular_user.id)

        assert "error" not in result
        assert result["batting"]["runs"] == 50
        assert result["batting"]["not_outs"] == 1
        assert result["batting"]["average"] is None, (
            f"Expected average=None for 0-dismissal batter, got "
            f"{result['batting']['average']!r}"
        )

    def test_undefined_bowling_average_and_sr_are_none_not_zero(self, regular_user):
        """Bowlers with zero wickets must report avg=None and sr=None, not 0.
        Before the fix, a wicketless bowler showed avg=0, falsely appearing as
        the most economical wicket-taker ever.
        """
        team = DBTeam(user_id=regular_user.id, name="WicketLess XI", short_code="WLX")
        db.session.add(team)
        db.session.flush()
        profile = DBTeamProfile(team_id=team.id, format_type="T20")
        db.session.add(profile)
        db.session.flush()
        bowler = DBPlayer(team_id=team.id, profile_id=profile.id, name="Donkey", role="Bowler")
        db.session.add(bowler)
        db.session.flush()

        match_id = str(uuid.uuid4())
        match = DBMatch(
            id=match_id, user_id=regular_user.id,
            home_team_id=team.id, away_team_id=team.id,
            match_format="T20", date=datetime.utcnow(),
        )
        db.session.add(match)
        db.session.add(MatchScorecard(
            match_id=match_id, player_id=bowler.id, team_id=team.id,
            innings_number=1, record_type="bowling",
            balls_bowled=24, runs_conceded=40, wickets=0,
        ))
        db.session.commit()

        svc = StatsService(logging.getLogger(__name__))
        result = svc.get_player_profile(bowler.id, regular_user.id)

        assert "error" not in result
        assert result["bowling"]["wickets"] == 0
        assert result["bowling"]["average"] is None, (
            f"Expected average=None for 0-wicket bowler, got "
            f"{result['bowling']['average']!r}"
        )
        assert result["bowling"]["strike_rate"] is None, (
            f"Expected strike_rate=None for 0-wicket bowler, got "
            f"{result['bowling']['strike_rate']!r}"
        )

    def test_milestone_detection_handles_multi_event_jumps(self, regular_user):
        """A player who takes 2 wickets in one match, going from 24 → 26 total,
        must trigger the 25-wicket milestone. Before the fix, the detector
        used `prev = current - 1` regardless of actual delta and missed the
        crossing whenever delta > 1.
        """
        team = DBTeam(user_id=regular_user.id, name="Milestone XI", short_code="MIL")
        db.session.add(team)
        db.session.flush()
        profile = DBTeamProfile(team_id=team.id, format_type="T20")
        db.session.add(profile)
        db.session.flush()
        player = DBPlayer(
            team_id=team.id, profile_id=profile.id, name="Wicket Hauler",
            role="Bowler", total_wickets=26,  # post-update total
        )
        db.session.add(player)
        db.session.commit()

        # 2-wicket match: prev = 26 - 2 = 24, which is < 25 → milestone fires.
        ms = StatsService.detect_milestones(player.id, deltas={"wickets": 2})
        assert any("25 career wickets" in m for m in ms), (
            f"Expected 25-wicket milestone for 24→26 jump; got {ms!r}"
        )

        # 1-wicket match landing at 26 (came from 25): no NEW milestone — the
        # 25-wicket milestone should have fired the previous match.
        ms = StatsService.detect_milestones(player.id, deltas={"wickets": 1})
        assert not any("25 career wickets" in m for m in ms), (
            f"25-wicket milestone should not re-fire when prev>=25; got {ms!r}"
        )

    def test_undefined_batting_strike_rate_is_none_not_zero(self, regular_user):
        """A batter who faced 0 balls (e.g., non-striker when match ended)
        must report strike_rate=None, not 0. Mirrors the average convention.
        """
        team = DBTeam(user_id=regular_user.id, name="ZeroBalls XI", short_code="ZBX")
        db.session.add(team)
        db.session.flush()
        profile = DBTeamProfile(team_id=team.id, format_type="T20")
        db.session.add(profile)
        db.session.flush()
        batter = DBPlayer(team_id=team.id, profile_id=profile.id, name="Non Striker", role="Batsman")
        db.session.add(batter)
        db.session.flush()

        match_id = str(uuid.uuid4())
        match = DBMatch(
            id=match_id, user_id=regular_user.id,
            home_team_id=team.id, away_team_id=team.id,
            match_format="T20", date=datetime.utcnow(),
        )
        db.session.add(match)
        # Realistic non-striker scenario: batter ran 2 from the non-striker's
        # end without facing a ball. Counts as an innings (runs>0) but SR is
        # mathematically undefined since balls=0.
        db.session.add(MatchScorecard(
            match_id=match_id, player_id=batter.id, team_id=team.id,
            innings_number=1, record_type="batting",
            runs=2, balls=0, is_out=False,
        ))
        db.session.commit()

        svc = StatsService(logging.getLogger(__name__))
        result = svc.get_player_profile(batter.id, regular_user.id)

        assert "error" not in result
        assert result["batting"]["runs"] == 2
        assert result["batting"]["balls"] == 0
        assert result["batting"]["strike_rate"] is None, (
            f"Expected strike_rate=None for 0-ball batter, got "
            f"{result['batting']['strike_rate']!r}"
        )

    def test_tournament_cache_dismissals_consistent_with_innings(self, regular_user):
        """Cache rebuild must apply the same valid-innings filter to both
        innings_batted and not_outs so `dismissals = innings - not_outs` is
        internally consistent. Before the fix, a non-striker scorecard
        (balls=0, runs>0, not out) was counted in innings but excluded from
        not_outs, so dismissals over-counted by 1 and the average was
        deflated.
        """
        team = DBTeam(user_id=regular_user.id, name="Cache XI", short_code="CCH")
        db.session.add(team)
        db.session.flush()
        profile = DBTeamProfile(team_id=team.id, format_type="T20")
        db.session.add(profile)
        db.session.flush()
        batter = DBPlayer(team_id=team.id, profile_id=profile.id, name="Mixed Bag", role="Batsman")
        db.session.add(batter)
        db.session.flush()

        tournament = Tournament(
            user_id=regular_user.id, name="Cache Test Tourney",
            mode="round_robin", format_type="T20",
        )
        db.session.add(tournament)
        db.session.flush()

        # Build 5 batting cards across 5 matches in this tournament:
        # 3 dismissed innings, 1 standard not-out, 1 non-striker scenario.
        cards = [
            {"runs": 30, "balls": 25, "is_out": True},
            {"runs": 40, "balls": 30, "is_out": True},
            {"runs": 10, "balls": 12, "is_out": True},
            {"runs": 25, "balls": 20, "is_out": False},  # standard not-out
            {"runs": 2,  "balls": 0,  "is_out": False},  # non-striker
        ]
        last_match_id = None
        for spec in cards:
            mid = str(uuid.uuid4())
            db.session.add(DBMatch(
                id=mid, user_id=regular_user.id,
                home_team_id=team.id, away_team_id=team.id,
                match_format="T20", date=datetime.utcnow(),
                tournament_id=tournament.id,
            ))
            db.session.add(TournamentFixture(
                tournament_id=tournament.id,
                home_team_id=team.id, away_team_id=team.id,
                match_id=mid, status="Completed",
            ))
            db.session.add(MatchScorecard(
                match_id=mid, player_id=batter.id, team_id=team.id,
                innings_number=1, record_type="batting",
                runs=spec["runs"], balls=spec["balls"], is_out=spec["is_out"],
            ))
            last_match_id = mid
        db.session.commit()

        engine = TournamentEngine()
        last_match = db.session.get(DBMatch, last_match_id)
        engine._update_player_stats_cache(last_match)
        db.session.commit()

        cache = TournamentPlayerStatsCache.query.filter_by(
            tournament_id=tournament.id, player_id=batter.id
        ).first()
        assert cache is not None

        # 5 valid innings (all have runs>0 OR is_out), 2 not-outs (the 25*
        # and the 2-from-non-striker), 3 actual dismissals.
        assert cache.innings_batted == 5
        assert cache.not_outs == 2
        dismissals = cache.innings_batted - cache.not_outs
        assert dismissals == 3
        # Avg = total runs / dismissals = 107 / 3 ≈ 35.67. Before the fix,
        # dismissals=4 would have given 26.75.
        assert cache.batting_average == round(107 / 3, 2)

    def test_statistics_route_defaults_to_t20_not_all(self, authenticated_client):
        """The 'All formats' option was removed — visiting /statistics with
        no `match_format` param must default to T20 (a real format), not
        None (which previously meant 'aggregate across formats').
        """
        response = authenticated_client.get("/statistics?view=overall")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # T20 pill should be active; the "All" pill should be gone entirely.
        assert "selectFormat('T20')" in body
        assert ">All<" not in body

    def test_player_profile_route_defaults_to_player_profile_format(self, regular_user, authenticated_client):
        """A ListA-only player viewed without `?match_format=` should be
        served their ListA stats, not an empty T20 view. The route now
        looks up the player's profile format when none is supplied.
        """
        team = DBTeam(user_id=regular_user.id, name="Profile Default XI", short_code="PDX")
        db.session.add(team)
        db.session.flush()
        list_a_profile = DBTeamProfile(team_id=team.id, format_type="ListA")
        db.session.add(list_a_profile)
        db.session.flush()
        list_a_player = DBPlayer(
            team_id=team.id, profile_id=list_a_profile.id,
            name="ListA Only", role="Batsman",
        )
        db.session.add(list_a_player)
        db.session.commit()

        response = authenticated_client.get(f"/player/{list_a_player.id}")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # The ListA <option> in the format dropdown should be selected.
        assert 'value="ListA"' in body and "selected" in body

    def test_player_profile_surfaces_stumpings(self, regular_user):
        """`get_player_profile` must include stumpings in the fielding block
        and roll them into the dismissals total."""
        team = DBTeam(user_id=regular_user.id, name="Profile XI", short_code="PFX")
        db.session.add(team)
        db.session.flush()

        profile = DBTeamProfile(team_id=team.id, format_type="T20")
        db.session.add(profile)
        db.session.flush()

        keeper = DBPlayer(
            team_id=team.id, profile_id=profile.id,
            name="Profile Keeper", role="Wicketkeeper", is_wicketkeeper=True,
        )
        db.session.add(keeper)
        db.session.flush()

        match_id = str(uuid.uuid4())
        match = DBMatch(
            id=match_id, user_id=regular_user.id,
            home_team_id=team.id, away_team_id=team.id,
            match_format="T20", date=datetime.utcnow(),
        )
        db.session.add(match)
        db.session.add(MatchScorecard(
            match_id=match_id, player_id=keeper.id, team_id=team.id,
            innings_number=1, record_type="fielding",
            catches=2, run_outs=1, stumpings=3,
        ))
        db.session.commit()

        svc = StatsService(logging.getLogger(__name__))
        result = svc.get_player_profile(keeper.id, regular_user.id)

        assert "error" not in result
        fielding = result["fielding"]
        assert fielding["catches"] == 2
        assert fielding["run_outs"] == 1
        assert fielding["stumpings"] == 3
        # Dismissals total must include stumpings.
        assert fielding["total"] == 6
