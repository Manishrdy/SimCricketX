import sqlite3
import pytest
from migrations.drop_admin_audit_log import migrate


def test_dry_run_apply_and_repeat(tmp_path):
    path = tmp_path / 'database.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE admin_audit_log (id INTEGER PRIMARY KEY, action TEXT);
            CREATE INDEX ix_audit_action ON admin_audit_log(action);
            INSERT INTO admin_audit_log(action) VALUES ('old action');
            CREATE TABLE users (id TEXT PRIMARY KEY);
            INSERT INTO users VALUES ('preserve');
        ''')
    original = path.read_bytes()
    assert migrate(path) == 1
    assert path.read_bytes() == original
    assert migrate(path, apply=True) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name IN ('admin_audit_log','ix_audit_action')").fetchall() == []
        assert conn.execute('SELECT id FROM users').fetchall() == [('preserve',)]
    assert migrate(path, apply=True) == 0


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / 'missing.db'
    with pytest.raises(FileNotFoundError):
        migrate(path, apply=True)
    assert not path.exists()


def test_referenced_table_is_not_dropped(tmp_path):
    path = tmp_path / 'database.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE admin_audit_log(id INTEGER PRIMARY KEY); CREATE TABLE dependent(audit_id INTEGER REFERENCES admin_audit_log(id));')
    with pytest.raises(RuntimeError, match='references'):
        migrate(path, apply=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='admin_audit_log'").fetchone()
