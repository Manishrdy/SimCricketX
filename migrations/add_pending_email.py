"""
Pending Email Change Migration
================================

Adds pending_email, pending_email_token, and pending_email_token_expires
columns to the users table to support safe email changes with verification.

This migration is idempotent — safe to run multiple times.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text


def run_migration(db, app):
    """Apply pending email migration within the given app context."""
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            result = conn.execute(text("PRAGMA table_info(users)")).fetchall()
            col_names = [row[1] for row in result]

            if 'pending_email' not in col_names:
                conn.execute(text("ALTER TABLE users ADD COLUMN pending_email VARCHAR(120)"))
                print("[Migration] add_pending_email: added pending_email column.")
            if 'pending_email_token' not in col_names:
                conn.execute(text("ALTER TABLE users ADD COLUMN pending_email_token VARCHAR(64)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_pending_email_token ON users(pending_email_token)"))
                print("[Migration] add_pending_email: added pending_email_token column + index.")
            if 'pending_email_token_expires' not in col_names:
                conn.execute(text("ALTER TABLE users ADD COLUMN pending_email_token_expires DATETIME"))
                print("[Migration] add_pending_email: added pending_email_token_expires column.")

            trans.commit()
            print("[Migration] add_pending_email: completed successfully.")
        except Exception as exc:
            trans.rollback()
            print(f"[Migration] add_pending_email: FAILED — {exc}")
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
