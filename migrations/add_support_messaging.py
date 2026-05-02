"""
Support Messaging Tables
========================

Creates the DB-backed in-app support chat tables that replace manual
user-report-to-GitHub submission. Safe to run multiple times.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception


def _table_exists(conn, name: str) -> bool:
    return conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=:name"
    ), {"name": name}).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}


def _add_missing_columns(conn, table: str, desired: dict[str, str]) -> None:
    existing = _columns(conn, table)
    for col_name, col_sql in desired.items():
        if col_name not in existing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_sql}"))
            print(f"[Migration] add_support_messaging: added {table}.{col_name}.")


def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            if not _table_exists(conn, "support_conversation"):
                conn.execute(text(
                    """
                    CREATE TABLE support_conversation (
                        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                        public_id             VARCHAR(16) NOT NULL UNIQUE,
                        user_id               VARCHAR(120) NOT NULL,
                        assigned_admin_id     VARCHAR(120),
                        status                VARCHAR(20) NOT NULL DEFAULT 'open',
                        priority              VARCHAR(20) NOT NULL DEFAULT 'normal',
                        subject               VARCHAR(200),
                        source_page_url       VARCHAR(500),
                        app_version           VARCHAR(50),
                        user_agent            VARCHAR(500),
                        last_message_at       DATETIME,
                        last_user_message_at  DATETIME,
                        last_admin_message_at DATETIME,
                        retention_eligible_at DATETIME,
                        hard_delete_at        DATETIME,
                        created_at            DATETIME NOT NULL,
                        updated_at            DATETIME NOT NULL,
                        closed_at             DATETIME,
                        closed_by             VARCHAR(120),
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                        FOREIGN KEY(assigned_admin_id) REFERENCES users(id) ON DELETE SET NULL
                    )
                    """
                ))
                print("[Migration] add_support_messaging: created support_conversation.")
            else:
                _add_missing_columns(conn, "support_conversation", {
                    "public_id": "VARCHAR(16)",
                    "user_id": "VARCHAR(120)",
                    "assigned_admin_id": "VARCHAR(120)",
                    "status": "VARCHAR(20) NOT NULL DEFAULT 'open'",
                    "priority": "VARCHAR(20) NOT NULL DEFAULT 'normal'",
                    "subject": "VARCHAR(200)",
                    "source_page_url": "VARCHAR(500)",
                    "app_version": "VARCHAR(50)",
                    "user_agent": "VARCHAR(500)",
                    "last_message_at": "DATETIME",
                    "last_user_message_at": "DATETIME",
                    "last_admin_message_at": "DATETIME",
                    "retention_eligible_at": "DATETIME",
                    "hard_delete_at": "DATETIME",
                    "created_at": "DATETIME",
                    "updated_at": "DATETIME",
                    "closed_at": "DATETIME",
                    "closed_by": "VARCHAR(120)",
                })

            if not _table_exists(conn, "support_message"):
                conn.execute(text(
                    """
                    CREATE TABLE support_message (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id INTEGER NOT NULL,
                        sender_type     VARCHAR(20) NOT NULL,
                        sender_id       VARCHAR(120),
                        body            TEXT NOT NULL,
                        message_type    VARCHAR(20) NOT NULL DEFAULT 'text',
                        client_nonce    VARCHAR(80),
                        metadata_json   TEXT,
                        created_at      DATETIME NOT NULL,
                        edited_at       DATETIME,
                        deleted_at      DATETIME,
                        FOREIGN KEY(conversation_id) REFERENCES support_conversation(id) ON DELETE CASCADE
                    )
                    """
                ))
                print("[Migration] add_support_messaging: created support_message.")
            else:
                _add_missing_columns(conn, "support_message", {
                    "conversation_id": "INTEGER",
                    "sender_type": "VARCHAR(20)",
                    "sender_id": "VARCHAR(120)",
                    "body": "TEXT",
                    "message_type": "VARCHAR(20) NOT NULL DEFAULT 'text'",
                    "client_nonce": "VARCHAR(80)",
                    "metadata_json": "TEXT",
                    "created_at": "DATETIME",
                    "edited_at": "DATETIME",
                    "deleted_at": "DATETIME",
                })

            if not _table_exists(conn, "support_conversation_read_state"):
                conn.execute(text(
                    """
                    CREATE TABLE support_conversation_read_state (
                        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id      INTEGER NOT NULL,
                        reader_type          VARCHAR(20) NOT NULL,
                        reader_id            VARCHAR(120) NOT NULL,
                        last_read_message_id INTEGER,
                        last_read_at         DATETIME NOT NULL,
                        FOREIGN KEY(conversation_id) REFERENCES support_conversation(id) ON DELETE CASCADE,
                        FOREIGN KEY(last_read_message_id) REFERENCES support_message(id) ON DELETE SET NULL
                    )
                    """
                ))
                print("[Migration] add_support_messaging: created support_conversation_read_state.")
            else:
                _add_missing_columns(conn, "support_conversation_read_state", {
                    "conversation_id": "INTEGER",
                    "reader_type": "VARCHAR(20)",
                    "reader_id": "VARCHAR(120)",
                    "last_read_message_id": "INTEGER",
                    "last_read_at": "DATETIME",
                })

            for ddl in [
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_support_conversation_public_id ON support_conversation(public_id)",
                "CREATE INDEX IF NOT EXISTS ix_support_conversation_user_status ON support_conversation(user_id, status)",
                "CREATE INDEX IF NOT EXISTS ix_support_conversation_status_last ON support_conversation(status, last_message_at)",
                "CREATE INDEX IF NOT EXISTS ix_support_conversation_retention ON support_conversation(retention_eligible_at)",
                "CREATE INDEX IF NOT EXISTS ix_support_conversation_hard_delete ON support_conversation(hard_delete_at)",
                "CREATE INDEX IF NOT EXISTS ix_support_message_conversation_created ON support_message(conversation_id, created_at)",
                "CREATE INDEX IF NOT EXISTS ix_support_message_sender_created ON support_message(sender_id, created_at)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_support_message_client_nonce ON support_message(conversation_id, sender_id, client_nonce)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_support_read_state_reader ON support_conversation_read_state(conversation_id, reader_type, reader_id)",
                "CREATE INDEX IF NOT EXISTS ix_support_read_state_reader ON support_conversation_read_state(reader_type, reader_id)",
            ]:
                conn.execute(text(ddl))

            trans.commit()
            print("[Migration] add_support_messaging: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_support_messaging"})
            trans.rollback()
            print(f"[Migration] add_support_messaging: FAILED - {exc}")
            raise
        finally:
            conn.close()
