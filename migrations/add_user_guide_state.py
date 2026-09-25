"""
User Guide State Column Migration
=================================

Adds one additive column:

  users.guide_state  TEXT, nullable — JSON progress for the onboarding guide
                     (page tours seen, new-user journey status). NULL for every
                     existing row, which the guide reads as "nothing seen yet":
                     existing users get each page tour once, on their next visit.

Idempotent: detects the column via PRAGMA before adding.
"""

import os
import sys

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
            users_exists = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
            )).fetchone()
            if not users_exists:
                print("[Migration] add_user_guide_state: users table absent — skipping.")
                return

            if _column_exists(conn, "users", "guide_state"):
                print("[Migration] add_user_guide_state: already applied.")
                return

            conn.execute(text("ALTER TABLE users ADD COLUMN guide_state TEXT"))
            conn.commit()
            print("[Migration] add_user_guide_state: added users.guide_state.")
        except Exception as exc:
            log_exception(exc, source="sqlite",
                          context={"migration": "add_user_guide_state"})
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] add_user_guide_state: FAILED — {exc}")
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass
