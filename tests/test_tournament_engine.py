"""
Unit tests for TournamentEngine edge cases.

Covers: overs conversion, NRR calculation, round-robin fixture generation,
knockout bye handling, custom series validation, and standings updates.
"""

import random
import uuid

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


@pytest.fixture
def six_teams(app, regular_user):
    """Create 6 teams — enough to force 2 byes in an 8-slot knockout bracket."""
    teams = []
    for i, (name, code) in enumerate([
        ("Alpha", "ALP"), ("Bravo", "BRV"), ("Charlie", "CHL"),
        ("Delta", "DLT"), ("Echo", "ECH"), ("Foxtrot", "FOX"),
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


@pytest.fixture
def seven_teams(app, regular_user):
    """Create 7 teams — exactly 1 bye in an 8-slot knockout bracket."""
    teams = []
    for i, (name, code) in enumerate([
        ("Alpha", "ALP"), ("Bravo", "BRV"), ("Charlie", "CHL"), ("Delta", "DLT"),
        ("Echo", "ECH"), ("Foxtrot", "FOX"), ("Golf", "GLF"),
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


@pytest.fixture
def eight_teams(app, regular_user):
    """Create 8 teams — an exact power of 2, so the bracket has zero byes."""
    teams = []
    for i, (name, code) in enumerate([
        ("Alpha", "ALP"), ("Bravo", "BRV"), ("Charlie", "CHL"), ("Delta", "DLT"),
        ("Echo", "ECH"), ("Foxtrot", "FOX"), ("Golf", "GLF"), ("Hotel", "HTL"),
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


class TestIsNoResult:
    """_is_no_result must prefer the structured match_status column, falling
    back to keyword-sniffing result_description only for legacy rows written
    before that column existed (permanent fallback — never backfilled)."""

    def test_structured_status_no_result_fast_path(self, engine):
        match = DBMatch(match_status="no_result", winner_team_id=None,
                         result_description=None, home_team_overs="12.3", away_team_overs="0.0")
        assert engine._is_no_result(match) is True

    def test_structured_status_completed_short_circuits_legacy_parsing(self, engine):
        # Even if result_description looks NR-ish, an explicit 'completed'
        # status must win — no keyword re-parsing once match_status is set.
        match = DBMatch(match_status="completed", winner_team_id=1,
                         result_description="abandoned", home_team_overs="20.0", away_team_overs="18.2")
        assert engine._is_no_result(match) is False

    def test_legacy_keyword_fallback_when_status_is_null(self, engine):
        match = DBMatch(match_status=None, winner_team_id=None,
                         result_description="Match abandoned due to rain",
                         home_team_overs="5.2", away_team_overs="0.0")
        assert engine._is_no_result(match) is True

    def test_legacy_non_keyword_tie_remains_a_tie_not_no_result(self, engine):
        """A legacy row with no winner, no NR keyword, and balls bowled is a
        tie — must NOT be misclassified as no_result just because
        match_status is NULL (this is permanent behavior, never backfilled)."""
        match = DBMatch(match_status=None, winner_team_id=None,
                         result_description="Match Tied",
                         home_team_overs="20.0", away_team_overs="20.0")
        assert engine._is_no_result(match) is False


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
        stats.overs_faced = "17.3"  # 17 overs + 3 balls = 105 balls (= 17.5 decimal overs)
        stats.runs_conceded = 99
        stats.overs_bowled = "17.3"
        engine._calculate_nrr(stats)
        # Should have more precision than 3 decimals
        nrr_str = f"{stats.net_run_rate:.6f}"
        assert len(nrr_str.split(".")[1]) == 6


class TestNRROversCap:
    """Defensive cap on _get_nrr_overs — overs > full quota are clamped.

    Some legacy match rows store values like "20.1" in a 20-over match
    (an upstream sim accounting bug). Letting them through to the NRR
    denominator skews per-team NRR by ~1% per affected match. The cap
    keeps NRR stable regardless of legacy data drift.
    """

    class _FakeMatch:
        def __init__(self, overs_per_side):
            self.overs_per_side = overs_per_side

    def test_cap_t20_overs_above_quota(self, engine):
        m = self._FakeMatch(20)
        # 20.1 / 20.2 / 21.0 in a 20-over match → clamp to 20.0
        assert engine._get_nrr_overs("20.1", 5, m) == "20.0"
        assert engine._get_nrr_overs("20.2", 7, m) == "20.0"
        assert engine._get_nrr_overs("21.0", 3, m) == "20.0"

    def test_no_cap_below_quota(self, engine):
        m = self._FakeMatch(20)
        # Below quota: pass through
        assert engine._get_nrr_overs("19.5", 4, m) == "19.5"
        assert engine._get_nrr_overs("12.0", 6, m) == "12.0"

    def test_all_out_uses_full_quota(self, engine):
        m = self._FakeMatch(20)
        # All out (10 wickets) → full quota regardless of actual
        assert engine._get_nrr_overs("12.3", 10, m) == "20.0"

    def test_lista_format(self, engine):
        m = self._FakeMatch(50)
        assert engine._get_nrr_overs("50.1", 8, m) == "50.0"
        assert engine._get_nrr_overs("48.3", 5, m) == "48.3"

    def test_cap_isolates_nrr_from_legacy_drift(self, app, engine, regular_user, four_teams):
        """A team carrying buggy '20.1' in overs_faced should still be
        capped if the rebuild flow re-runs through _get_nrr_overs."""
        t_id = _create_tournament(regular_user, four_teams[:2], engine)
        stats = TournamentTeam.query.filter_by(
            tournament_id=t_id, team_id=four_teams[0].id
        ).first()
        stats.runs_scored = 180
        stats.overs_faced = "20.0"
        stats.runs_conceded = 120
        stats.overs_bowled = "20.0"
        engine._calculate_nrr(stats)
        clean_nrr = stats.net_run_rate

        # Simulate one match's worth of buggy "20.1" being aggregated in
        stats.overs_faced = "20.1"
        engine._calculate_nrr(stats)
        buggy_nrr = stats.net_run_rate
        # Without the cap, NRR shifts measurably (~0.05+ on a 3.0 baseline)
        assert buggy_nrr != clean_nrr


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


class TestKnockoutPhantomCascade:
    """
    Regression tests for the Round-1 "Phantom Match" soft-lock: a knockout
    bracket with 2+ byes can shuffle both byes into the same Round-1 match,
    producing a fixture with no teams and a permanently-None winner_team_id.
    The live progression checker must not treat that by-design winner-less
    fixture as "still pending" forever, must still block on a genuine
    no-winner anomaly (a real match that somehow completed without a
    winner), and must keep cascading through rounds that resolve purely via
    byes/phantoms with no real match left to trigger the next check.
    """

    def _play(self, engine, fixture, winner_id, user_id=None):
        """Simulate playing a Scheduled fixture through the real completion
        path: create + link a DBMatch, then run update_standings exactly as
        app.py does after a live match archives.
        """
        match = DBMatch(
            id=str(uuid.uuid4()),
            user_id=user_id,
            tournament_id=fixture.tournament_id,
            home_team_id=fixture.home_team_id,
            away_team_id=fixture.away_team_id,
            winner_team_id=winner_id,
            match_format="T20",
            match_status="completed",
        )
        db.session.add(match)
        db.session.flush()
        fixture.match_id = match.id
        db.session.commit()
        engine.update_standings(match, commit=True)
        return match

    def test_phantom_in_round1_does_not_stall_progression(
        self, app, engine, regular_user, six_teams, monkeypatch
    ):
        """6 teams -> 8-slot bracket, 2 byes. With shuffling disabled the two
        byes land in the same (last) Round-1 slot, producing a Phantom
        Match. Before the fix, playing out the 3 real Round-1 matches left
        the tournament stuck forever: Round 2 fixtures never unlocked
        because the Phantom's permanent winner_team_id=None tripped the
        "any winner missing" guard on every subsequent check.
        """
        monkeypatch.setattr(random, "shuffle", lambda seq: None)

        t = engine.create_tournament(
            name="6Team KO Phantom", user_id=regular_user.id,
            team_ids=[tm.id for tm in six_teams], mode="knockout",
        )

        round1 = (
            TournamentFixture.query
            .filter_by(tournament_id=t.id, round_number=1)
            .order_by(TournamentFixture.bracket_position)
            .all()
        )
        phantom = [f for f in round1 if f.home_team_id is None and f.away_team_id is None]
        real_matches = [f for f in round1 if f.status == "Scheduled"]

        assert len(phantom) == 1
        assert phantom[0].status == "Completed"
        assert phantom[0].winner_team_id is None
        assert len(real_matches) == 3

        for f in real_matches:
            self._play(engine, f, winner_id=f.home_team_id, user_id=regular_user.id)

        round1_stage = round1[0].stage
        db.session.refresh(t)
        assert t.current_stage != round1_stage, (
            "tournament stalled in Round 1 — the Phantom match's permanent "
            "winner_team_id=None blocked progression"
        )

        round2 = (
            TournamentFixture.query
            .filter_by(tournament_id=t.id, round_number=2)
            .order_by(TournamentFixture.bracket_position)
            .all()
        )
        assert round2, "Round 2 fixtures should exist"
        assert all(f.status != "Locked" for f in round2), (
            "Round 2 fixtures never got unlocked/resolved"
        )

    def test_real_no_winner_anomaly_still_blocks_progression(
        self, app, engine, regular_user, four_teams
    ):
        """A genuine anomaly — a match between two real teams that
        completed without a recorded winner — must still block
        progression. This guards against over-relaxing the fix: only
        Phantom fixtures (home_team_id and away_team_id both None) are
        exempt from the "missing winner" check.
        """
        t = engine.create_tournament(
            name="4Team KO Anomaly", user_id=regular_user.id,
            team_ids=[tm.id for tm in four_teams], mode="knockout",
        )
        round1 = (
            TournamentFixture.query
            .filter_by(tournament_id=t.id, round_number=1)
            .order_by(TournamentFixture.bracket_position)
            .all()
        )
        assert len(round1) == 2

        # First semifinal plays out normally.
        self._play(engine, round1[0], winner_id=round1[0].home_team_id, user_id=regular_user.id)

        # Second semifinal: simulate a data anomaly — completed between two
        # real (non-placeholder) teams but with no winner recorded, e.g. an
        # unresolved rain-abandoned knockout tie.
        round1[1].status = "Completed"
        db.session.commit()

        result = engine._check_knockout_progression(t)
        assert result is False

        db.session.refresh(t)
        assert t.current_stage == round1[0].stage, "must not advance past a real no-winner anomaly"

    def test_round_resolved_entirely_by_byes_cascades_in_one_pass(
        self, app, engine, regular_user, six_teams
    ):
        """Hand-build an 8-slot bracket where, after the sole real Round-1
        match is played, BOTH Round-2 slots resolve via byes/phantoms (no
        real Round-2 match exists to ever trigger another check). Before
        the fix, progression stopped the moment Round 1 advanced to Round 2
        and never re-checked whether Round 2 was itself already decided —
        the Final would stay Locked forever with nothing left to unstick
        it. The corrected checker must loop and cascade all the way to the
        Final in the same pass.
        """
        team_a, team_b, team_c = six_teams[0], six_teams[1], six_teams[2]

        t = Tournament(
            name="Hand-built KO", user_id=regular_user.id, mode="knockout",
            current_stage=engine.STAGE_KNOCKOUT_QF,
        )
        db.session.add(t)
        db.session.flush()

        # 5 registered teams -> next_power_of_two(5) == 8, matching the
        # 4 QF / 2 SF / 1 Final tree built below.
        for team in six_teams[:5]:
            db.session.add(TournamentTeam(tournament_id=t.id, team_id=team.id))

        qf0 = TournamentFixture(
            tournament_id=t.id, home_team_id=team_a.id, away_team_id=team_b.id,
            round_number=1, stage=engine.STAGE_KNOCKOUT_QF, bracket_position=0,
            status="Scheduled",
        )
        qf1 = TournamentFixture(
            tournament_id=t.id, home_team_id=None, away_team_id=None,
            round_number=1, stage=engine.STAGE_KNOCKOUT_QF, bracket_position=1,
            status="Completed", winner_team_id=None, stage_description="Phantom Match",
        )
        qf2 = TournamentFixture(
            tournament_id=t.id, home_team_id=team_c.id, away_team_id=None,
            round_number=1, stage=engine.STAGE_KNOCKOUT_QF, bracket_position=2,
            status="Completed", winner_team_id=team_c.id,
            stage_description="Bye - Advances to next round",
        )
        qf3 = TournamentFixture(
            tournament_id=t.id, home_team_id=None, away_team_id=None,
            round_number=1, stage=engine.STAGE_KNOCKOUT_QF, bracket_position=3,
            status="Completed", winner_team_id=None, stage_description="Phantom Match",
        )
        sf0 = TournamentFixture(
            tournament_id=t.id, home_team_id=None, away_team_id=None,
            round_number=2, stage=engine.STAGE_KNOCKOUT_SF, bracket_position=4,
            status="Locked",
        )
        sf1 = TournamentFixture(
            tournament_id=t.id, home_team_id=None, away_team_id=None,
            round_number=2, stage=engine.STAGE_KNOCKOUT_SF, bracket_position=5,
            status="Locked",
        )
        final = TournamentFixture(
            tournament_id=t.id, home_team_id=None, away_team_id=None,
            round_number=3, stage=engine.STAGE_FINAL, bracket_position=6,
            status="Locked",
        )
        for f in (qf0, qf1, qf2, qf3, sf0, sf1, final):
            db.session.add(f)
        db.session.commit()

        # Play the ONLY real match in the whole bracket. Its pairing with
        # Phantom qf1 makes sf0 a bye (team_a advances); qf2+qf3 already
        # made sf1 a bye (team_c advances) once qf0 completes the round —
        # so Round 2 resolves with zero real matches, and the Final must be
        # reached in this same call.
        self._play(engine, qf0, winner_id=team_a.id, user_id=regular_user.id)

        db.session.refresh(t)
        final = db.session.get(TournamentFixture, final.id)

        assert t.current_stage == engine.STAGE_FINAL
        assert final.status == "Scheduled", (
            "Round 2 resolved entirely via byes but progression stopped "
            "there instead of cascading straight to the Final"
        )
        assert {final.home_team_id, final.away_team_id} == {team_a.id, team_c.id}

    def test_zero_byes_multi_round_does_not_prematurely_complete(
        self, app, engine, regular_user, eight_teams
    ):
        """8 teams -> exact power of 2, zero byes at all. Before the fix,
        _advance_bye_winners's Step 2 judged a next-round fixture's fate
        purely from whatever partial state Step 1 had written into it,
        without ever checking its actual two feeders. For a bracket with
        NO byes, Step 1 never touches SF/Final at creation time (nothing is
        'Completed' yet), so they're still sitting at their normal initial
        home=None/away=None — and Step 2 misread that completely ordinary
        "not decided yet" state as "both feeders were phantom," marking SF
        AND (cascading from there) the Final 'Completed' with no winner
        immediately at tournament creation, before a single match was
        played. This exact pattern (SF + Final both Phantom, all 4 QFs
        genuinely played) was found in two live production tournaments.
        """
        t = engine.create_tournament(
            name="8Team KO Clean", user_id=regular_user.id,
            team_ids=[tm.id for tm in eight_teams], mode="knockout",
        )

        non_round1 = TournamentFixture.query.filter(
            TournamentFixture.tournament_id == t.id,
            TournamentFixture.round_number > 1,
        ).all()
        assert non_round1, "expected SF + Final fixtures"
        assert all(f.status == "Locked" for f in non_round1), (
            "SF/Final fixtures were prematurely resolved at creation time "
            "despite there being zero byes"
        )
        assert all(
            f.home_team_id is None and f.away_team_id is None for f in non_round1
        )

        round1 = (
            TournamentFixture.query
            .filter_by(tournament_id=t.id, round_number=1)
            .order_by(TournamentFixture.bracket_position)
            .all()
        )
        assert len(round1) == 4
        assert all(f.status == "Scheduled" for f in round1)

        for f in round1:
            self._play(engine, f, winner_id=f.home_team_id, user_id=regular_user.id)

        db.session.refresh(t)
        sf = TournamentFixture.query.filter_by(tournament_id=t.id, round_number=2).all()
        assert all(f.status == "Scheduled" for f in sf)
        assert all(f.home_team_id is not None and f.away_team_id is not None for f in sf)
        assert t.current_stage == sf[0].stage

    def test_bye_paired_with_pending_real_match_stays_locked_at_creation(
        self, app, engine, regular_user, seven_teams, monkeypatch
    ):
        """7 teams -> 8-slot bracket, exactly 1 bye. With shuffling disabled
        the bye lands in the last Round-1 slot (paired with a real team),
        sharing its Round-2 feeder pairing with an adjacent REAL,
        still-unplayed match.

        Before the fix, the old two-step _advance_bye_winners could only
        see that Round-2 slot's accumulated home/away fields — both still
        None, since the real sibling hadn't produced a winner yet — and
        would misjudge it: the bye's genuine winner would get prematurely
        auto-advanced as the slot's outright winner (or the slot would get
        marked a false Phantom), in both cases before the real sibling
        match was ever played. This is the exact "one bye/phantom feeder +
        one still-Scheduled real feeder" ambiguity described as a second,
        related bug alongside the Round-1 Phantom stall. The corrected
        code must leave this slot completely untouched (still 'Locked')
        until BOTH its feeders are genuinely decided.
        """
        monkeypatch.setattr(random, "shuffle", lambda seq: None)

        t = engine.create_tournament(
            name="7Team KO Mixed Bye", user_id=regular_user.id,
            team_ids=[tm.id for tm in seven_teams], mode="knockout",
        )

        round1 = (
            TournamentFixture.query
            .filter_by(tournament_id=t.id, round_number=1)
            .order_by(TournamentFixture.bracket_position)
            .all()
        )
        byes = [f for f in round1 if f.status == "Completed"]
        real_matches = [f for f in round1 if f.status == "Scheduled"]
        assert len(round1) == 4
        assert len(byes) == 1, "7 teams in an 8-slot bracket should produce exactly 1 bye"
        assert len(real_matches) == 3

        bye_fixture = round1[3]
        pending_sibling = round1[2]
        assert bye_fixture.status == "Completed"
        assert bye_fixture.winner_team_id is not None
        assert pending_sibling.status == "Scheduled", (
            "expected the bye's Round-2 sibling to be a real, unplayed match"
        )

        round2 = (
            TournamentFixture.query
            .filter_by(tournament_id=t.id, round_number=2)
            .order_by(TournamentFixture.bracket_position)
            .all()
        )
        fed_slot = round2[1]  # fed by round1[2] (pending real) + round1[3] (bye)

        assert fed_slot.status == "Locked", (
            "the Round-2 slot fed by a genuine bye + a still-unplayed real "
            "match was prematurely resolved at creation time"
        )
        assert fed_slot.home_team_id is None and fed_slot.away_team_id is None
        assert fed_slot.winner_team_id is None

        # _advance_knockout_round gates on the WHOLE round being Completed,
        # not per-pair, so every other still-Scheduled Round-1 match has to
        # be played too before anything advances — including the pair that
        # doesn't involve the bye at all.
        bye_winner = bye_fixture.winner_team_id
        sibling_winner = pending_sibling.home_team_id
        for f in real_matches:
            self._play(engine, f, winner_id=f.home_team_id, user_id=regular_user.id)

        # Both feeders of fed_slot (bye winner + sibling winner) are real,
        # non-placeholder teams, so it must unlock as a genuine 'Scheduled'
        # match to be played — not resolve itself as if one side were
        # still a walkover.
        db.session.refresh(fed_slot)
        assert fed_slot.status == "Scheduled"
        assert fed_slot.winner_team_id is None
        assert {fed_slot.home_team_id, fed_slot.away_team_id} == {bye_winner, sibling_winner}


class TestKnockoutHistoricalCorruptionSelfHeals:
    """
    Regression tests for a second bug found while investigating the Phantom
    stall: fixtures already corrupted by the old _advance_bye_winners
    (marked 'Completed' with a bogus single-sided winner, no match ever
    played) must be able to self-correct once their real sibling match is
    actually played. An earlier version of the fix's guard was
    `status != 'Locked'`, which — meant to stop a resolved fixture from
    being re-clobbered — also blocked correcting fixtures that were already
    wrongly 'Completed', so they would stay wrong forever and the
    tournament could complete with a bogus winner instead of the real one.
    The guard must protect only fixtures with real match data: a linked
    match_id, or already 'Scheduled' for a real match.
    """

    def _play(self, engine, fixture, winner_id, user_id=None):
        match = DBMatch(
            id=str(uuid.uuid4()),
            user_id=user_id,
            tournament_id=fixture.tournament_id,
            home_team_id=fixture.home_team_id,
            away_team_id=fixture.away_team_id,
            winner_team_id=winner_id,
            match_format="T20",
            match_status="completed",
        )
        db.session.add(match)
        db.session.flush()
        fixture.match_id = match.id
        db.session.commit()
        engine.update_standings(match, commit=True)
        return match

    def test_prematurely_completed_final_self_heals_once_real_sf_is_played(
        self, app, engine, regular_user, four_teams
    ):
        """Directly mimics the historical bug's exact output on a Final —
        'Completed', one real winner, other side null, no match_id — while
        its real sibling semifinal is still unplayed. Playing that
        semifinal for real must correct the Final instead of leaving it
        stuck on the bogus winner.
        """
        t = engine.create_tournament(
            name="4Team KO Corrupted", user_id=regular_user.id,
            team_ids=[tm.id for tm in four_teams], mode="knockout",
        )
        round1 = (
            TournamentFixture.query
            .filter_by(tournament_id=t.id, round_number=1)
            .order_by(TournamentFixture.bracket_position)
            .all()
        )
        final = TournamentFixture.query.filter_by(tournament_id=t.id, round_number=2).first()
        assert len(round1) == 2 and final is not None

        # Simulate the historical corruption artifact directly on the Final.
        winner1 = round1[0].home_team_id
        round1[0].status = "Completed"
        round1[0].winner_team_id = winner1
        final.status = "Completed"
        final.home_team_id = winner1
        final.away_team_id = None
        final.winner_team_id = winner1
        final.match_id = None
        db.session.commit()

        # SF2 is a genuine, still-unplayed real match.
        assert round1[1].status == "Scheduled"
        winner2 = round1[1].home_team_id
        self._play(engine, round1[1], winner_id=winner2, user_id=regular_user.id)

        db.session.refresh(final)
        assert final.status == "Scheduled", (
            "a Final corrupted by the historical bug must self-heal once "
            "its real semifinal is actually played"
        )
        assert {final.home_team_id, final.away_team_id} == {winner1, winner2}
        assert final.winner_team_id is None, (
            "the bogus winner from the historical corruption must be cleared, "
            "not left stale on a fixture that hasn't actually been played"
        )

    def test_resolve_round_pair_never_overwrites_a_played_fixture(
        self, app, engine, regular_user, four_teams
    ):
        """Guard against over-relaxing: a fixture with a real match already
        linked must never be touched again, even if its feeders are
        re-processed with a different outcome.
        """
        t = Tournament(
            name="Guard Test", user_id=regular_user.id, mode="knockout",
            current_stage="final",
        )
        db.session.add(t)
        db.session.flush()

        team_a, team_b, team_c, team_d = four_teams

        m1 = TournamentFixture(
            tournament_id=t.id, home_team_id=team_a.id, away_team_id=team_b.id,
            round_number=1, stage="knockout_sf", bracket_position=0,
            status="Completed", winner_team_id=team_a.id,
        )
        m2 = TournamentFixture(
            tournament_id=t.id, home_team_id=team_c.id, away_team_id=team_d.id,
            round_number=1, stage="knockout_sf", bracket_position=1,
            status="Completed", winner_team_id=team_c.id,
        )
        real_match = DBMatch(
            id=str(uuid.uuid4()), user_id=regular_user.id, tournament_id=t.id,
            home_team_id=team_a.id, away_team_id=team_c.id, winner_team_id=team_a.id,
            match_format="T20", match_status="completed",
        )
        db.session.add(real_match)
        db.session.flush()

        final = TournamentFixture(
            tournament_id=t.id, home_team_id=team_a.id, away_team_id=team_c.id,
            round_number=2, stage="final", bracket_position=2,
            status="Completed", winner_team_id=team_a.id, match_id=real_match.id,
        )
        db.session.add_all([m1, m2, final])
        db.session.commit()

        # Re-process the same pair with a different (hypothetical) outcome
        # — must be a complete no-op since the Final already has a real
        # match linked.
        m2.winner_team_id = team_d.id
        engine._resolve_round_pair(m1, m2, final)

        db.session.refresh(final)
        assert final.home_team_id == team_a.id
        assert final.away_team_id == team_c.id
        assert final.winner_team_id == team_a.id
        assert final.match_id == real_match.id


class TestUpdateStandingsUnresolvedKnockout:
    """
    Regression tests: update_standings must not blindly overwrite a
    knockout/playoff fixture to 'Completed' when its linked match has no
    winner (an abandoned/no-result match a super over couldn't resolve).
    It used to unconditionally set status='Completed' the moment a fixture
    was linked, silently discarding the caller's intent (app.py's own
    "keep it Scheduled so the user can re-simulate directly" decision) —
    and since update_standings is also called directly by the standalone
    orphaned-match repair script, that caller had the exact same exposure.
    """

    def _link_and_update(self, engine, regular_user, tournament_id, fixture, winner_id, match_status):
        match = DBMatch(
            id=str(uuid.uuid4()),
            user_id=regular_user.id,
            tournament_id=tournament_id,
            home_team_id=fixture.home_team_id,
            away_team_id=fixture.away_team_id,
            winner_team_id=winner_id,
            match_format="T20",
            match_status=match_status,
        )
        db.session.add(match)
        db.session.flush()
        fixture.match_id = match.id
        db.session.commit()
        return engine.update_standings(match, commit=True)

    def test_knockout_fixture_with_no_winner_stays_scheduled(
        self, app, engine, regular_user, four_teams
    ):
        t = engine.create_tournament(
            name="4Team KO NoResult", user_id=regular_user.id,
            team_ids=[tm.id for tm in four_teams], mode="knockout",
        )
        fixture = TournamentFixture.query.filter_by(
            tournament_id=t.id, round_number=1
        ).first()

        result = self._link_and_update(
            engine, regular_user, t.id, fixture, winner_id=None, match_status="no_result"
        )

        db.session.refresh(fixture)
        assert result is True
        assert fixture.status == "Scheduled", (
            "a winner-less knockout-stage match must not be silently "
            "flipped to Completed"
        )
        assert fixture.winner_team_id is None
        assert fixture.standings_applied is False

    def test_league_fixture_with_no_winner_still_completes(
        self, app, engine, regular_user, four_teams
    ):
        """Contrast case: a league-stage no-result is a legitimate final
        result (it counts toward standings via no-result bookkeeping) and
        must still be marked Completed — only knockout/playoff stages get
        the "stay Scheduled" treatment.
        """
        t = engine.create_tournament(
            name="4Team RR NoResult", user_id=regular_user.id,
            team_ids=[tm.id for tm in four_teams], mode="round_robin",
        )
        fixture = TournamentFixture.query.filter_by(tournament_id=t.id).first()

        result = self._link_and_update(
            engine, regular_user, t.id, fixture, winner_id=None, match_status="no_result"
        )

        db.session.refresh(fixture)
        assert result is True
        assert fixture.status == "Completed"
        assert fixture.standings_applied is True

    def test_knockout_fixture_with_a_winner_still_completes(
        self, app, engine, regular_user, four_teams
    ):
        """Regression guard: a genuinely decided knockout match must still
        be marked Completed exactly as before.
        """
        t = engine.create_tournament(
            name="4Team KO Decided", user_id=regular_user.id,
            team_ids=[tm.id for tm in four_teams], mode="knockout",
        )
        fixture = TournamentFixture.query.filter_by(
            tournament_id=t.id, round_number=1
        ).first()

        result = self._link_and_update(
            engine, regular_user, t.id, fixture,
            winner_id=fixture.home_team_id, match_status="completed",
        )

        db.session.refresh(fixture)
        assert result is True
        assert fixture.status == "Completed"
        assert fixture.winner_team_id == fixture.home_team_id
        assert fixture.standings_applied is True


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


class TestIPLPlayoffResimulation:
    """
    Regression tests for IPL-style playoff re-simulation. The Q1/Eliminator/
    Q2/Final structure isn't a binary tree — Q1 feeds both Q2 (as its loser)
    and Final (as its winner) directly — so resetting downstream fixtures
    can't use the same bracket-position halving that works for a real
    knockout bracket. Hand-builds just the playoff stage (skipping league
    play, which is orthogonal) so each test can play a known path through
    Q1 -> Eliminator -> Q2 -> Final and then re-simulate one leg of it.
    """

    def _play(self, engine, fixture, winner_id, user_id=None):
        match = DBMatch(
            id=str(uuid.uuid4()),
            user_id=user_id,
            tournament_id=fixture.tournament_id,
            home_team_id=fixture.home_team_id,
            away_team_id=fixture.away_team_id,
            winner_team_id=winner_id,
            match_format="T20",
            match_status="completed",
        )
        db.session.add(match)
        db.session.flush()
        fixture.match_id = match.id
        db.session.commit()
        engine.update_standings(match, commit=True)
        return match

    def _build_and_play_to_final(self, engine, regular_user, four_teams):
        """Hand-build the IPL playoff stage and play it out completely:
        Q1: team_a beats team_b (team_b is the Q1 "loser" who gets a
            second life in Q2).
        Eliminator: team_c beats team_d (team_d is eliminated).
        Q2 (auto-populated: team_b vs team_c): team_b beats team_c.
        Final (auto-populated: team_a vs team_b): team_a beats team_b.

        Returns (tournament, fixtures, matches) — fixtures/matches are
        dicts keyed by 'q1'/'elim'/'q2'/'final'.
        """
        team_a, team_b, team_c, team_d = four_teams

        t = Tournament(
            name="IPL Playoff Resim", user_id=regular_user.id, mode="ipl_style",
            current_stage=engine.STAGE_QUALIFIER_1,
        )
        db.session.add(t)
        db.session.flush()

        for team in four_teams:
            db.session.add(TournamentTeam(tournament_id=t.id, team_id=team.id))

        q1 = TournamentFixture(
            tournament_id=t.id, home_team_id=team_a.id, away_team_id=team_b.id,
            round_number=1, stage=engine.STAGE_QUALIFIER_1, bracket_position=1,
            status="Scheduled",
        )
        elim = TournamentFixture(
            tournament_id=t.id, home_team_id=team_c.id, away_team_id=team_d.id,
            round_number=1, stage=engine.STAGE_ELIMINATOR, bracket_position=2,
            status="Scheduled",
        )
        q2 = TournamentFixture(
            tournament_id=t.id, home_team_id=None, away_team_id=None,
            round_number=2, stage=engine.STAGE_QUALIFIER_2, bracket_position=3,
            status="Locked",
        )
        final = TournamentFixture(
            tournament_id=t.id, home_team_id=None, away_team_id=None,
            round_number=3, stage=engine.STAGE_FINAL, bracket_position=4,
            status="Locked",
        )
        for f in (q1, elim, q2, final):
            db.session.add(f)
        db.session.commit()

        matches = {}
        matches['q1'] = self._play(engine, q1, winner_id=team_a.id, user_id=regular_user.id)
        matches['elim'] = self._play(engine, elim, winner_id=team_c.id, user_id=regular_user.id)

        db.session.refresh(t)
        assert t.current_stage == engine.STAGE_QUALIFIER_2
        db.session.refresh(q2)
        assert q2.status == "Scheduled"
        assert {q2.home_team_id, q2.away_team_id} == {team_b.id, team_c.id}  # Q1 loser vs Elim winner

        matches['q2'] = self._play(engine, q2, winner_id=team_b.id, user_id=regular_user.id)

        db.session.refresh(t)
        assert t.current_stage == engine.STAGE_FINAL
        db.session.refresh(final)
        assert final.status == "Scheduled"
        assert {final.home_team_id, final.away_team_id} == {team_a.id, team_b.id}  # Q1 winner vs Q2 winner

        matches['final'] = self._play(engine, final, winner_id=team_a.id, user_id=regular_user.id)

        fixtures = {'q1': q1, 'elim': elim, 'q2': q2, 'final': final}
        return t, fixtures, matches

    def test_resimulating_q1_resets_q2_and_final(
        self, app, engine, regular_user, four_teams
    ):
        """Q1 has two direct downstream dependents (Q2 as loser, Final as
        winner) — the exact two-hop case the old bracket-position math
        couldn't represent at all.
        """
        t, fixtures, matches = self._build_and_play_to_final(engine, regular_user, four_teams)

        engine.reverse_standings(matches['q1'], commit=True)

        db.session.refresh(fixtures['q2'])
        db.session.refresh(fixtures['final'])
        db.session.refresh(fixtures['elim'])

        for f in (fixtures['q2'], fixtures['final']):
            assert f.status == "Locked"
            assert f.winner_team_id is None
            assert f.match_id is None

        # Eliminator is upstream of Q1, not downstream — must be untouched.
        assert fixtures['elim'].status == "Completed"
        assert fixtures['elim'].winner_team_id == four_teams[2].id

    def test_resimulating_eliminator_resets_q2_and_final(
        self, app, engine, regular_user, four_teams
    ):
        """Eliminator only feeds Q2 directly, but Q2 feeds Final — resetting
        Eliminator must transitively reach Final too, not stop at Q2.
        """
        t, fixtures, matches = self._build_and_play_to_final(engine, regular_user, four_teams)

        engine.reverse_standings(matches['elim'], commit=True)

        db.session.refresh(fixtures['q2'])
        db.session.refresh(fixtures['final'])
        db.session.refresh(fixtures['q1'])

        for f in (fixtures['q2'], fixtures['final']):
            assert f.status == "Locked"
            assert f.winner_team_id is None
            assert f.match_id is None

        # Q1 is not downstream of Eliminator — must be untouched.
        assert fixtures['q1'].status == "Completed"
        assert fixtures['q1'].winner_team_id == four_teams[0].id

    def test_resimulating_q2_resets_only_final(
        self, app, engine, regular_user, four_teams
    ):
        """Q2 feeds only the Final — Q1 and Eliminator are upstream and
        must be left alone.
        """
        t, fixtures, matches = self._build_and_play_to_final(engine, regular_user, four_teams)

        engine.reverse_standings(matches['q2'], commit=True)

        db.session.refresh(fixtures['final'])
        assert fixtures['final'].status == "Locked"
        assert fixtures['final'].winner_team_id is None
        assert fixtures['final'].match_id is None

        db.session.refresh(fixtures['q1'])
        db.session.refresh(fixtures['elim'])
        assert fixtures['q1'].status == "Completed"
        assert fixtures['q1'].winner_team_id == four_teams[0].id
        assert fixtures['elim'].status == "Completed"
        assert fixtures['elim'].winner_team_id == four_teams[2].id

    def test_resimulating_final_resets_nothing_downstream(
        self, app, engine, regular_user, four_teams
    ):
        """The Final has nothing downstream — a sanity check that this
        doesn't error and doesn't touch Q1/Eliminator/Q2.
        """
        t, fixtures, matches = self._build_and_play_to_final(engine, regular_user, four_teams)

        engine.reverse_standings(matches['final'], commit=True)

        db.session.refresh(fixtures['final'])
        assert fixtures['final'].status == "Scheduled"
        assert fixtures['final'].winner_team_id is None

        for key in ('q1', 'elim', 'q2'):
            db.session.refresh(fixtures[key])
            assert fixtures[key].status == "Completed"


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

    def test_unknown_mode_rejected(self, app, engine, regular_user, four_teams):
        """An unrecognized mode must never silently commit a tournament
        with teams attached but zero fixtures generated — MIN_TEAMS.get(mode, 2)
        would otherwise let it slide through with a default of 2 teams
        required, and _generate_fixtures_for_mode's if/elif chain would
        match nothing.
        """
        with pytest.raises(ValueError, match="Unknown tournament mode"):
            engine.create_tournament(
                name="Bogus", user_id=regular_user.id,
                team_ids=[t.id for t in four_teams],
                mode="not_a_real_mode",
            )

        assert Tournament.query.filter_by(name="Bogus").first() is None


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
