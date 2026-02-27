"""
Tournament Format Migration
============================

Adds the format_type column to the tournaments table so each tournament
can specify a single cricket format (T20, ListA) for all its matches.

This migration is safe to run multiple times (idempotent). It is called
automatically at app startup by app.py.

Manual usage (optional):
    python migrations/add_tournament_format.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text


def run_migration(db, app):
    """
    Apply tournament-format migration within the given app context.

    Adds format_type VARCHAR(20) NOT NULL DEFAULT 'T20' to the tournaments table.
    Existing tournaments are safely defaulted to T20.
    """
    with app.app_context():
        with db.engine.connect() as conn:
            # Check if column already exists (idempotency guard)
            result = conn.execute(text("PRAGMA table_info(tournaments)"))
            existing_cols = [row[1] for row in result]

            if 'format_type' not in existing_cols:
                conn.execute(text(
                    "ALTER TABLE tournaments "
                    "ADD COLUMN format_type VARCHAR(20) NOT NULL DEFAULT 'T20'"
                ))
                conn.commit()
                print("[Migration] Added format_type column to tournaments table.")
            else:
                print("[Migration] format_type column already exists in tournaments. Skipping.")


if __name__ == "__main__":
    # Allow running directly: python migrations/add_tournament_format.py
    from app import app, db as _db
    run_migration(_db, app)
    print("Tournament format migration complete.")
