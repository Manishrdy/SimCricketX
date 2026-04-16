"""
AUCTION-REDESIGN — Phase 8 Migration
=====================================

Creates persistence for auction history and moderation trails:
    auction_bids        — append-only log of every accepted bid
    auction_audit_logs  — organizer actions + key system events

Idempotent. No changes to pre-existing tables.
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
            _create_auction_bids(conn)
            _create_auction_audit_logs(conn)
            trans.commit()
            print("[Migration] auction_history_phase8: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "auction_history_phase8"})
            trans.rollback()
            print(f"[Migration] auction_history_phase8: FAILED — {exc}")
            raise
        finally:
            conn.close()


def _create_auction_bids(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS auction_bids (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id          INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
            auction_player_id   INTEGER REFERENCES auction_players(id) ON DELETE SET NULL,
            season_team_id      INTEGER REFERENCES season_teams(id) ON DELETE SET NULL,
            amount              BIGINT NOT NULL,
            round               INTEGER NOT NULL DEFAULT 1,
            created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_bid_auction ON auction_bids(auction_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_bid_player ON auction_bids(auction_player_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_bid_team ON auction_bids(season_team_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_bid_created ON auction_bids(created_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_bid_auction_created ON auction_bids(auction_id, created_at)"))
    print("[Migration] auction_bids table ready.")


def _create_auction_audit_logs(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS auction_audit_logs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id   INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
            action       VARCHAR(50) NOT NULL,
            actor_type   VARCHAR(20) NOT NULL DEFAULT 'system',
            actor_label  VARCHAR(120),
            payload      TEXT,
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_audit_auction ON auction_audit_logs(auction_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_audit_created ON auction_audit_logs(created_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_audit_auction_created ON auction_audit_logs(auction_id, created_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_audit_action ON auction_audit_logs(action)"))
    print("[Migration] auction_audit_logs table ready.")


if __name__ == "__main__":
    print("=" * 60)
    print("AUCTION-REDESIGN Phase 8 — History Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
