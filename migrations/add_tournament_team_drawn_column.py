"""
Tournament Team Drawn Column Migration
=======================================

Adds the column needed to track First-Class (FC) draw outcomes separately
from ties in tournament standings:

  tournament_teams.drawn   INTEGER, default 0 — count of drawn matches.
                            Always 0 for T20/ListA teams (those formats
                            have no draw outcome).

Before this migration, a drawn FC match had no home in the standings
schema and was silently counted as a tie by the standings-update logic.

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
            table_exists = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tournament_teams'"
            )).fetchone()
            if not table_exists:
                conn.commit()
                print("[Migration] add_tournament_team_drawn_column: tournament_teams table absent — nothing to do.")
                return

            added = _add_column_if_missing(
                conn, "tournament_teams", "drawn", "drawn INTEGER DEFAULT 0"
            )

            conn.commit()
            if added:
                print("[Migration] add_tournament_team_drawn_column: added tournament_teams.drawn.")
            else:
                print("[Migration] add_tournament_team_drawn_column: already applied.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_tournament_team_drawn_column"})
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] add_tournament_team_drawn_column: FAILED — {exc}")
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    print("=" * 60)
    print("Tournament Team Drawn Column - Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
