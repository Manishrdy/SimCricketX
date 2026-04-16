"""
AUCTION-REDESIGN — Phase 4 Migration
=====================================

Adds live-runtime columns to the `auctions` table:
    live_player_id          → FK auction_players.id ON DELETE SET NULL
    lot_ends_at             → DATETIME, server-authoritative lot expiry
    lot_paused_remaining_ms → INTEGER, set while paused (null otherwise)

Idempotent: safe to re-run. Bid history is intentionally NOT persisted
in MVP — only the current highest bid lives in process memory.
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
            _ensure_live_runtime_columns(conn)
            trans.commit()
            print("[Migration] auction_live_phase4: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "auction_live_phase4"})
            trans.rollback()
            print(f"[Migration] auction_live_phase4: FAILED — {exc}")
            raise
        finally:
            conn.close()


def _ensure_live_runtime_columns(conn):
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(auctions)")).fetchall()}

    if "live_player_id" not in cols:
        # SQLite cannot ADD COLUMN with an inline REFERENCES against an existing
        # table that already has rows in some pragma modes; we add the bare
        # column and rely on the application-level guard. ON DELETE SET NULL
        # behaviour is provided by the runtime which clears the field whenever
        # an auction_player is deleted (rare; happens only during setup).
        conn.execute(text("ALTER TABLE auctions ADD COLUMN live_player_id INTEGER"))
        print("[Migration] auctions.live_player_id column added.")
    else:
        print("[Migration] auctions.live_player_id already present — skipped.")

    if "lot_ends_at" not in cols:
        conn.execute(text("ALTER TABLE auctions ADD COLUMN lot_ends_at DATETIME"))
        print("[Migration] auctions.lot_ends_at column added.")
    else:
        print("[Migration] auctions.lot_ends_at already present — skipped.")

    if "lot_paused_remaining_ms" not in cols:
        conn.execute(text("ALTER TABLE auctions ADD COLUMN lot_paused_remaining_ms INTEGER"))
        print("[Migration] auctions.lot_paused_remaining_ms column added.")
    else:
        print("[Migration] auctions.lot_paused_remaining_ms already present — skipped.")


if __name__ == "__main__":
    print("=" * 60)
    print("AUCTION-REDESIGN Phase 4 — Live Runtime Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
