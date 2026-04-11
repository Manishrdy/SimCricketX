"""
Issue Webhook Events Table Migration
====================================

Creates the `issue_webhook_event` table used by the inbound GitHub
webhook handler. Provides idempotency (`delivery_id` unique) and an
audit trail of every received delivery.

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
            existing = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='issue_webhook_event'"
            )).fetchone()

            if existing is None:
                conn.execute(text(
                    """
                    CREATE TABLE issue_webhook_event (
                        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                        delivery_id         VARCHAR(100) NOT NULL UNIQUE,
                        event_type          VARCHAR(50),
                        action              VARCHAR(50),
                        github_issue_number INTEGER,
                        payload_json        TEXT,
                        signature_valid     BOOLEAN NOT NULL DEFAULT 0,
                        processed           BOOLEAN NOT NULL DEFAULT 0,
                        processing_error    TEXT,
                        received_at         DATETIME NOT NULL
                    )
                    """
                ))
                print("[Migration] add_issue_webhook_events: created table issue_webhook_event.")
            else:
                # Idempotent column add for any future expansion.
                existing_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(issue_webhook_event)")).fetchall()}
                desired_cols = {
                    "delivery_id":         "VARCHAR(100)",
                    "event_type":          "VARCHAR(50)",
                    "action":              "VARCHAR(50)",
                    "github_issue_number": "INTEGER",
                    "payload_json":        "TEXT",
                    "signature_valid":     "BOOLEAN NOT NULL DEFAULT 0",
                    "processed":           "BOOLEAN NOT NULL DEFAULT 0",
                    "processing_error":    "TEXT",
                    "received_at":         "DATETIME",
                }
                for col_name, col_sql in desired_cols.items():
                    if col_name not in existing_cols:
                        conn.execute(text(f"ALTER TABLE issue_webhook_event ADD COLUMN {col_name} {col_sql}"))
                        print(f"[Migration] add_issue_webhook_events: added column {col_name}.")

            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_issue_webhook_event_delivery_id ON issue_webhook_event(delivery_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_issue_webhook_event_issue_number ON issue_webhook_event(github_issue_number)"
            ))

            trans.commit()
            print("[Migration] add_issue_webhook_events: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_issue_webhook_events"})
            trans.rollback()
            print(f"[Migration] add_issue_webhook_events: FAILED — {exc}")
            raise
        finally:
            conn.close()
