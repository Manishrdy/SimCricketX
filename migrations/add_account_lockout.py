"""
Account Lockout Migration
==========================

Adds lockout_until, lockout_count, and lockout_window_start columns to the
users table to support account lockout after N failed login attempts.

This migration is idempotent — safe to run multiple times.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text


def run_migration(db, app):
    """Apply account lockout migration within the given app context."""
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            result = conn.execute(text("PRAGMA table_info(users)")).fetchall()
            col_names = [row[1] for row in result]

            if 'lockout_until' not in col_names:
                conn.execute(text("ALTER TABLE users ADD COLUMN lockout_until DATETIME"))
                print("[Migration] add_account_lockout: added lockout_until column.")
            if 'lockout_count' not in col_names:
                conn.execute(text("ALTER TABLE users ADD COLUMN lockout_count INTEGER NOT NULL DEFAULT 0"))
                print("[Migration] add_account_lockout: added lockout_count column.")
            if 'lockout_window_start' not in col_names:
                conn.execute(text("ALTER TABLE users ADD COLUMN lockout_window_start DATETIME"))
                print("[Migration] add_account_lockout: added lockout_window_start column.")

            trans.commit()
            print("[Migration] add_account_lockout: completed successfully.")
        except Exception as exc:
            trans.rollback()
            print(f"[Migration] add_account_lockout: FAILED — {exc}")
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
