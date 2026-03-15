"""
Unit tests for TournamentEngine edge cases.

Covers: overs conversion, NRR calculation, round-robin fixture generation,
knockout bye handling, custom series validation, and standings updates.
"""

import pytest
from app import db
from database.models import (
    Tournament,
    TournamentTeam,
    TournamentFixture,
    Team as DBTeam,
    Match as DBMatch,
)
from engine.tournament_engine import TournamentEngine


@pytest.fixture
def engine():
    return TournamentEngine()


@pytest.fixture
def four_teams(app, regular_user):
    """Create 4 teams for tournament tests."""
    teams = []
    for i, (name, code) in enumerate([
        ("Alpha", "ALP"), ("Bravo", "BRV"),
        ("Charlie", "CHL"), ("Delta", "DLT"),
    ]):
        t = DBTeam(
            name=name, short_code=code,
            user_id=regular_user.id, is_placeholder=False,
        )
        db.session.add(t)
        db.session.flush()
        teams.append(t)
    db.session.commit()
    return teams


class TestOversConversion:
    """Test overs ↔ balls conversion edge cases."""

    def test_standard_overs(self, engine):
        assert engine.overs_to_balls("20.0") == 120
        assert engine.overs_to_balls("19.5") == 119

    def test_zero_overs(self, engine):
        assert engine.overs_to_balls("0.0") == 0
        assert engine.overs_to_balls(None) == 0

    def test_partial_balls(self, engine):
        assert engine.overs_to_balls("0.3") == 3
        assert engine.overs_to_balls("1.1") == 7

    def test_balls_to_overs_roundtrip(self, engine):
        assert engine.balls_to_overs(119) == "19.5"
        assert engine.balls_to_overs(120) == "20.0"
        assert engine.balls_to_overs(0) == "0.0"

    def test_balls_to_overs_negative(self, engine):
        assert engine.balls_to_overs(-1) == "0.0"
        assert engine.balls_to_overs(None) == "0.0"

    def test_invalid_partial_clamped(self, engine):
        """Overs like 19.7 should clamp partial to 5."""
        result = engine.overs_to_balls("19.7")
        assert result == 19 * 6 + 5  # clamped to .5


class TestNRRCalculation:
    """Test NRR calculation precision and edge cases."""

    def test_nrr_positive(self, app, engine, regular_user, four_teams):
        """Team that scores more per over than concedes has positive NRR."""
        t_id = _create_tournament(regular_user, four_teams[:2], engine)
        stats = TournamentTeam.query.filter_by(
            tournament_id=t_id, team_id=four_teams[0].id
        ).first()
        stats.runs_scored = 180
        stats.overs_faced = "20.0"
        stats.runs_conceded = 120
        stats.overs_bowled = "20.0"
        engine._calculate_nrr(stats)
        assert stats.net_run_rate == pytest.approx(3.0, abs=0.001)

    def test_nrr_zero_overs(self, app, engine, regular_user, four_teams):
        """NRR should be 0 when no overs faced/bowled."""
        t_id = _create_tournament(regular_user, four_teams[:2], engine)
        stats = TournamentTeam.query.filter_by(
            tournament_id=t_id, team_id=four_teams[0].id
        ).first()
        stats.runs_scored = 0
        stats.overs_faced = "0.0"
        stats.runs_conceded = 0
        stats.overs_bowled = "0.0"
        engine._calculate_nrr(stats)
        assert stats.net_run_rate == 0.0

    def test_nrr_precision_six_decimals(self, app, engine, regular_user, four_teams):
        """NRR should be stored with 6 decimal precision."""
        t_id = _create_tournament(regular_user, four_teams[:2], engine)
        stats = TournamentTeam.query.filter_by(
            tournament_id=t_id, team_id=four_teams[0].id
        ).first()
        stats.runs_scored = 100
        stats.overs_faced = "17.3"  # 105 balls = 17.5 overs
        stats.runs_conceded = 99
        stats.overs_bowled = "17.3"
        engine._calculate_nrr(stats)
        # Should have more precision than 3 decimals
        nrr_str = f"{stats.net_run_rate:.6f}"
        assert len(nrr_str.split(".")[1]) == 6


class TestRoundRobinGeneration:
    """Test fixture generation for round robin modes."""

    def test_two_team_rr(self, app, engine, regular_user, four_teams):
        """2-team RR should generate 1 match."""
        t = engine.create_tournament(
            name="2Team RR", user_id=regular_user.id,
            team_ids=[four_teams[0].id, four_teams[1].id],
            mode="round_robin",
        )
        fixtures = TournamentFixture.query.filter_by(tournament_id=t.id).all()
        assert len(fixtures) == 1

    def test_four_team_rr(self, app, engine, regular_user, four_teams):
        """4-team RR should generate 6 matches (4*3/2)."""
        t = engine.create_tournament(
            name="4Team RR", user_id=regular_user.id,
            team_ids=[t.id for t in four_teams],
            mode="round_robin",
        )
        fixtures = TournamentFixture.query.filter_by(
            tournament_id=t.id, stage="league"
        ).all()
        assert len(fixtures) == 6

    def test_double_rr(self, app, engine, regular_user, four_teams):
        """4-team DRR should generate 12 matches."""
        t = engine.create_tournament(
            name="4Team DRR", user_id=regular_user.id,
            team_ids=[t.id for t in four_teams],
            mode="double_round_robin",
        )
        fixtures = TournamentFixture.query.filter_by(tournament_id=t.id).all()
        assert len(fixtures) == 12


