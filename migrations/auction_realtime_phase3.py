"""
AUCTION-REDESIGN — Phase 3 Migration
=====================================

Adds the realtime-foundation table:
    auction_chat_messages — flat per-auction chat room with soft-delete.

Idempotent. Presence state is in-memory (not persisted), so no table for it.
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
            _create_chat(conn)
            trans.commit()
            print("[Migration] auction_realtime_phase3: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "auction_realtime_phase3"})
            trans.rollback()
            print(f"[Migration] auction_realtime_phase3: FAILED — {exc}")
            raise
        finally:
            conn.close()


def _create_chat(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS auction_chat_messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id      INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
            sender_type     VARCHAR(20) NOT NULL,
            season_team_id  INTEGER REFERENCES season_teams(id) ON DELETE SET NULL,
            sender_label    VARCHAR(100) NOT NULL,
            body            TEXT NOT NULL,
            deleted_at      DATETIME,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_chat_auction ON auction_chat_messages(auction_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_chat_season_team ON auction_chat_messages(season_team_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auction_chat_created ON auction_chat_messages(created_at)"))
    print("[Migration] auction_chat_messages table ready.")


if __name__ == "__main__":
    print("=" * 60)
    print("AUCTION-REDESIGN Phase 3 — Chat Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
