"""
AUCTION-REDESIGN — Phase 1 Migration
=====================================

Drops the old standalone auction tables (auction_events, auction_categories,
auction_teams, auction_players, auction_player_categories, auction_bids) and
creates the new hierarchy:

    leagues → seasons → season_teams → teams (nullable season_id FK)

Idempotent: safe to re-run. Drops only tables that exist; creates only tables
that don't; adds the teams.season_id column only if missing.

The old tables are confirmed unused in production prior to this migration
(see AUCTION_REDESIGN_PLAN.md).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception


OLD_AUCTION_TABLES = [
    "auction_bids",
    "auction_player_categories",
    "auction_players",
    "auction_teams",
    "auction_categories",
    "auction_events",
]


def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            _drop_old_auction_tables(conn)
            _create_leagues(conn)
            _create_seasons(conn)
            _create_season_teams(conn)
            _add_season_id_to_teams(conn)
            trans.commit()
            print("[Migration] redesign_auction_phase1: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "redesign_auction_phase1"})
            trans.rollback()
            print(f"[Migration] redesign_auction_phase1: FAILED — {exc}")
            raise
        finally:
            conn.close()


def _drop_old_auction_tables(conn):
    for tbl in OLD_AUCTION_TABLES:
        conn.execute(text(f"DROP TABLE IF EXISTS {tbl}"))
    print(f"[Migration] dropped old auction tables: {', '.join(OLD_AUCTION_TABLES)}")


def _create_leagues(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS leagues (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     VARCHAR(120) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name        VARCHAR(200) NOT NULL,
            short_code  VARCHAR(10),
            frequency   VARCHAR(20) DEFAULT 'one_time',
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, name)
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_leagues_user ON leagues(user_id)"))
    print("[Migration] leagues table ready.")


def _create_seasons(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS seasons (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            league_id     INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            name          VARCHAR(200) NOT NULL,
            format        VARCHAR(20) DEFAULT 'T20',
            auction_mode  VARCHAR(20) DEFAULT 'traditional',
            status        VARCHAR(30) DEFAULT 'setup',
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(league_id, name)
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_seasons_league ON seasons(league_id)"))
    print("[Migration] seasons table ready.")


def _create_season_teams(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS season_teams (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            season_id       INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
            team_id         INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            display_name    VARCHAR(200) NOT NULL,
            access_token    VARCHAR(36) NOT NULL UNIQUE,
            custom_budget   BIGINT,
            purse_remaining BIGINT DEFAULT 0,
            players_bought  INTEGER DEFAULT 0,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(season_id, display_name)
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_season_teams_season ON season_teams(season_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_season_teams_team ON season_teams(team_id)"))
    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_season_teams_token ON season_teams(access_token)"))
    print("[Migration] season_teams table ready.")


def _add_season_id_to_teams(conn):
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(teams)")).fetchall()}
    if "season_id" not in cols:
        conn.execute(text("ALTER TABLE teams ADD COLUMN season_id INTEGER REFERENCES seasons(id) ON DELETE CASCADE"))
        print("[Migration] teams.season_id column added.")
    else:
        print("[Migration] teams.season_id already present — skipped.")
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_teams_season ON teams(season_id)"))


if __name__ == "__main__":
    print("=" * 60)
    print("AUCTION-REDESIGN Phase 1 — Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
