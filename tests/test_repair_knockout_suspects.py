"""
Regression tests for the fabricated-pairing ("suspect") arm of
migrations/repair_knockout_bracket_corruption.py.

The historical premature bye/phantom resolution bug had a third
manifestation beyond the two the repair script originally handled: a
next-round fixture left 'Scheduled' with both teams populated from bogus
feeder "winners". Status alone can't tell that apart from a real queued
match, and `_resolve_round_pair` refuses to touch a 'Scheduled' fixture —
so left alone, the fabricated pairing outlives the repair and the real
winners never reach their own next round.
"""

import json
import os
import uuid

import pytest
from app import db
from database.models import Tournament, TournamentTeam, TournamentFixture, Team as DBTeam
from engine.tournament_engine import TournamentEngine
from migrations.repair_knockout_bracket_corruption import run as repair_run
from tests.conftest import login_user

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MATCH_DIR = os.path.join(PROJECT_ROOT, "data", "matches")


@pytest.fixture
def engine():
    return TournamentEngine()


@pytest.fixture
def five_teams(app, regular_user):
    """5 teams -> next_power_of_two(5) == 8: a 4 QF / 2 SF / 1 Final tree."""
    teams = []
    for name, code in [("Alpha", "ALP"), ("Bravo", "BRV"), ("Charlie", "CHL"),
                       ("Delta", "DLT"), ("Echo", "ECH")]:
        t = DBTeam(name=name, short_code=code,
                   user_id=regular_user.id, is_placeholder=False)
        db.session.add(t)
        db.session.flush()
        teams.append(t)
    db.session.commit()
    return teams


def _corrupted_bracket(engine, regular_user, teams):
    """Rebuild tournament 32's exact corruption shape.

    QF0 is a real unplayed match; the SFs were marked 'Completed' with bogus
    bye winners before it was ever played, and the Final was fabricated from
    those two bogus winners and left playable.
    """
    a, b, c, d = teams[0], teams[1], teams[2], teams[3]

    t = Tournament(name="Corrupted KO", user_id=regular_user.id, mode="knockout",
                   status="Active", current_stage=engine.STAGE_KNOCKOUT_QF)
    db.session.add(t)
    db.session.flush()
    for team in teams:
        db.session.add(TournamentTeam(tournament_id=t.id, team_id=team.id))

    fx = {}
    # Round 1: one real match still to play, plus byes/phantoms.
    fx[0] = TournamentFixture(
        tournament_id=t.id, home_team_id=a.id, away_team_id=b.id,
        round_number=1, stage=engine.STAGE_KNOCKOUT_QF, bracket_position=0,
        status="Scheduled")
    fx[1] = TournamentFixture(
        tournament_id=t.id, home_team_id=c.id, away_team_id=None,
        round_number=1, stage=engine.STAGE_KNOCKOUT_QF, bracket_position=1,
        status="Completed", winner_team_id=c.id,
        stage_description="Bye - Advances to next round")
    fx[2] = TournamentFixture(
        tournament_id=t.id, home_team_id=d.id, away_team_id=None,
        round_number=1, stage=engine.STAGE_KNOCKOUT_QF, bracket_position=2,
        status="Completed", winner_team_id=d.id,
        stage_description="Bye - Advances to next round")
    fx[3] = TournamentFixture(
        tournament_id=t.id, home_team_id=None, away_team_id=None,
        round_number=1, stage=engine.STAGE_KNOCKOUT_QF, bracket_position=3,
        status="Completed", winner_team_id=None, stage_description="Phantom Match")
    # Round 2: prematurely 'Completed' with bogus bye winners (the bug).
    fx[4] = TournamentFixture(
        tournament_id=t.id, home_team_id=c.id, away_team_id=None,
        round_number=2, stage=engine.STAGE_KNOCKOUT_SF, bracket_position=4,
        status="Completed", winner_team_id=c.id)
    fx[5] = TournamentFixture(
        tournament_id=t.id, home_team_id=d.id, away_team_id=None,
        round_number=2, stage=engine.STAGE_KNOCKOUT_SF, bracket_position=5,
        status="Completed", winner_team_id=d.id)
    # Round 3: fabricated from those bogus winners, and playable.
    fx[6] = TournamentFixture(
        tournament_id=t.id, home_team_id=c.id, away_team_id=d.id,
        round_number=3, stage=engine.STAGE_FINAL, bracket_position=6,
        status="Scheduled")

    for f in fx.values():
        db.session.add(f)
    db.session.commit()
    return t, fx


