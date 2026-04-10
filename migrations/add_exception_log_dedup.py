"""
Exception Log Deduplication Migration
====================================

Adds fingerprinting and occurrence tracking columns to exception_log.
Safe to run multiple times.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception


def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            table_exists = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='exception_log'"
            )).fetchone()
            if table_exists is None:
                trans.commit()
                print("[Migration] add_exception_log_dedup: exception_log not found, skipping.")
                return

            existing_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(exception_log)")).fetchall()}
            desired_cols = {
                "fingerprint": "VARCHAR(64)",
                "occurrence_count": "INTEGER NOT NULL DEFAULT 1",
                "first_seen_at": "DATETIME",
                "last_seen_at": "DATETIME",
                "github_issue_number": "INTEGER",
                "github_issue_url": "VARCHAR(300)",
            }

            for col_name, col_sql in desired_cols.items():
                if col_name not in existing_cols:
                    conn.execute(text(f"ALTER TABLE exception_log ADD COLUMN {col_name} {col_sql}"))
                    print(f"[Migration] add_exception_log_dedup: added column {col_name}.")

            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_exception_fingerprint ON exception_log(fingerprint)"))
            trans.commit()
            print("[Migration] add_exception_log_dedup: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_exception_log_dedup"})
            trans.rollback()
            print(f"[Migration] add_exception_log_dedup: FAILED — {exc}")
            raise
        finally:
            conn.close()

