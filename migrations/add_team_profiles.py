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
        trans = conn.begin()
        try:
            _step1_create_team_profiles(conn)
            _step2_add_profile_id_column(conn)
            _step3_swap_unique_constraint(conn)
            _step4_create_t20_profiles(conn)
            _step5_assign_orphaned_players(conn)
            trans.commit()
            print("[Migration] add_team_profiles: completed successfully.")
        except Exception as exc:
            trans.rollback()
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
    # Check current unique indexes on players
    indexes = conn.execute(text("PRAGMA index_list(players)")).fetchall()
    index_names = [row[1] for row in indexes]

    if 'uq_player_profile_name' in index_names:
        print("[Migration] Step 3: Unique constraint already updated — skipped.")
        return

    # Collect current columns to rebuild CREATE TABLE statement
    cols_info = conn.execute(text("PRAGMA table_info(players)")).fetchall()
    # col: (cid, name, type, notnull, dflt_value, pk)

    # Build column definitions preserving all columns
    col_defs = []
    for col in cols_info:
        cid, cname, ctype, notnull, dflt, pk = col
        if pk:
            col_defs.append(f"    {cname} {ctype} PRIMARY KEY AUTOINCREMENT")
            continue
        parts = [f"    {cname} {ctype or 'TEXT'}"]
        if notnull:
            parts.append("NOT NULL")
        if dflt is not None:
            parts.append(f"DEFAULT {dflt}")
        # Re-attach FK for known FK columns
        if cname == 'team_id':
            parts.append("REFERENCES teams(id)")
        elif cname == 'profile_id':
            parts.append("REFERENCES team_profiles(id) ON DELETE CASCADE")
        col_defs.append(" ".join(parts))

    col_defs_sql = ",\n".join(col_defs)

    conn.execute(text("""
        CREATE TABLE players_new (
""" + col_defs_sql + """,
            UNIQUE(profile_id, name)
        )
    """))

    # Copy all data
    col_names = [col[1] for col in cols_info]
    cols_csv = ", ".join(col_names)
    conn.execute(text(f"INSERT INTO players_new ({cols_csv}) SELECT {cols_csv} FROM players"))

    # Swap tables
    conn.execute(text("DROP TABLE players"))
    conn.execute(text("ALTER TABLE players_new RENAME TO players"))

    # Recreate indexes
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_players_team_id ON players(team_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_players_profile_id ON players(profile_id)"))

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
    """Assign players with profile_id IS NULL to their team's T20 profile."""
    orphans = conn.execute(
        text("SELECT id, team_id FROM players WHERE profile_id IS NULL")
    ).fetchall()

    updated = 0
    for (player_id, team_id) in orphans:
        profile = conn.execute(
            text("SELECT id FROM team_profiles WHERE team_id = :tid AND format_type = 'T20'"),
            {"tid": team_id},
        ).fetchone()
        if profile:
            conn.execute(
                text("UPDATE players SET profile_id = :pid WHERE id = :plid"),
                {"pid": profile[0], "plid": player_id},
            )
            updated += 1
    print(f"[Migration] Step 5: Assigned {updated} orphaned player(s) to T20 profiles.")


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
