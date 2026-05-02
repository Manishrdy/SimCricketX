"""
Drop Legacy Issue Reports
=========================

Manual user-report-to-GitHub submission has been replaced by support
messaging. This removes the old `issue_report` table if it exists.
"""

import os
import sys

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
            if existing is not None:
                conn.execute(text("DROP TABLE issue_report"))
                print("[Migration] drop_issue_reports: dropped legacy issue_report table.")
            else:
                print("[Migration] drop_issue_reports: issue_report table absent.")
            trans.commit()
        except Exception as exc:
            trans.rollback()
            log_exception(exc, source="sqlite", context={"migration": "drop_issue_reports"})
            print(f"[Migration] drop_issue_reports: FAILED - {exc}")
            raise
        finally:
            conn.close()
