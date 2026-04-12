"""
Auction Module Migration
===========================

Adds auction_events, auction_categories, auction_teams, auction_players,
auction_player_categories, and auction_bids tables.

Idempotent — safe to run multiple times.
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
            _create_auction_events(conn)
            _create_auction_categories(conn)
            _create_auction_teams(conn)
            _create_auction_players(conn)
            _create_auction_player_categories(conn)
            _create_auction_bids(conn)
            trans.commit()
            print("[Migration] add_auction: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_auction"})
            trans.rollback()
            print(f"[Migration] add_auction: FAILED — {exc}")
            raise
        finally:
            conn.close()


def _create_auction_events(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS auction_events (
            id                              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id                         VARCHAR(120) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name                            VARCHAR(200) NOT NULL,
            format                          VARCHAR(20) DEFAULT 'T20',
            status                          VARCHAR(20) DEFAULT 'setup',
            num_teams                       INTEGER DEFAULT 8,
            budget_mode                     VARCHAR(20) DEFAULT 'uniform',
            uniform_budget                  BIGINT DEFAULT 0,
            bid_increment_tiers             TEXT DEFAULT '[]',
            min_players_per_team            INTEGER DEFAULT 12,
            max_players_per_team            INTEGER DEFAULT 25,
            reauction_enabled               BOOLEAN DEFAULT 0,
            max_reauction_rounds            INTEGER,
            reauction_base_price_reduction_pct INTEGER DEFAULT 0,
            current_round                   INTEGER DEFAULT 1,
            created_at                      DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at                      DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_events_user ON auction_events(user_id)"))
    print("[Migration] auction_events table ready.")


def _create_auction_categories(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS auction_categories (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id            INTEGER NOT NULL REFERENCES auction_events(id) ON DELETE CASCADE,
            name                VARCHAR(100) NOT NULL,
            default_base_price  BIGINT DEFAULT 0,
            max_per_team        INTEGER,
            UNIQUE(event_id, name)
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_categories_event ON auction_categories(event_id)"))
    print("[Migration] auction_categories table ready.")


def _create_auction_teams(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS auction_teams (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id        INTEGER NOT NULL REFERENCES auction_events(id) ON DELETE CASCADE,
            name            VARCHAR(200) NOT NULL,
            access_token    VARCHAR(36) NOT NULL UNIQUE,
            custom_budget   BIGINT,
            purse_remaining BIGINT DEFAULT 0,
            players_bought  INTEGER DEFAULT 0,
            UNIQUE(event_id, name)
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_teams_event ON auction_teams(event_id)"))
    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_auction_teams_token ON auction_teams(access_token)"))
    print("[Migration] auction_teams table ready.")


def _create_auction_players(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS auction_players (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id            INTEGER NOT NULL REFERENCES auction_events(id) ON DELETE CASCADE,
            master_player_id    INTEGER REFERENCES master_players(id) ON DELETE SET NULL,
            user_player_id      INTEGER REFERENCES user_players(id) ON DELETE SET NULL,
            name                VARCHAR(100) NOT NULL,
            role                VARCHAR(50),
            batting_rating      INTEGER DEFAULT 50,
            bowling_rating      INTEGER DEFAULT 50,
            fielding_rating     INTEGER DEFAULT 50,
            batting_hand        VARCHAR(20),
            bowling_type        VARCHAR(50),
            bowling_hand        VARCHAR(20),
            base_price          BIGINT,
            status              VARCHAR(20) DEFAULT 'upcoming',
            sold_to             INTEGER REFERENCES auction_teams(id) ON DELETE SET NULL,
            sold_price          BIGINT,
            sold_in_round       INTEGER,
            lot_order           INTEGER
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_players_event ON auction_players(event_id)"))
    print("[Migration] auction_players table ready.")


def _create_auction_player_categories(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS auction_player_categories (
            auction_player_id   INTEGER NOT NULL REFERENCES auction_players(id) ON DELETE CASCADE,
            category_id         INTEGER NOT NULL REFERENCES auction_categories(id) ON DELETE CASCADE,
            PRIMARY KEY (auction_player_id, category_id)
        )
    """))
    print("[Migration] auction_player_categories table ready.")


def _create_auction_bids(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS auction_bids (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_player_id   INTEGER NOT NULL REFERENCES auction_players(id) ON DELETE CASCADE,
            team_id             INTEGER NOT NULL REFERENCES auction_teams(id) ON DELETE CASCADE,
            amount              BIGINT NOT NULL,
            round               INTEGER DEFAULT 1,
            timestamp           DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_bids_player ON auction_bids(auction_player_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_bids_team ON auction_bids(team_id)"))
    print("[Migration] auction_bids table ready.")


if __name__ == "__main__":
    print("=" * 60)
    print("Auction Module - Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