class TestSuspectDetection:
    """The 'Scheduled' fabricated pairing is identified, not guessed at."""

    def test_corrupted_feeders_still_read_as_decided(self, app, engine, regular_user, five_teams):
        """Documents the limit of the structural check.

        Pre-repair, the bogus SFs are literally status='Completed', so the
        final's feeders *do* read as decided. Catching that is the repair
        script's job (its round-by-round walk judges each fixture against
        already-corrected feeder state), not this check's.
        """
        t, fx = _corrupted_bracket(engine, regular_user, five_teams)
        assert engine.feeders_decided(t, fx[6]) is True

    def test_fabricated_pairing_fails_check_once_feeders_are_reset(
        self, app, engine, regular_user, five_teams
    ):
        """The window the guard actually protects.

        After the repair locks the SFs, the fabricated final would still be
        'Scheduled' and playable if --include-suspects wasn't used — this is
        what stops it being cemented into a real match.
        """
        t, fx = _corrupted_bracket(engine, regular_user, five_teams)
        fx[4].status = "Locked"
        fx[4].home_team_id = fx[4].winner_team_id = None
        db.session.commit()
        assert engine.feeders_decided(t, fx[6]) is False

    def test_first_round_fixture_always_passes(self, app, engine, regular_user, five_teams):
        """No feeders to check — must never be judged fabricated."""
        t, fx = _corrupted_bracket(engine, regular_user, five_teams)
        assert engine.feeders_decided(t, fx[0]) is True

    def test_legitimately_earned_pairing_passes(self, app, engine, regular_user, five_teams):
        t, fx = _corrupted_bracket(engine, regular_user, five_teams)
        # Play QF0 for real, so both of SF0's feeders are genuinely decided.
        fx[0].status = "Completed"
        fx[0].winner_team_id = five_teams[0].id
        db.session.commit()
        assert engine.feeders_decided(t, fx[4]) is True

    def test_non_knockout_mode_is_never_judged(self, app, engine, regular_user, five_teams):
        """Playoff modes reuse bracket_position for a different shape."""
        t, fx = _corrupted_bracket(engine, regular_user, five_teams)
        t.mode = engine.MODE_IPL_STYLE
        db.session.commit()
        assert engine.feeders_decided(t, fx[6]) is True


class TestSuspectRepair:
    """--include-suspects resets the fabricated pairing; the default does not."""

    def test_default_run_leaves_suspect_untouched(self, app, engine, regular_user, five_teams, capsys):
        t, fx = _corrupted_bracket(engine, regular_user, five_teams)
        final_id = fx[6].id

        repair_run(db, app, apply=True)
        db.session.expire_all()

        final = db.session.get(TournamentFixture, final_id)
        assert final.status == "Scheduled"
        assert {final.home_team_id, final.away_team_id} == {five_teams[2].id, five_teams[3].id}
        assert "--include-suspects" in capsys.readouterr().out

    def test_include_suspects_resets_fabricated_pairing(self, app, engine, regular_user, five_teams):
        t, fx = _corrupted_bracket(engine, regular_user, five_teams)
        final_id = fx[6].id

        repair_run(db, app, apply=True, include_suspects=True)
        db.session.expire_all()

        final = db.session.get(TournamentFixture, final_id)
        assert final.status == "Locked"
        assert final.home_team_id is None
        assert final.away_team_id is None
        assert final.winner_team_id is None
        # Must land on the state _generate_knockout would have created,
        # not merely a blank one — the dashboard renders this label.
        assert final.stage_description == "Tournament Winner"

    def test_reset_fixture_restores_virgin_stage_description(self, app, engine, regular_user, five_teams):
        t, fx = _corrupted_bracket(engine, regular_user, five_teams)
        sf_id = fx[4].id

        repair_run(db, app, apply=True, include_suspects=True)
        db.session.expire_all()

        assert db.session.get(TournamentFixture, sf_id).stage_description == "Winner advances"

    def test_dry_run_commits_nothing(self, app, engine, regular_user, five_teams):
        t, fx = _corrupted_bracket(engine, regular_user, five_teams)
        final_id, sf_id = fx[6].id, fx[4].id

        repair_run(db, app, apply=False, include_suspects=True)
        db.session.expire_all()

        assert db.session.get(TournamentFixture, final_id).status == "Scheduled"
        assert db.session.get(TournamentFixture, sf_id).status == "Completed"

    def test_in_flight_match_is_never_touched(self, app, engine, regular_user, five_teams, capsys):
        """A live match is the one case status genuinely can't disambiguate."""
        t, fx = _corrupted_bracket(engine, regular_user, five_teams)
        final_id = fx[6].id

        os.makedirs(MATCH_DIR, exist_ok=True)
        marker = os.path.join(MATCH_DIR, f"match_{uuid.uuid4()}.json")
        with open(marker, "w") as f:
            json.dump({"match_id": str(uuid.uuid4()), "fixture_id": final_id}, f)
        try:
            repair_run(db, app, apply=True, include_suspects=True)
        finally:
            os.remove(marker)
        db.session.expire_all()

        final = db.session.get(TournamentFixture, final_id)
        assert final.status == "Scheduled"
        assert {final.home_team_id, final.away_team_id} == {five_teams[2].id, five_teams[3].id}
        assert "in flight" in capsys.readouterr().out

    def test_legitimate_scheduled_fixture_survives_repair(self, app, engine, regular_user, five_teams):
        """The flag must not reset pairings that were genuinely earned."""
        t, fx = _corrupted_bracket(engine, regular_user, five_teams)
        # Make SF0 a real, earned, awaiting-play match.
        fx[0].status = "Completed"
        fx[0].winner_team_id = five_teams[0].id
        fx[4].status = "Scheduled"
        fx[4].home_team_id = five_teams[0].id
        fx[4].away_team_id = five_teams[2].id
        fx[4].winner_team_id = None
        db.session.commit()
        sf_id = fx[4].id

        repair_run(db, app, apply=True, include_suspects=True)
        db.session.expire_all()

        sf = db.session.get(TournamentFixture, sf_id)
        assert sf.status == "Scheduled"
        assert {sf.home_team_id, sf.away_team_id} == {five_teams[0].id, five_teams[2].id}


