"""
Man of the Match Column Migration
==================================

Adds one additive column:

  matches.motm_player_id  INTEGER, nullable — the player.id chosen as
                           Man of the Match for this match, set once at
                           archive time by engine/motm_service.py. NULL for
                           pre-existing matches (no backfill) and for
                           no_result/aborted matches (nothing to award).

Idempotent: detects the column via PRAGMA before adding.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception


def _column_exists(conn, table, column):
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    # row: (cid, name, type, notnull, dflt_value, pk)
    return any(row[1] == column for row in rows)


def _add_column_if_missing(conn, table, column, ddl):
    if not _column_exists(conn, table, column):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
        return True
    return False


def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        try:
            conn.rollback()
        except Exception:
            pass

        try:
            matches_exists = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='matches'"
            )).fetchone()
            if not matches_exists:
                print("[Migration] add_motm_column: matches table absent — skipping.")
                return

            added = _add_column_if_missing(
                conn, "matches", "motm_player_id", "motm_player_id INTEGER"
            )

            conn.commit()
            if added:
                print("[Migration] add_motm_column: added matches.motm_player_id.")
            else:
                print("[Migration] add_motm_column: already applied.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_motm_column"})
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] add_motm_column: FAILED — {exc}")
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    print("=" * 60)
    print("Man of the Match Column - Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