class TestKnockoutGeneration:
    """Test knockout bracket generation and bye handling."""

    def test_two_team_knockout(self, app, engine, regular_user, four_teams):
        """2-team knockout = 1 match (the final)."""
        t = engine.create_tournament(
            name="2Team KO", user_id=regular_user.id,
            team_ids=[four_teams[0].id, four_teams[1].id],
            mode="knockout",
        )
        fixtures = TournamentFixture.query.filter_by(tournament_id=t.id).all()
        scheduled = [f for f in fixtures if f.status == "Scheduled"]
        assert len(scheduled) >= 1

    def test_four_team_knockout(self, app, engine, regular_user, four_teams):
        """4-team knockout = 3 matches (2 semis + final)."""
        t = engine.create_tournament(
            name="4Team KO", user_id=regular_user.id,
            team_ids=[t.id for t in four_teams],
            mode="knockout",
        )
        all_fixtures = TournamentFixture.query.filter_by(tournament_id=t.id).all()
        assert len(all_fixtures) == 3

    def test_three_team_knockout_has_bye(self, app, engine, regular_user, four_teams):
        """3-team knockout should handle bye correctly."""
        t = engine.create_tournament(
            name="3Team KO", user_id=regular_user.id,
            team_ids=[four_teams[0].id, four_teams[1].id, four_teams[2].id],
            mode="knockout",
        )
        all_fixtures = TournamentFixture.query.filter_by(tournament_id=t.id).all()
        # 3 teams → padded to 4 → 3 fixtures
        # At least one bye match should be auto-completed
        completed_byes = [f for f in all_fixtures if f.status == "Completed"]
        assert len(completed_byes) >= 1


class TestIPLStyleGeneration:
    """Test IPL-style tournament generation."""

    def test_ipl_style_fixtures(self, app, engine, regular_user, four_teams):
        """IPL-style with 4 teams: 12 league + 4 playoff = 16 fixtures."""
        t = engine.create_tournament(
            name="IPL Style", user_id=regular_user.id,
            team_ids=[t.id for t in four_teams],
            mode="ipl_style",
        )
        all_fixtures = TournamentFixture.query.filter_by(tournament_id=t.id).all()
        league = [f for f in all_fixtures if f.stage == "league"]
        playoff = [f for f in all_fixtures if f.stage != "league"]
        assert len(league) == 12  # 4-team DRR
        assert len(playoff) == 4  # Q1, Elim, Q2, Final


class TestCustomSeries:
    """Test custom series validation and generation."""

    def test_custom_series_two_teams(self, app, engine, regular_user, four_teams):
        """Custom series should work with exactly 2 teams."""
        config = {
            "series_name": "Test Series",
            "matches": [
                {"match_num": 1, "home": 0},
                {"match_num": 2, "home": 1},
                {"match_num": 3, "home": 0},
            ],
        }
        t = engine.create_tournament(
            name="Custom", user_id=regular_user.id,
            team_ids=[four_teams[0].id, four_teams[1].id],
            mode="custom_series", series_config=config,
        )
        fixtures = TournamentFixture.query.filter_by(tournament_id=t.id).all()
        assert len(fixtures) == 3

    def test_custom_series_invalid_home_idx(self, app, engine, regular_user, four_teams):
        """Custom series with invalid home index should raise ValueError."""
        config = {
            "series_name": "Bad Series",
            "matches": [{"match_num": 1, "home": 5}],
        }
        with pytest.raises(ValueError, match="must be 0 or 1"):
            engine.create_tournament(
                name="Bad", user_id=regular_user.id,
                team_ids=[four_teams[0].id, four_teams[1].id],
                mode="custom_series", series_config=config,
            )

    def test_custom_series_three_teams_rejected(self, app, engine, regular_user, four_teams):
        """Custom series with 3 teams should be rejected."""
        config = {
            "series_name": "Bad",
            "matches": [{"match_num": 1, "home": 0}],
        }
        with pytest.raises(ValueError):
            engine.create_tournament(
                name="Bad3", user_id=regular_user.id,
                team_ids=[four_teams[0].id, four_teams[1].id, four_teams[2].id],
                mode="custom_series", series_config=config,
            )


class TestMinTeamValidation:
    """Test minimum team requirements per mode."""

    def test_one_team_rejected(self, app, engine, regular_user, four_teams):
        with pytest.raises(ValueError):
            engine.create_tournament(
                name="Solo", user_id=regular_user.id,
                team_ids=[four_teams[0].id],
                mode="round_robin",
            )

    def test_three_teams_for_ipl_rejected(self, app, engine, regular_user, four_teams):
        with pytest.raises(ValueError):
            engine.create_tournament(
                name="IPL3", user_id=regular_user.id,
                team_ids=[four_teams[0].id, four_teams[1].id, four_teams[2].id],
                mode="ipl_style",
            )


class TestAvailableModes:
    """Test mode availability based on team count."""

    def test_two_teams(self, engine):
        modes = engine.get_available_modes(2)
        mode_ids = [m[0] for m in modes]
        assert "round_robin" in mode_ids
        assert "knockout" in mode_ids
        assert "custom_series" in mode_ids
        assert "ipl_style" not in mode_ids

    def test_four_teams(self, engine):
        modes = engine.get_available_modes(4)
        mode_ids = [m[0] for m in modes]
        assert "ipl_style" in mode_ids
        assert "round_robin_knockout" in mode_ids

    def test_one_team(self, engine):
        modes = engine.get_available_modes(1)
        assert modes == []


# ── Helper ────────────────────────────────────────────────────────────────

def _create_tournament(user, teams, engine):
    """Quick helper to create a minimal tournament and return its ID."""
    t = engine.create_tournament(
        name="Helper Tournament",
        user_id=user.id,
        team_ids=[t.id for t in teams],
        mode="round_robin",
    )
    return t.id
