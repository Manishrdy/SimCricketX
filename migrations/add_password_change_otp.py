"""
Migration: add_password_change_otp
===================================
Adds three columns to the `users` table to support the account-settings
password-change flow (verify via email OTP before committing):

    pw_change_otp_hash       VARCHAR(64)  — SHA-256 of the 6-digit OTP
    pw_change_otp_expires    DATETIME     — OTP expiry (10 min TTL)
    pw_change_pending_hash   VARCHAR(200) — new password hash, staged
                                            until OTP is confirmed

Idempotent: safe to re-run.
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
            _ensure_columns(conn)
            trans.commit()
            print("[Migration] add_password_change_otp: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_password_change_otp"})
            trans.rollback()
            print(f"[Migration] add_password_change_otp: FAILED — {exc}")
            raise
        finally:
            conn.close()


def _ensure_columns(conn):
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()}

    if "pw_change_otp_hash" not in cols:
        conn.execute(text("ALTER TABLE users ADD COLUMN pw_change_otp_hash VARCHAR(64)"))
        print("[Migration] users.pw_change_otp_hash column added.")
    else:
        print("[Migration] users.pw_change_otp_hash already present — skipped.")

    if "pw_change_otp_expires" not in cols:
        conn.execute(text("ALTER TABLE users ADD COLUMN pw_change_otp_expires DATETIME"))
        print("[Migration] users.pw_change_otp_expires column added.")
    else:
        print("[Migration] users.pw_change_otp_expires already present — skipped.")

    if "pw_change_pending_hash" not in cols:
        conn.execute(text("ALTER TABLE users ADD COLUMN pw_change_pending_hash VARCHAR(200)"))
        print("[Migration] users.pw_change_pending_hash column added.")
    else:
        print("[Migration] users.pw_change_pending_hash already present — skipped.")


if __name__ == "__main__":
    os.environ.setdefault("SIMCRICKETX_SKIP_GLOBAL_APP", "1")
    os.environ.setdefault("SIMCRICKETX_PRECHECK_RUNNING", "1")
    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
