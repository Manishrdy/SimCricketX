"""
Issue Reports Table Migration
=============================

Creates the `issue_report` table used by the in-app issue reporting
widget. Mirrors the auto-exception flow but stores user-supplied
descriptions and session log snapshots instead of stack traces.

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
                "SELECT name FROM sqlite_master WHERE type='table' AND name='issue_report'"
            )).fetchone()

            if existing is None:
                conn.execute(text(
                    """
                    CREATE TABLE issue_report (
                        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                        public_id                VARCHAR(16) NOT NULL UNIQUE,
                        user_email               VARCHAR(120) NOT NULL,
                        category                 VARCHAR(30) NOT NULL DEFAULT 'other',
                        title                    VARCHAR(200) NOT NULL,
                        description              TEXT NOT NULL,
                        page_url                 VARCHAR(500),
                        user_agent               VARCHAR(500),
                        app_version              VARCHAR(50),
                        session_logs_json        TEXT,
                        linked_exception_log_ids TEXT,
                        github_issue_number      INTEGER,
                        github_issue_url         VARCHAR(300),
                        github_sync_status       VARCHAR(20) NOT NULL DEFAULT 'pending',
                        github_sync_error        TEXT,
                        github_last_synced_at    DATETIME,
                        status                   VARCHAR(20) NOT NULL DEFAULT 'new',
                        severity                 VARCHAR(20),
                        admin_notes              TEXT,
                        created_at               DATETIME NOT NULL,
                        updated_at               DATETIME NOT NULL
                    )
                    """
                ))
                print("[Migration] add_issue_reports: created table issue_report.")
            else:
                # Idempotent: ensure each column exists if the table predates a column.
                existing_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(issue_report)")).fetchall()}
                desired_cols = {
                    "public_id":                "VARCHAR(16)",
                    "user_email":               "VARCHAR(120)",
                    "category":                 "VARCHAR(30) NOT NULL DEFAULT 'other'",
                    "title":                    "VARCHAR(200)",
                    "description":              "TEXT",
                    "page_url":                 "VARCHAR(500)",
                    "user_agent":               "VARCHAR(500)",
                    "app_version":              "VARCHAR(50)",
                    "session_logs_json":        "TEXT",
                    "linked_exception_log_ids": "TEXT",
                    "github_issue_number":      "INTEGER",
                    "github_issue_url":         "VARCHAR(300)",
                    "github_sync_status":       "VARCHAR(20) NOT NULL DEFAULT 'pending'",
                    "github_sync_error":        "TEXT",
                    "github_last_synced_at":    "DATETIME",
                    "status":                   "VARCHAR(20) NOT NULL DEFAULT 'new'",
                    "severity":                 "VARCHAR(20)",
                    "admin_notes":              "TEXT",
                    "created_at":               "DATETIME",
                    "updated_at":               "DATETIME",
                }
                for col_name, col_sql in desired_cols.items():
                    if col_name not in existing_cols:
                        conn.execute(text(f"ALTER TABLE issue_report ADD COLUMN {col_name} {col_sql}"))
                        print(f"[Migration] add_issue_reports: added column {col_name}.")

            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_issue_report_public_id ON issue_report(public_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_issue_report_status_created ON issue_report(status, created_at)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_issue_report_user_created ON issue_report(user_email, created_at)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_issue_report_github_issue_number ON issue_report(github_issue_number)"
            ))

            trans.commit()
            print("[Migration] add_issue_reports: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_issue_reports"})
            trans.rollback()
            print(f"[Migration] add_issue_reports: FAILED — {exc}")
            raise
        finally:
            conn.close()
