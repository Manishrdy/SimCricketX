"""
User Support-Prompt Columns Migration
=====================================

Adds four additive, nullable DATETIME columns to ``users``:

  support_prompt_shown_at       when the dashboard support popup was displayed
  support_prompt_dismissed_at   when the user closed it
  support_prompt_clicked_at     when the user followed the support link
  support_prompt_optout_at      when the user chose "Don't ask again"

NULL for every existing row, which reads as "never asked" — no backfill. The
cooldown rules live in utils/support_prompt.py.

Idempotent: each column is detected via PRAGMA before it is added.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception

COLUMNS = (
    "support_prompt_shown_at",
    "support_prompt_dismissed_at",
    "support_prompt_clicked_at",
    "support_prompt_optout_at",
)


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
            users_exists = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
            )).fetchone()
            if not users_exists:
                print("[Migration] add_user_support_prompt: users table absent — skipping.")
                return

            added = []
            for column in COLUMNS:
                if not _column_exists(conn, "users", column):
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN {column} DATETIME"))
                    added.append(column)

            conn.commit()
            if added:
                print(f"[Migration] add_user_support_prompt: added {', '.join(added)}.")
            else:
                print("[Migration] add_user_support_prompt: already applied.")
        except Exception as exc:
            log_exception(exc, source="sqlite",
                          context={"migration": "add_user_support_prompt"})
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] add_user_support_prompt: FAILED — {exc}")
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    print("=" * 60)
    print("User Support-Prompt Columns - Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
