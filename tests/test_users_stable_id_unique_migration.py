"""users.stable_id needs a UNIQUE index or every identity FK raises 'foreign key mismatch'."""
import sqlite3

import pytest
from flask import Flask

from database import db
from migrations.add_users_stable_id_unique import run_migration


def _app(path):
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + str(path),
                      SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    return app


def _seed(path, ids):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE users (id VARCHAR(120) PRIMARY KEY, stable_id VARCHAR(36))")
    conn.execute("CREATE TABLE child (id INTEGER PRIMARY KEY, s VARCHAR(36) REFERENCES users(stable_id))")
    conn.executemany("INSERT INTO users VALUES (?, ?)", [(f'u{i}', s) for i, s in enumerate(ids)])
    conn.commit()
    return conn


def test_creates_index_and_fixes_foreign_key_mismatch(tmp_path):
    path = tmp_path / 't.db'
    conn = _seed(path, ['a', 'b', None, ''])
    with pytest.raises(sqlite3.OperationalError, match='foreign key mismatch'):
        conn.execute('PRAGMA foreign_key_check').fetchall()
    run_migration(db, _app(path))
    first_ids = conn.execute("SELECT id, stable_id FROM users ORDER BY id").fetchall()
    assert all(sid and sid.strip() for _, sid in first_ids)
    assert len({sid for _, sid in first_ids}) == 4
    run_migration(db, _app(path))  # idempotent: preserves backfilled IDs
    assert conn.execute("SELECT id, stable_id FROM users ORDER BY id").fetchall() == first_ids
    conn.close()
    fresh = sqlite3.connect(str(path))
    assert fresh.execute('PRAGMA foreign_key_check').fetchall() == []
    fresh.close()


def test_refuses_duplicates(tmp_path):
    path = tmp_path / 't.db'
    _seed(path, ['a', 'a']).close()
    with pytest.raises(RuntimeError, match='duplicate'):
        run_migration(db, _app(path))
