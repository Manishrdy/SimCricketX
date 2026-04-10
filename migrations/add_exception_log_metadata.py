"""
Exception Log Metadata Migration
===============================

Adds operational metadata columns and indexes to exception_log.
Idempotent and safe to run multiple times.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception


def run_migration(db, app):
    """Apply exception_log metadata migration within the given app context."""
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            table_exists = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='exception_log'"
            )).fetchone()

            if table_exists is None:
                print("[Migration] add_exception_log_metadata: exception_log not found, skipping.")
                trans.commit()
                return

            existing_cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(exception_log)")).fetchall()
            }
            desired_cols = {
                "severity": "VARCHAR(10) NOT NULL DEFAULT 'error'",
                "source": "VARCHAR(30) NOT NULL DEFAULT 'backend'",
                "context_json": "TEXT",
                "request_id": "VARCHAR(64)",
                "handled": "BOOLEAN NOT NULL DEFAULT 1",
                "resolved": "BOOLEAN NOT NULL DEFAULT 0",
                "resolved_at": "DATETIME",
                "resolved_by": "VARCHAR(120)",
            }

            for col_name, col_sql in desired_cols.items():
                if col_name not in existing_cols:
                    conn.execute(text(f"ALTER TABLE exception_log ADD COLUMN {col_name} {col_sql}"))
                    print(f"[Migration] add_exception_log_metadata: added column {col_name}.")

            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_exception_timestamp ON exception_log(timestamp)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_exception_type_ts ON exception_log(exception_type, timestamp)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_exception_source_ts ON exception_log(source, timestamp)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_exception_resolved_ts ON exception_log(resolved, timestamp)"
            ))

            trans.commit()
            print("[Migration] add_exception_log_metadata: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_exception_log_metadata"})
            trans.rollback()
            print(f"[Migration] add_exception_log_metadata: FAILED — {exc}")
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
