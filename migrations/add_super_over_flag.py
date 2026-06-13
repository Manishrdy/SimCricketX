"""
MatchScorecard `is_super_over` Column Migration
===============================================

Adds an `is_super_over BOOLEAN DEFAULT 0` column to `match_scorecards` and
backfills it from the legacy convention (`innings_number > 2`).

Super-over career-stat rows are written at innings_number=3 so career totals
aggregate/reverse through the standard paths. Before this migration the ONLY
discriminator was that magic innings number, and every stats consumer had to
independently remember to exclude those rows (most didn't — they were counted
as real innings, inflating innings/dismissal counts and averages). The
explicit flag lets consumers filter with
`MatchScorecard.is_super_over.isnot(True)`.

Idempotent: detects the column via PRAGMA before adding; the backfill UPDATE
only touches rows whose flag is still unset, so re-runs are no-ops.
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
                print("[Migration] add_super_over_flag: match_scorecards table absent — nothing to do.")
                return

            already_applied = _column_exists(conn, "match_scorecards", "is_super_over")
            if not already_applied:
                conn.execute(text(
                    "ALTER TABLE match_scorecards ADD COLUMN is_super_over BOOLEAN DEFAULT 0"
                ))
            # Backfill rows written before the flag existed (legacy convention:
            # super-over rows live at innings_number 3). Idempotent — safe to
            # re-run even when the column was already present (e.g. added by a
            # schema guard without the backfill).
            conn.execute(text(
                "UPDATE match_scorecards SET is_super_over = 1 "
                "WHERE innings_number > 2 AND (is_super_over IS NULL OR is_super_over = 0)"
            ))
            conn.commit()
            print(
                "[Migration] add_super_over_flag: "
                + ("backfill re-checked (column already present)." if already_applied
                   else "completed successfully.")
            )
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_super_over_flag"})
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] add_super_over_flag: FAILED — {exc}")
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    print("=" * 60)
    print("MatchScorecard is_super_over Flag - Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
