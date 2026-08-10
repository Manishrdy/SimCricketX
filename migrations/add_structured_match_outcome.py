"""
Structured Match Outcome + Byes/Leg-Byes Columns Migration
============================================================

Adds four additive columns used to stop deriving match/scorecard facts by
parsing prose or subtracting known quantities from a total:

  matches.match_status      VARCHAR(20), nullable — 'completed' | 'tied' |
                             'no_result' | 'aborted'. Set by the engine at
                             the moment the outcome is decided, instead of
                             being re-inferred later from result_description
                             text (keyword search) at every consumer.
  matches.stats_incomplete  BOOLEAN DEFAULT 0 — set true when the archiver
                             could not resolve a player for at least one
                             scorecard row and had to skip it, so the gap is
                             visible instead of silent.
  match_scorecards.byes     INTEGER DEFAULT 0 — tracked live by the engine
                             per bowler but previously had no column to
                             persist to; view_scoreboard had to guess it as
                             score - batting_runs - wides - noballs.
  match_scorecards.leg_byes INTEGER DEFAULT 0 — same as byes.

No backfill: pre-existing rows keep match_status = NULL (readers must treat
NULL as "unknown, fall back to legacy inference") and byes/leg_byes = 0
(same value they implicitly had before this column existed).

Idempotent: detects each column via PRAGMA before adding.
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
            added = []

            matches_exists = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='matches'"
            )).fetchone()
            if matches_exists:
                if _add_column_if_missing(conn, "matches", "match_status", "match_status VARCHAR(20)"):
                    added.append("matches.match_status")
                if _add_column_if_missing(conn, "matches", "stats_incomplete", "stats_incomplete BOOLEAN DEFAULT 0"):
                    added.append("matches.stats_incomplete")
            else:
                print("[Migration] add_structured_match_outcome: matches table absent — skipping its columns.")

            scorecards_exists = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='match_scorecards'"
            )).fetchone()
            if scorecards_exists:
                if _add_column_if_missing(conn, "match_scorecards", "byes", "byes INTEGER DEFAULT 0"):
                    added.append("match_scorecards.byes")
                if _add_column_if_missing(conn, "match_scorecards", "leg_byes", "leg_byes INTEGER DEFAULT 0"):
                    added.append("match_scorecards.leg_byes")
            else:
                print("[Migration] add_structured_match_outcome: match_scorecards table absent — skipping its columns.")

            conn.commit()
            if added:
                print(f"[Migration] add_structured_match_outcome: added {', '.join(added)}.")
            else:
                print("[Migration] add_structured_match_outcome: already applied.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_structured_match_outcome"})
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] add_structured_match_outcome: FAILED — {exc}")
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    print("=" * 60)
    print("Structured Match Outcome + Byes/Leg-Byes - Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