class TestBracketHealsAfterRepair:
    """The point of the repair: real winners must reach their own final."""

    def test_real_winners_reach_the_final(self, app, engine, regular_user, five_teams):
        t, fx = _corrupted_bracket(engine, regular_user, five_teams)
        a, c, d = five_teams[0], five_teams[2], five_teams[3]
        final_id = fx[6].id

        repair_run(db, app, apply=True, include_suspects=True)
        db.session.expire_all()

        # Now play the bracket out for real: A wins QF0, then the SFs.
        f0 = db.session.get(TournamentFixture, fx[0].id)
        f0.status, f0.winner_team_id = "Completed", a.id
        db.session.commit()
        engine._check_knockout_progression(db.session.get(Tournament, t.id))
        db.session.commit()

        sf0 = db.session.get(TournamentFixture, fx[4].id)
        sf1 = db.session.get(TournamentFixture, fx[5].id)
        assert {sf0.home_team_id, sf0.away_team_id} == {a.id, c.id}
        sf0.status, sf0.winner_team_id = "Completed", a.id
        sf1.status, sf1.winner_team_id = "Completed", sf1.home_team_id or d.id
        db.session.commit()
        engine._check_knockout_progression(db.session.get(Tournament, t.id))
        db.session.commit()

        final = db.session.get(TournamentFixture, final_id)
        assert final.status == "Scheduled"
        assert a.id in {final.home_team_id, final.away_team_id}, (
            "the real QF0 winner must reach the final it earned"
        )


class TestLaunchGuard:
    """A fabricated pairing must not be playable once its feeders are locked."""

    def test_fixture_with_undecided_feeders_cannot_be_started(
        self, app, client, engine, regular_user, five_teams
    ):
        t, fx = _corrupted_bracket(engine, regular_user, five_teams)
        final_id = fx[6].id
        # Post-repair state: SFs locked, fabricated final still 'Scheduled'.
        for bp in (4, 5):
            fx[bp].status = "Locked"
            fx[bp].home_team_id = fx[bp].away_team_id = fx[bp].winner_team_id = None
        db.session.commit()

        login_user(client, regular_user.email, "Password123!")
        resp = client.get(f"/match/setup?fixture_id={final_id}", follow_redirects=False)

        assert resp.status_code == 302
        assert "/match/setup" not in resp.headers["Location"]

    def test_legitimate_fixture_is_still_playable(
        self, app, client, engine, regular_user, five_teams
    ):
        """The guard must not block a pairing that was genuinely earned."""
        t, fx = _corrupted_bracket(engine, regular_user, five_teams)

        login_user(client, regular_user.email, "Password123!")
        resp = client.get(f"/match/setup?fixture_id={fx[0].id}", follow_redirects=False)

        assert resp.status_code == 200
