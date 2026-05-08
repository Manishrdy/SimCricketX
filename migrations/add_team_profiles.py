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
                _step5_assign_orphaned_players(conn)
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


def _step5_assign_orphaned_players(conn):
    """Assign players with profile_id IS NULL to their team's T20 profile.

    Orphans whose name already exists in the target profile are stale duplicates
    (left over from pre-profile data) and are deleted instead of migrated, since
    the (profile_id, name) unique constraint would otherwise fail the UPDATE.
    """
    orphans = conn.execute(
        text("SELECT id, team_id, name FROM players WHERE profile_id IS NULL")
    ).fetchall()

    updated = 0
    deleted = 0
    for (player_id, team_id, name) in orphans:
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
            conn.execute(
                text("DELETE FROM players WHERE id = :plid"),
                {"plid": player_id},
            )
            deleted += 1
        else:
            conn.execute(
                text("UPDATE players SET profile_id = :pid WHERE id = :plid"),
                {"pid": profile[0], "plid": player_id},
            )
            updated += 1
    print(
        f"[Migration] Step 5: assigned {updated} orphan(s) to T20 profiles, "
        f"deleted {deleted} stale duplicate(s)."
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
