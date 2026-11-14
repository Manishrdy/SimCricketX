"""
users.stable_id unique index and backfill
=========================================

Every identity table declares ``ForeignKey('users.stable_id')``. SQLite only
accepts a foreign key whose parent column is a PRIMARY KEY or has a UNIQUE
index; otherwise every DML on the child table (and ``PRAGMA foreign_key_check``)
fails with "foreign key mismatch". The model declares ``unique=True``, but
``stable_id`` was added to existing databases with ``ALTER TABLE ADD COLUMN``,
which cannot add a UNIQUE constraint, so older databases never got the index.

Idempotent. Backfills a fresh UUID4 for any account whose ``stable_id`` is NULL
or blank so all-account identity reviews and FKs have a distinct stable ID for
every user. If duplicate non-NULL values exist the migration refuses to
continue rather than guessing which account owns an id.
"""

import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception

INDEX = "ux_users_stable_id"


def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(users)")).fetchall()]
            if 'stable_id' not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN stable_id VARCHAR(36)"))
                print("[Migration] add_users_stable_id_unique: added stable_id column.")
            dupes = conn.execute(text(
                "SELECT stable_id, COUNT(*) FROM users "
                "WHERE stable_id IS NOT NULL AND trim(stable_id) != '' "
                "GROUP BY stable_id HAVING COUNT(*) > 1")).fetchall()
            if dupes:
                raise RuntimeError(
                    "%d duplicate users.stable_id value(s); resolve before the unique "
                    "index can be created" % len(dupes))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS %s ON users(stable_id)" % INDEX))
            missing = conn.execute(text(
                "SELECT id FROM users WHERE stable_id IS NULL OR trim(stable_id) = '' ORDER BY id"
            )).fetchall()
            if missing:
                existing = {
                    r[0] for r in conn.execute(text(
                        "SELECT stable_id FROM users WHERE stable_id IS NOT NULL AND trim(stable_id) != ''"
                    )).fetchall()
                }
                for (user_id,) in missing:
                    sid = str(uuid.uuid4())
                    while sid in existing:
                        sid = str(uuid.uuid4())
                    existing.add(sid)
                    conn.execute(
                        text("UPDATE users SET stable_id = :sid WHERE id = :uid"),
                        {"sid": sid, "uid": user_id},
                    )
                print(f"[Migration] add_users_stable_id_unique: backfilled {len(missing)} missing stable_id value(s).")
            trans.commit()
            print("[Migration] add_users_stable_id_unique: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_users_stable_id_unique"})
            trans.rollback()
            print(f"[Migration] add_users_stable_id_unique: FAILED — {exc}")
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
