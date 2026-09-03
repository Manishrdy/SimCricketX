"""Add compact completed-match weather summary columns.

The detailed FC event ledger remains in the generated archive; these fields
are enough for the completed scorecard banner and match-history consumers.
The migration is additive and idempotent.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception


def _columns(conn):
    return {row[1] for row in conn.execute(text("PRAGMA table_info(matches)"))}


def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        try:
            existing = _columns(conn)
            additions = (
                ("weather_forecast", "weather_forecast VARCHAR(20)"),
                ("weather_affected", "weather_affected BOOLEAN DEFAULT 0"),
                ("weather_minutes_lost", "weather_minutes_lost INTEGER DEFAULT 0"),
                ("weather_overs_lost", "weather_overs_lost INTEGER DEFAULT 0"),
            )
            added = []
            for name, ddl in additions:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE matches ADD COLUMN {ddl}"))
                    added.append(name)
            conn.commit()
            print("[Migration] add_match_weather_summary: " + (
                f"added {', '.join(added)}" if added else "already applied"))
        except Exception as exc:
            log_exception(exc, source="sqlite", context={
                "migration": "add_match_weather_summary"})
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    from database import db
    from app import create_app

    run_migration(db, create_app())
