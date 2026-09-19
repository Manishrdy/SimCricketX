"""
Team Updated-At Column Migration
================================

Adds one additive column:

  teams.updated_at  DATETIME, nullable — the UTC timestamp of the last edit
                    to the team (identity edit, squad change, publish). NULL
                    for teams that have not been touched since creation, and
                    NULL for every pre-existing row (no backfill: we cannot
                    invent an edit time we never recorded, and backfilling it
                    to created_at would make /teams/manage claim every team
                    was "last updated" when none of them were).

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
            teams_exists = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='teams'"
            )).fetchone()
            if not teams_exists:
                print("[Migration] add_team_updated_at: teams table absent — skipping.")
                return

            added = _add_column_if_missing(
                conn, "teams", "updated_at", "updated_at DATETIME"
            )

            conn.commit()
            if added:
                print("[Migration] add_team_updated_at: added teams.updated_at.")
            else:
                print("[Migration] add_team_updated_at: already applied.")
        except Exception as exc:
            log_exception(exc, source="sqlite",
                          context={"migration": "add_team_updated_at"})
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] add_team_updated_at: FAILED — {exc}")
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    print("=" * 60)
    print("Team Updated-At Column - Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
