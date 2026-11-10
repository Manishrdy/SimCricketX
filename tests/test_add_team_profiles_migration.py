"""
Tests for migrations/add_team_profiles.py step 5 (orphan assignment/merge).

Regression coverage for the production IntegrityError:
    DELETE FROM players WHERE id = ? → FOREIGN KEY constraint failed
Step 5 used to blind-DELETE duplicate-name orphans, but match_partnerships (and
the since-retired tournament_player_stats_cache, which can still exist on an
undropped database) reference players(id) without ON DELETE CASCADE,
and the orphan (legacy pre-profile row) usually carries real match history.
The fix merges the orphan into the surviving profile row instead.
"""

import uuid

import pytest
from sqlalchemy import text

from app import db
from database.models import (
    Team as DBTeam,
    TeamProfile,
    Player as DBPlayer,
    Match as DBMatch,
    MatchScorecard,
    MatchPartnership,
    Tournament,
)
from migrations.add_team_profiles import _step5_assign_orphaned_players


LEGACY_CACHE_DDL = """
    CREATE TABLE tournament_player_stats_cache (
        id INTEGER PRIMARY KEY,
        tournament_id INTEGER NOT NULL REFERENCES tournaments (id),
        player_id INTEGER NOT NULL REFERENCES players (id),
        team_id INTEGER NOT NULL REFERENCES teams (id),
        matches_played INTEGER DEFAULT 0
    )
"""


def _run_step5():
    """Run step 5 the same way run_migration does (FK enforcement is ON via
    the engine's connect listener)."""
    with db.engine.connect() as conn:
        with conn.begin():
            failures = _step5_assign_orphaned_players(conn)
    return failures


def _make_team_with_profile(user_id, name="Merge Test XI"):
    team = DBTeam(
        name=name,
        short_code=name[:3].upper(),
        user_id=user_id,
        is_placeholder=False,
        is_draft=False,
    )
    db.session.add(team)
    db.session.flush()
    profile = TeamProfile(team_id=team.id, format_type="T20")
    db.session.add(profile)
    db.session.flush()
    return team, profile


@pytest.fixture
def merge_scenario(app, regular_user):
    """The production failure state: a legacy orphan with match history whose
    name already exists in the team's T20 profile."""
    team, profile = _make_team_with_profile(regular_user.id)

    survivor = DBPlayer(
        team_id=team.id,
        profile_id=profile.id,
        name="Ravi Sharma",
        role="All-rounder",
        matches_played=2,
        total_runs=80,
        total_balls_faced=60,
        total_fours=8,
        highest_score=55,
        total_balls_bowled=24,
        total_runs_conceded=40,
        total_wickets=1,
        best_bowling_wickets=1,
        best_bowling_runs=18,
    )
    orphan = DBPlayer(
        team_id=team.id,
        profile_id=None,
        name="Ravi Sharma",
        role="All-rounder",
        matches_played=10,
        total_runs=300,
        total_balls_faced=250,
        total_fours=30,
        total_sixes=10,
        total_fifties=2,
        highest_score=90,
        not_outs=1,
        total_balls_bowled=120,
        total_runs_conceded=180,
        total_wickets=8,
        best_bowling_wickets=3,
        best_bowling_runs=25,
    )
    partner = DBPlayer(
        team_id=team.id, profile_id=profile.id, name="Other Batter", role="Batsman"
    )
    db.session.add_all([survivor, orphan, partner])
    db.session.flush()

    match = DBMatch(id=str(uuid.uuid4()), user_id=regular_user.id)
    db.session.add(match)
    db.session.flush()

    db.session.add(
        MatchScorecard(
            match_id=match.id,
            player_id=orphan.id,
            team_id=team.id,
            innings_number=1,
            record_type="batting",
        )
    )
    db.session.add(
        MatchPartnership(
            match_id=match.id,
            innings_number=1,
            wicket_number=1,
            batsman1_id=orphan.id,
            batsman2_id=partner.id,
            runs=45,
        )
    )

    t_repoint = Tournament(user_id=regular_user.id, name="Repoint Cup")
    t_overlap = Tournament(user_id=regular_user.id, name="Overlap Cup")
    db.session.add_all([t_repoint, t_overlap])
    db.session.flush()
    db.session.commit()
    # The retired cache table as it still exists on a database that has not
    # run `drop_tournament_player_stats_cache --apply` yet: no model, but
    # live foreign keys onto players with no ON DELETE action.
    with db.engine.begin() as conn:
        conn.execute(text(LEGACY_CACHE_DDL))
        for tournament_id, player_id in (
            (t_repoint.id, orphan.id),
            (t_overlap.id, orphan.id),
            (t_overlap.id, survivor.id),
        ):
            conn.execute(
                text(
                    "INSERT INTO tournament_player_stats_cache "
                    "(tournament_id, player_id, team_id) VALUES (:t, :p, :team)"
                ),
                {"t": tournament_id, "p": player_id, "team": team.id},
            )

    return {
        "team": team,
        "profile": profile,
        "survivor_id": survivor.id,
        "orphan_id": orphan.id,
        "partner_id": partner.id,
        "match_id": match.id,
        "t_repoint_id": t_repoint.id,
        "t_overlap_id": t_overlap.id,
    }


