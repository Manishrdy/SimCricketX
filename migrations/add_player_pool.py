"""
Player Pool Migration
========================

Adds the master_players and user_players tables for the centralized player pool.

This migration is safe to run multiple times (idempotent). It is also called
automatically at app startup by app.py if the migration has not yet been applied.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception


def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            _step1_create_master_players(conn)
            _step2_create_user_players(conn)
            trans.commit()
            print("[Migration] add_player_pool: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_player_pool"})
            trans.rollback()
            print(f"[Migration] add_player_pool: FAILED — {exc}")
            raise
        finally:
            conn.close()


def _step1_create_master_players(conn):
    """Create master_players table if it doesn't exist."""
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS master_players (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        VARCHAR(100) NOT NULL,
            role        VARCHAR(50),
            batting_rating  INTEGER DEFAULT 50,
            bowling_rating  INTEGER DEFAULT 50,
            fielding_rating INTEGER DEFAULT 50,
            batting_hand    VARCHAR(20),
            bowling_type    VARCHAR(50),
            bowling_hand    VARCHAR(20),
            is_captain      BOOLEAN DEFAULT 0,
            is_wicketkeeper BOOLEAN DEFAULT 0,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(name)
        )
    """))
    print("[Migration] Step 1: master_players table ready.")


def _step2_create_user_players(conn):
    """Create user_players table if it doesn't exist."""
    result = conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_players'"
    )).fetchone()
    if result is not None:
        print("[Migration] Step 2: user_players table already exists — skipped.")
        return

    conn.execute(text("""
        CREATE TABLE user_players (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         VARCHAR(120) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            master_player_id INTEGER REFERENCES master_players(id) ON DELETE CASCADE,
            name            VARCHAR(100) NOT NULL,
            role            VARCHAR(50),
            batting_rating  INTEGER DEFAULT 50,
            bowling_rating  INTEGER DEFAULT 50,
            fielding_rating INTEGER DEFAULT 50,
            batting_hand    VARCHAR(20),
            bowling_type    VARCHAR(50),
            bowling_hand    VARCHAR(20),
            is_captain      BOOLEAN DEFAULT 0,
            is_wicketkeeper BOOLEAN DEFAULT 0,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, master_player_id)
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_user_players_user_id ON user_players(user_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_user_players_master_id ON user_players(master_player_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_user_player_user_name ON user_players(user_id, name)"))
    conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_user_custom_player_name
        ON user_players(user_id, name) WHERE master_player_id IS NULL
    """))
    print("[Migration] Step 2: user_players table ready.")


if __name__ == "__main__":
    print("=" * 60)
    print("Player Pool - Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
