"""
Exception Log GitHub Sync Tracking Migration
============================================

Adds columns used by services/github_issue_queue.py to track the async
push of exceptions to GitHub Issues:

  - github_sync_status   ('pending' / 'synced' / 'failed')
  - github_sync_error    (last error message if a push failed)
  - github_last_synced_at (timestamp of the most recent push attempt)

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
                print("[Migration] add_exception_log_github_sync: exception_log not found, skipping.")
                return

            existing_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(exception_log)")).fetchall()}
            desired_cols = {
                "github_sync_status": "VARCHAR(20)",
                "github_sync_error": "TEXT",
                "github_last_synced_at": "DATETIME",
            }

            for col_name, col_sql in desired_cols.items():
                if col_name not in existing_cols:
                    conn.execute(text(f"ALTER TABLE exception_log ADD COLUMN {col_name} {col_sql}"))
                    print(f"[Migration] add_exception_log_github_sync: added column {col_name}.")

            trans.commit()
            print("[Migration] add_exception_log_github_sync: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_exception_log_github_sync"})
            trans.rollback()
            print(f"[Migration] add_exception_log_github_sync: FAILED — {exc}")
            raise
        finally:
            conn.close()
