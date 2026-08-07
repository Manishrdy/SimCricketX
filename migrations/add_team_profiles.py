"""
Team Format Profiles Migration
================================

Adds the team_profiles table and migrates existing players to T20 profiles.

This migration is safe to run multiple times (idempotent). It is also called
automatically at app startup by app.py if the migration has not yet been applied.

Manual usage (optional):
    python migrations/add_team_profiles.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception


def run_migration(db, app):
    """
    Apply team-profiles migration within the given app context.

    Steps:
    1. Create the team_profiles table (if absent).
    2. Add profile_id column to players (if absent).
    3. Recreate the players table to swap the unique constraint from
       (team_id, name) → (profile_id, name).
    4. Create T20 profiles for every team that has none.
    5. Assign orphaned players (profile_id IS NULL) to their team's T20 profile.
    """
    with app.app_context():
        conn = db.engine.connect()
        try:
            with conn.begin():
                _step1_create_team_profiles(conn)
                _step2_add_profile_id_column(conn)
            _step3_swap_unique_constraint(conn)
            with conn.begin():
                _step4_create_t20_profiles(conn)
                step5_failures = _step5_assign_orphaned_players(conn)
            # Log after the write transaction commits — log_exception writes to
            # exception_log on a separate connection and would deadlock against
            # the migration's own SQLite write lock if called mid-transaction.
            for player_id, exc in step5_failures:
                log_exception(exc, source="sqlite", context={
                    "migration": "add_team_profiles",
                    "step": "step5_assign_orphaned_players",
                    "player_id": player_id,
                })
            print("[Migration] add_team_profiles: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_team_profiles"})
            print(f"[Migration] add_team_profiles: FAILED — {exc}")
            raise
        finally:
            conn.close()


# ── Step helpers ──────────────────────────────────────────────────────────────

def _step1_create_team_profiles(conn):
    """Create team_profiles table if it doesn't exist."""
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS team_profiles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id     INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            format_type VARCHAR(20) NOT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(team_id, format_type)
        )
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_team_profiles_team_id ON team_profiles(team_id)
    """))
    print("[Migration] Step 1: team_profiles table ready.")


def _step2_add_profile_id_column(conn):
    """Add profile_id column to players if not already present."""
    result = conn.execute(text("PRAGMA table_info(players)")).fetchall()
    col_names = [row[1] for row in result]
    if 'profile_id' not in col_names:
        conn.execute(text("""
            ALTER TABLE players
            ADD COLUMN profile_id INTEGER REFERENCES team_profiles(id) ON DELETE CASCADE
        """))
        print("[Migration] Step 2: profile_id column added to players.")
    else:
        print("[Migration] Step 2: profile_id column already present — skipped.")


def _step3_swap_unique_constraint(conn):
    """
    Replace the (team_id, name) unique constraint with (profile_id, name) by
    recreating the players table. Safe to skip if already done.
    """
    # Detect by actual table DDL, not index names (SQLite auto-generates names).
    with conn.begin():
        players_ddl_row = conn.execute(text("""
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = 'players'
        """)).fetchone()
        players_ddl = (players_ddl_row[0] or "").replace(" ", "").lower() if players_ddl_row else ""
        if "unique(profile_id,name)" in players_ddl:
            # Cleanup stale temp table from any previously interrupted run.
            conn.execute(text("DROP TABLE IF EXISTS players_new"))
            print("[Migration] Step 3: Unique constraint already updated — skipped.")
            return

        # Recover from interrupted prior run before creating players_new again.
        conn.execute(text("DROP TABLE IF EXISTS players_new"))
        cols_info = conn.execute(text("PRAGMA table_info(players)")).fetchall()

    # Build column definitions preserving all columns.
    col_defs = []
    for col in cols_info:
        _, cname, ctype, notnull, dflt, pk = col
        if pk:
            col_defs.append(f"    {cname} {ctype} PRIMARY KEY AUTOINCREMENT")
            continue
        parts = [f"    {cname} {ctype or 'TEXT'}"]
        if notnull:
            parts.append("NOT NULL")
        if dflt is not None:
            parts.append(f"DEFAULT {dflt}")
        if cname == "team_id":
            parts.append("REFERENCES teams(id)")
        elif cname == "profile_id":
            parts.append("REFERENCES team_profiles(id) ON DELETE CASCADE")
        col_defs.append(" ".join(parts))

    col_defs_sql = ",\n".join(col_defs)
    col_names = [col[1] for col in cols_info]
    cols_csv = ", ".join(col_names)

    with conn.begin():
        conn.execute(text("""
            CREATE TABLE players_new (
    """ + col_defs_sql + """,
                UNIQUE(profile_id, name)
            )
        """))
        conn.execute(text(f"INSERT INTO players_new ({cols_csv}) SELECT {cols_csv} FROM players"))

    # Swap tables safely with FK checks temporarily disabled. This is required
    # because historical tables reference players(id).
    conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    try:
        with conn.begin():
            conn.execute(text("DROP TABLE players"))
            conn.execute(text("ALTER TABLE players_new RENAME TO players"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_players_team_id ON players(team_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_players_profile_id ON players(profile_id)"))
    finally:
        conn.exec_driver_sql("PRAGMA foreign_keys = ON")

    print("[Migration] Step 3: Unique constraint swapped to (profile_id, name).")


def _step4_create_t20_profiles(conn):
    """Create a T20 TeamProfile for every team that has no profiles yet."""
    teams = conn.execute(text("SELECT id FROM teams")).fetchall()
    created = 0
    for (team_id,) in teams:
        existing = conn.execute(
            text("SELECT id FROM team_profiles WHERE team_id = :tid"),
            {"tid": team_id},
        ).fetchone()
        if not existing:
            conn.execute(
                text("INSERT INTO team_profiles (team_id, format_type) VALUES (:tid, 'T20')"),
                {"tid": team_id},
            )
            created += 1
    print(f"[Migration] Step 4: Created T20 profiles for {created} team(s).")


# Career-stat columns that are summed when merging a duplicate player pair.
_SUM_STAT_COLS = (
    "matches_played", "total_runs", "total_balls_faced", "total_fours",
    "total_sixes", "total_fifties", "total_centuries", "not_outs",
    "total_balls_bowled", "total_runs_conceded", "total_wickets",
    "total_maidens", "five_wicket_hauls",
)


def _step5_assign_orphaned_players(conn):
    """Assign players with profile_id IS NULL to their team's T20 profile.

    An orphan whose name already exists in the target profile is the same real
    person twice: the legacy pre-profile row (often carrying career stats and
    match history) plus a profile row recreated through the team UI. The two
    are MERGED — history rows re-pointed, career aggregates combined — and only
    then is the legacy row deleted. A bare DELETE is never issued:
    match_partnerships and tournament_player_stats_cache reference players(id)
    without ON DELETE CASCADE, so it fails whenever the orphan has history, and
    match_scorecards' CASCADE would silently erase career scorecards.

    Each orphan runs in its own SAVEPOINT so one bad row cannot roll back the
    whole step (previously one failure wedged the migration on every startup).
    Returns a list of (player_id, exception) for rows that could not be
    processed; the caller logs them after the transaction commits.
    """
    orphans = conn.execute(
        text("SELECT id, team_id, name FROM players WHERE profile_id IS NULL")
    ).fetchall()

    updated = 0
    merged = 0
    failures = []
    for (player_id, team_id, name) in orphans:
        try:
            with conn.begin_nested():
                profile = conn.execute(
                    text("SELECT id FROM team_profiles WHERE team_id = :tid AND format_type = 'T20'"),
                    {"tid": team_id},
                ).fetchone()
                if not profile:
                    continue
                dupe = conn.execute(
                    text("SELECT id FROM players WHERE profile_id = :pid AND name = :name"),
                    {"pid": profile[0], "name": name},
                ).fetchone()
                if dupe:
                    _merge_duplicate_player(conn, orphan_id=player_id, survivor_id=dupe[0])
                    merged += 1
                else:
                    conn.execute(
                        text("UPDATE players SET profile_id = :pid WHERE id = :plid"),
                        {"pid": profile[0], "plid": player_id},
                    )
                    updated += 1
        except Exception as exc:  # noqa: BLE001 — isolate per-player failures
            failures.append((player_id, exc))
    print(
        f"[Migration] Step 5: assigned {updated} orphan(s) to T20 profiles, "
        f"merged {merged} duplicate(s), {len(failures)} failure(s)."
    )
    return failures


def _merge_duplicate_player(conn, orphan_id, survivor_id):
    """Fold a legacy duplicate player row into its profile-bound survivor.

    Re-points every table referencing players(id), combines career aggregates
    onto the survivor, then deletes the legacy row. Identity fields (role,
    ratings, captain/keeper flags) keep the survivor's values — they reflect
    the user's latest edits through the profile UI.
    """
    conn.execute(
        text("UPDATE match_scorecards SET player_id = :sid WHERE player_id = :oid"),
        {"sid": survivor_id, "oid": orphan_id},
    )
    conn.execute(
        text("UPDATE match_partnerships SET batsman1_id = :sid WHERE batsman1_id = :oid"),
        {"sid": survivor_id, "oid": orphan_id},
    )
    conn.execute(
        text("UPDATE match_partnerships SET batsman2_id = :sid WHERE batsman2_id = :oid"),
        {"sid": survivor_id, "oid": orphan_id},
    )

    # Tournament stats cache: where BOTH rows appear in the same tournament the
    # merged numbers cannot be derived from cache rows alone, so drop that
    # tournament's cache entirely — the stats service recomputes from
    # match_scorecards, which is authoritative after the re-point above.
    # Remaining orphan rows (no overlap) are simply re-pointed.
    conn.execute(text("""
        DELETE FROM tournament_player_stats_cache
        WHERE tournament_id IN (
            SELECT o.tournament_id
            FROM tournament_player_stats_cache o
            JOIN tournament_player_stats_cache s
              ON s.tournament_id = o.tournament_id AND s.player_id = :sid
            WHERE o.player_id = :oid
        )
    """), {"sid": survivor_id, "oid": orphan_id})
    conn.execute(
        text("UPDATE tournament_player_stats_cache SET player_id = :sid WHERE player_id = :oid"),
        {"sid": survivor_id, "oid": orphan_id},
    )

    _combine_career_stats(conn, orphan_id, survivor_id)

    conn.execute(text("DELETE FROM players WHERE id = :oid"), {"oid": orphan_id})


def _combine_career_stats(conn, orphan_id, survivor_id):
    """Write the pair's combined career aggregates onto the survivor row."""
    cols_csv = ", ".join(
        _SUM_STAT_COLS + ("highest_score", "best_bowling_wickets", "best_bowling_runs")
    )
    rows = conn.execute(
        text(f"SELECT id, {cols_csv} FROM players WHERE id IN (:oid, :sid)"),
        {"oid": orphan_id, "sid": survivor_id},
    ).mappings().fetchall()
    by_id = {row["id"]: row for row in rows}
    orphan, survivor = by_id[orphan_id], by_id[survivor_id]

    combined = {
        col: (orphan[col] or 0) + (survivor[col] or 0) for col in _SUM_STAT_COLS
    }
    combined["highest_score"] = max(
        orphan["highest_score"] or 0, survivor["highest_score"] or 0
    )

    # Best bowling: only rows that actually bowled may contribute, otherwise a
    # never-bowled row's (0, 0) default would beat a genuine best like 1/23.
    candidates = [
        (row["best_bowling_wickets"] or 0, row["best_bowling_runs"] or 0)
        for row in (orphan, survivor)
        if (row["total_balls_bowled"] or 0) > 0
    ]
    if candidates:
        best = min(candidates, key=lambda wr: (-wr[0], wr[1]))
        combined["best_bowling_wickets"], combined["best_bowling_runs"] = best

    set_sql = ", ".join(f"{col} = :{col}" for col in combined)
    conn.execute(
        text(f"UPDATE players SET {set_sql} WHERE id = :sid"),
        {**combined, "sid": survivor_id},
    )


# ── Standalone entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("Team Format Profiles - Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
