"""
Team Logos Migration
====================

Additive only:

  team_logos          one optional row per team holding the normalised
                      256x256 PNG (see utils/team_logo.py). Kept out of the
                      teams table so team lists never drag the blob along.
  teams.logo_version  INTEGER, nullable. NULL means "no logo"; otherwise it
                      is bumped on every change and used as the ?v= cache
                      buster on the logo URL. Lets lists know whether a logo
                      exists without joining team_logos.

No backfill: existing teams simply have no logo.

Idempotent: CREATE TABLE IF NOT EXISTS, and the column is detected via
PRAGMA before being added.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception


def _column_exists(conn, table, column):
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


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
                print("[Migration] add_team_logos: teams table absent — skipping.")
                return

            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS team_logos ("
                " team_id INTEGER NOT NULL PRIMARY KEY"
                "   REFERENCES teams(id) ON DELETE CASCADE,"
                " png BLOB NOT NULL,"
                " byte_size INTEGER NOT NULL,"
                " sha256 VARCHAR(64) NOT NULL,"
                " updated_at DATETIME NOT NULL"
                ")"
            ))
            added = False
            if not _column_exists(conn, "teams", "logo_version"):
                conn.execute(text("ALTER TABLE teams ADD COLUMN logo_version INTEGER"))
                added = True

            conn.commit()
            if added:
                print("[Migration] add_team_logos: created team_logos, added teams.logo_version.")
            else:
                print("[Migration] add_team_logos: already applied.")
        except Exception as exc:
            log_exception(exc, source="sqlite",
                          context={"migration": "add_team_logos"})
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] add_team_logos: FAILED — {exc}")
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
