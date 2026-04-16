"""
AUCTION-REDESIGN — Phase 2 Migration
=====================================

Creates the Auction setup tables:
    auctions            — 1:1 with seasons; holds all auction config
    auction_categories  — user-defined pool tiers under an auction
    auction_players     — curated pool (snapshot from master/user pool)

Idempotent: safe to re-run. All CREATEs use IF NOT EXISTS.

Runtime tables for bids/picks/chat land in later phases.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception


def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            _create_auctions(conn)
            _ensure_bid_increment_column(conn)
            _create_auction_categories(conn)
            _ensure_max_players_column(conn)
            _create_auction_players(conn)
            trans.commit()
            print("[Migration] auction_setup_phase2: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "auction_setup_phase2"})
            trans.rollback()
            print(f"[Migration] auction_setup_phase2: FAILED — {exc}")
            raise
        finally:
            conn.close()


def _create_auctions(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS auctions (
            id                              INTEGER PRIMARY KEY AUTOINCREMENT,
            season_id                       INTEGER NOT NULL UNIQUE REFERENCES seasons(id) ON DELETE CASCADE,
            budget_mode                     VARCHAR(20) DEFAULT 'uniform',
            uniform_budget                  BIGINT DEFAULT 0,
            bid_increment                   BIGINT DEFAULT 0,
            min_players_per_team            INTEGER DEFAULT 12,
            max_players_per_team            INTEGER DEFAULT 25,
            per_player_timer_seconds        INTEGER DEFAULT 20,
            draft_pick_timer_seconds        INTEGER DEFAULT 30,
            category_order_mode             VARCHAR(10) DEFAULT 'manual',
            category_order                  TEXT DEFAULT '[]',
            reauction_rounds                INTEGER DEFAULT 0,
            reauction_price_reduction_pct   INTEGER DEFAULT 0,
            current_round                   INTEGER DEFAULT 1,
            started_at                      DATETIME,
            ended_at                        DATETIME,
            created_at                      DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at                      DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auctions_season ON auctions(season_id)"))
    print("[Migration] auctions table ready.")


def _ensure_bid_increment_column(conn):
    """Existing DBs created before the flat-increment redesign have the legacy
    bid_increment_tiers TEXT column; add bid_increment BIGINT if missing. The
    legacy column is left orphaned (harmless, no ORM reference)."""
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(auctions)")).fetchall()}
    if "bid_increment" not in cols:
        conn.execute(text("ALTER TABLE auctions ADD COLUMN bid_increment BIGINT DEFAULT 0"))
        print("[Migration] auctions.bid_increment column added.")
    else:
        print("[Migration] auctions.bid_increment already present — skipped.")


def _create_auction_categories(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS auction_categories (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id          INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
            name                VARCHAR(100) NOT NULL,
            display_order       INTEGER DEFAULT 0,
            default_base_price  BIGINT,
            max_players         INTEGER DEFAULT 15,
            UNIQUE(auction_id, name)
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_categories_auction ON auction_categories(auction_id)"))
    print("[Migration] auction_categories table ready.")


def _ensure_max_players_column(conn):
    """Rename legacy max_per_team -> max_players (pool-size cap). Idempotent.

    Originally this column was modelled as a per-team quota; the redesigned
    semantics treat it as a hard cap on the total number of players that can
    be curated into a given category.
    """
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(auction_categories)")).fetchall()}
    if "max_players" in cols:
        print("[Migration] auction_categories.max_players already present — skipped.")
        return
    if "max_per_team" in cols:
        # SQLite >= 3.25 supports RENAME COLUMN.
        conn.execute(text("ALTER TABLE auction_categories RENAME COLUMN max_per_team TO max_players"))
        print("[Migration] auction_categories.max_per_team renamed to max_players.")
    else:
        conn.execute(text("ALTER TABLE auction_categories ADD COLUMN max_players INTEGER DEFAULT 15"))
        print("[Migration] auction_categories.max_players column added.")


def _create_auction_players(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS auction_players (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id              INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
            category_id             INTEGER NOT NULL REFERENCES auction_categories(id) ON DELETE CASCADE,
            master_player_id        INTEGER REFERENCES master_players(id) ON DELETE SET NULL,
            user_player_id          INTEGER REFERENCES user_players(id) ON DELETE SET NULL,
            name                    VARCHAR(100) NOT NULL,
            role                    VARCHAR(50),
            batting_rating          INTEGER DEFAULT 50,
            bowling_rating          INTEGER DEFAULT 50,
            fielding_rating         INTEGER DEFAULT 50,
            batting_hand            VARCHAR(20),
            bowling_type            VARCHAR(50),
            bowling_hand            VARCHAR(20),
            base_price_override     BIGINT,
            lot_order               INTEGER,
            status                  VARCHAR(20) DEFAULT 'upcoming',
            sold_to_season_team_id  INTEGER REFERENCES season_teams(id) ON DELETE SET NULL,
            sold_price              BIGINT,
            sold_in_round           INTEGER
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_players_auction ON auction_players(auction_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_players_category ON auction_players(category_id)"))
    print("[Migration] auction_players table ready.")


if __name__ == "__main__":
    print("=" * 60)
    print("AUCTION-REDESIGN Phase 2 — Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
