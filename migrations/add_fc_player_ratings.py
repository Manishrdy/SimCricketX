"""
First-Class Player Rating Columns Migration
=============================================

Adds three additive rating columns to `players`, used by the First-Class
(FC) engine — scoped per-profile like every existing rating, so a player's
FC row is independent of their T20/ListA row for the same real person:

  players.technique_rating   INTEGER DEFAULT 0 — long-innings survival /
                              defensive technique.
  players.temperament_rating INTEGER DEFAULT 0 — resistance to session-long
                              pressure.
  players.stamina_rating     INTEGER DEFAULT 0 — bowling workload capacity;
                              feeds the FC bowler-fatigue slope.

These are inert for T20/ListA profiles (default 0, not read by those
engines) and are only meaningful on a player's FC-format profile row.

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
    ("technique_rating", "technique_rating INTEGER DEFAULT 0"),
    ("temperament_rating", "temperament_rating INTEGER DEFAULT 0"),
    ("stamina_rating", "stamina_rating INTEGER DEFAULT 0"),
]


def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        try:
            conn.rollback()
        except Exception:
            pass

        try:
            players_exists = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='players'"
            )).fetchone()
            if not players_exists:
                conn.commit()
                print("[Migration] add_fc_player_ratings: players table absent — nothing to do.")
                return

            added = []
            for column, ddl in _NEW_COLUMNS:
                if _add_column_if_missing(conn, "players", column, ddl):
                    added.append(f"players.{column}")

            conn.commit()
            if added:
                print(f"[Migration] add_fc_player_ratings: added {', '.join(added)}.")
            else:
                print("[Migration] add_fc_player_ratings: already applied.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_fc_player_ratings"})
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] add_fc_player_ratings: FAILED — {exc}")
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    print("=" * 60)
    print("First-Class Player Rating Columns - Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
