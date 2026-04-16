"""
AUCTION-REDESIGN — Phase 5 Migration
=====================================

Creates the draft-pick table used by the snake-order draft auction mode:
    draft_picks — one row per pick slot (pending / picked / missed)

Idempotent. No schema changes to pre-existing tables; traditional auctions
continue to work untouched.
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
            _create_draft_picks(conn)
            trans.commit()
            print("[Migration] auction_draft_phase5: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "auction_draft_phase5"})
            trans.rollback()
            print(f"[Migration] auction_draft_phase5: FAILED — {exc}")
            raise
        finally:
            conn.close()


def _create_draft_picks(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS draft_picks (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id            INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
            round                 INTEGER NOT NULL,
            pick_order_in_round   INTEGER NOT NULL,
            season_team_id        INTEGER NOT NULL REFERENCES season_teams(id) ON DELETE CASCADE,
            auction_player_id     INTEGER REFERENCES auction_players(id) ON DELETE SET NULL,
            category_id           INTEGER NOT NULL REFERENCES auction_categories(id) ON DELETE CASCADE,
            is_carryover          BOOLEAN NOT NULL DEFAULT 0,
            carryover_from_round  INTEGER,
            picked_at             DATETIME,
            status                VARCHAR(20) NOT NULL DEFAULT 'pending',
            created_at            DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_draft_picks_auction_round ON draft_picks(auction_id, round)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_draft_picks_queue ON draft_picks(auction_id, status, round, pick_order_in_round)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_draft_picks_team ON draft_picks(season_team_id)"))
    print("[Migration] draft_picks table ready.")


if __name__ == "__main__":
    print("=" * 60)
    print("AUCTION-REDESIGN Phase 5 — Draft Picks Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
