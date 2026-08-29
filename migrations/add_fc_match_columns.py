"""
First-Class Match Columns Migration
====================================

Adds the columns needed for First-Class (FC) matches — 4/5-day, up to
2 innings per side — on top of the existing single-innings-per-team
`matches` schema:

  matches.days                       INTEGER, nullable — 4 or 5; NULL for
                                      non-FC matches.
  matches.follow_on_enforced         BOOLEAN, nullable — display/history only.
  matches.home_team_score_innings2   INTEGER, nullable
  matches.home_team_wickets_innings2 INTEGER, nullable
  matches.home_team_overs_innings2   VARCHAR(10), nullable
  matches.away_team_score_innings2   INTEGER, nullable
  matches.away_team_wickets_innings2 INTEGER, nullable
  matches.away_team_overs_innings2   VARCHAR(10), nullable

The existing `home_team_score`/`away_team_score`/`*_overs` columns are
reused as "innings 1 for that side"; the new `_innings2` columns hold a
side's second innings when one occurs (they stay NULL for an innings-and-N
win, a draw before the 4th innings starts, or any non-FC match). No
backfill: pre-existing rows keep every new column NULL, which is exactly
what they implicitly had before FC existed.

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


_NEW_COLUMNS = [
    ("days", "days INTEGER"),
    ("follow_on_enforced", "follow_on_enforced BOOLEAN"),
    ("home_team_score_innings2", "home_team_score_innings2 INTEGER"),
    ("home_team_wickets_innings2", "home_team_wickets_innings2 INTEGER"),
    ("home_team_overs_innings2", "home_team_overs_innings2 VARCHAR(10)"),
    ("away_team_score_innings2", "away_team_score_innings2 INTEGER"),
    ("away_team_wickets_innings2", "away_team_wickets_innings2 INTEGER"),
    ("away_team_overs_innings2", "away_team_overs_innings2 VARCHAR(10)"),
]


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
                conn.commit()
                print("[Migration] add_fc_match_columns: matches table absent — nothing to do.")
                return

            added = []
            for column, ddl in _NEW_COLUMNS:
                if _add_column_if_missing(conn, "matches", column, ddl):
                    added.append(f"matches.{column}")

            conn.commit()
            if added:
                print(f"[Migration] add_fc_match_columns: added {', '.join(added)}.")
            else:
                print("[Migration] add_fc_match_columns: already applied.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_fc_match_columns"})
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] add_fc_match_columns: FAILED — {exc}")
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    print("=" * 60)
    print("First-Class Match Columns - Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
