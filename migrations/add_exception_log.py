"""
Exception Log Migration
========================

Creates the exception_log table for structured error tracking.
This migration is idempotent — safe to run multiple times.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception


def run_migration(db, app):
    """Apply exception_log migration within the given app context."""
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            result = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='exception_log'"
            )).fetchone()

            if result is None:
                conn.execute(text("""
                    CREATE TABLE exception_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        exception_type VARCHAR(200) NOT NULL,
                        exception_message TEXT NOT NULL DEFAULT '',
                        traceback TEXT,
                        module VARCHAR(200),
                        function VARCHAR(200),
                        line_number INTEGER,
                        filename VARCHAR(300),
                        user_email VARCHAR(120),
                        timestamp DATETIME NOT NULL
                    )
                """))
                print("[Migration] add_exception_log: created exception_log table.")
            else:
                print("[Migration] add_exception_log: table already exists, skipping.")

            trans.commit()
            print("[Migration] add_exception_log: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_exception_log"})
            trans.rollback()
            print(f"[Migration] add_exception_log: FAILED — {exc}")
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
