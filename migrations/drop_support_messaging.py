"""
Drop Support Messaging Tables
=============================

The 1:1 support chat was replaced by the community board (private posts cover
what used to be private chats). The owner chose to discard the old chat
history rather than migrate it, so this drops the three tables outright.

Order matters: read_state references support_message, and both reference
support_conversation. Safe to run multiple times.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception

TABLES = ("support_conversation_read_state", "support_message", "support_conversation")


def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            for table in TABLES:
                exists = conn.execute(text(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": table}).fetchone()
                if exists:
                    conn.execute(text(f"DROP TABLE {table}"))
                    print(f"[Migration] drop_support_messaging: dropped {table}.")
            trans.commit()
        except Exception as exc:
            trans.rollback()
            log_exception(exc, source="sqlite", context={"migration": "drop_support_messaging"})
            print(f"[Migration] drop_support_messaging: FAILED - {exc}")
            raise
        finally:
            conn.close()