def test_duplicate_orphan_with_history_is_merged_not_deleted(app, merge_scenario):
    s = merge_scenario
    failures = _run_step5()
    assert failures == []

    db.session.expire_all()
    assert db.session.get(DBPlayer, s["orphan_id"]) is None

    survivor = db.session.get(DBPlayer, s["survivor_id"])
    assert survivor.profile_id == s["profile"].id
    # Summed aggregates
    assert survivor.matches_played == 12
    assert survivor.total_runs == 380
    assert survivor.total_balls_faced == 310
    assert survivor.total_fours == 38
    assert survivor.total_sixes == 10
    assert survivor.total_fifties == 2
    assert survivor.not_outs == 1
    assert survivor.total_balls_bowled == 144
    assert survivor.total_runs_conceded == 220
    assert survivor.total_wickets == 9
    # Max / best-of aggregates
    assert survivor.highest_score == 90
    assert survivor.best_bowling_wickets == 3
    assert survivor.best_bowling_runs == 25

    # History rows re-pointed, none orphaned or lost
    scorecards = MatchScorecard.query.filter_by(match_id=s["match_id"]).all()
    assert [sc.player_id for sc in scorecards] == [s["survivor_id"]]
    pship = MatchPartnership.query.filter_by(match_id=s["match_id"]).one()
    assert pship.batsman1_id == s["survivor_id"]
    assert pship.batsman2_id == s["partner_id"]

    # Retired cache: the orphan's rows are gone (they would have blocked the
    # DELETE), the survivor's untouched row is left for the drop migration.
    with db.engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT tournament_id, player_id FROM tournament_player_stats_cache"
        )).fetchall()
        conn.execute(text("DROP TABLE tournament_player_stats_cache"))
    assert rows == [(s["t_overlap_id"], s["survivor_id"])]


def test_orphan_without_duplicate_is_assigned_to_profile(app, regular_user):
    team, profile = _make_team_with_profile(regular_user.id, name="Assign XI")
    orphan = DBPlayer(team_id=team.id, profile_id=None, name="Solo Player")
    db.session.add(orphan)
    db.session.commit()

    failures = _run_step5()
    assert failures == []

    db.session.expire_all()
    assert db.session.get(DBPlayer, orphan.id).profile_id == profile.id


def test_best_bowling_ignores_never_bowled_row(app, regular_user):
    """A never-bowled row's (0, 0) defaults must not beat a real best figure."""
    team, profile = _make_team_with_profile(regular_user.id, name="Bowling XI")
    survivor = DBPlayer(
        team_id=team.id,
        profile_id=profile.id,
        name="Pace Ace",
        total_balls_bowled=0,
        best_bowling_wickets=0,
        best_bowling_runs=0,
    )
    orphan = DBPlayer(
        team_id=team.id,
        profile_id=None,
        name="Pace Ace",
        total_balls_bowled=60,
        total_wickets=4,
        best_bowling_wickets=2,
        best_bowling_runs=30,
    )
    db.session.add_all([survivor, orphan])
    db.session.commit()

    assert _run_step5() == []
    db.session.expire_all()
    merged = db.session.get(DBPlayer, survivor.id)
    assert merged.best_bowling_wickets == 2
    assert merged.best_bowling_runs == 30


def test_one_failing_row_does_not_block_other_orphans(app, regular_user):
    """A row that still cannot be processed is reported but does not roll back
    the rest of the step (previously the whole migration failed on startup)."""
    team, profile = _make_team_with_profile(regular_user.id, name="Savepoint XI")
    survivor = DBPlayer(
        team_id=team.id, profile_id=profile.id, name="Blocked Player"
    )
    blocked = DBPlayer(team_id=team.id, profile_id=None, name="Blocked Player")
    fine = DBPlayer(team_id=team.id, profile_id=None, name="Fine Player")
    db.session.add_all([survivor, blocked, fine])
    db.session.commit()

    # Simulate an out-of-model legacy table referencing players(id) without
    # cascade, so the merge's final DELETE fails for `blocked` only.
    with db.engine.connect() as conn:
        with conn.begin():
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS legacy_ref "
                "(id INTEGER PRIMARY KEY, pid INTEGER REFERENCES players(id))"
            ))
            conn.execute(
                text("INSERT INTO legacy_ref (pid) VALUES (:pid)"),
                {"pid": blocked.id},
            )
    try:
        failures = _run_step5()
        assert [pid for pid, _ in failures] == [blocked.id]

        db.session.expire_all()
        # The healthy orphan was still migrated…
        assert db.session.get(DBPlayer, fine.id).profile_id == profile.id
        # …and the blocked orphan is untouched (retried next startup).
        assert db.session.get(DBPlayer, blocked.id).profile_id is None
    finally:
        with db.engine.connect() as conn:
            with conn.begin():
                conn.execute(text("DROP TABLE IF EXISTS legacy_ref"))
