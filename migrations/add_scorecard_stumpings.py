"""
MatchScorecard `stumpings` Column Migration
==========================================

Adds a nullable `stumpings INTEGER DEFAULT 0` column to `match_scorecards`
so wicketkeeper stumpings can be attributed alongside catches and run-outs.

Before this migration, `_save_fielding_stats` silently dropped any wicket
with `wicket_type == "Stumped"` because there was no column to write to.

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


def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        try:
            conn.rollback()
        except Exception:
            pass

        try:
            exists = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='match_scorecards'"
            )).fetchone()
            if not exists:
                conn.commit()
                print("[Migration] add_scorecard_stumpings: match_scorecards table absent — nothing to do.")
                return

            if _column_exists(conn, "match_scorecards", "stumpings"):
                conn.commit()
                print("[Migration] add_scorecard_stumpings: already applied.")
                return

            conn.execute(text(
                "ALTER TABLE match_scorecards ADD COLUMN stumpings INTEGER DEFAULT 0"
            ))
            conn.commit()
            print("[Migration] add_scorecard_stumpings: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_scorecard_stumpings"})
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] add_scorecard_stumpings: FAILED — {exc}")
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    print("=" * 60)
    print("MatchScorecard Stumpings - Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
