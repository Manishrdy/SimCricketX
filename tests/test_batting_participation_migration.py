"""Production migration is read-only by default and preserves ambiguity."""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from migrations.add_batting_participation import migrate_connection


def _legacy_db(path):
    with sqlite3.connect(str(path)) as conn:
        conn.execute('CREATE TABLE match_scorecards (id INTEGER PRIMARY KEY, record_type TEXT, runs INTEGER, balls INTEGER, is_out BOOLEAN, is_super_over BOOLEAN)')
        conn.executemany('INSERT INTO match_scorecards VALUES (?, ?, ?, ?, ?, ?)', [
            (1, 'batting', 0, 0, 0, 0),  # unknown 0* or DNB
            (2, 'batting', 0, 1, 0, 0),  # known legacy innings
            (3, 'batting', 0, 0, 1, 0),  # 0-ball dismissal
            (4, 'bowling', 0, 0, 0, 0),
            (5, 'batting', 0, 0, 0, 1),  # exclude SO from ambiguity report
        ])


@pytest.mark.parametrize('flags', [[], ['--dry-run']])
def test_cli_dry_run_does_not_change_database(tmp_path, flags):
    path = tmp_path / 'production copy.db'
    _legacy_db(path)
    before = path.read_bytes()
    result = subprocess.run([sys.executable, '-m', 'migrations.add_batting_participation',
                             '--db', str(path)] + flags, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'DRY RUN' in result.stdout
    assert 'rows left unknown: 1' in result.stdout
    assert path.read_bytes() == before


def test_apply_adds_nullable_marker_and_is_idempotent(tmp_path):
    path = tmp_path / 'prod.db'
    _legacy_db(path)
    with sqlite3.connect(str(path)) as conn:
        before = conn.execute('SELECT * FROM match_scorecards').fetchall()
        report = migrate_connection(conn, apply=True)
        assert report == {'column_missing': True, 'ambiguous_legacy_rows': 1}
        column = next(row for row in conn.execute('PRAGMA table_info(match_scorecards)') if row[1] == 'did_bat')
        assert column[3] == 0  # nullable
        assert conn.execute('SELECT did_bat FROM match_scorecards').fetchall() == [(None,)] * 5
        assert conn.execute('SELECT id, record_type, runs, balls, is_out, is_super_over FROM match_scorecards').fetchall() == before
        conn.execute('UPDATE match_scorecards SET did_bat = 1 WHERE id = 1')
        conn.commit()
        again = migrate_connection(conn, apply=True)
        assert again == {'column_missing': False, 'ambiguous_legacy_rows': 0}
        assert conn.execute('SELECT did_bat FROM match_scorecards WHERE id = 1').fetchone()[0] == 1


def test_cli_apply_and_missing_path(tmp_path):
    path = tmp_path / 'prod.db'
    _legacy_db(path)
    script = Path(__file__).resolve().parents[1] / 'migrations' / 'add_batting_participation.py'
    result = subprocess.run([sys.executable, str(script), '--db', str(path), '--apply'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(str(path)) as conn:
        assert 'did_bat' in {row[1] for row in conn.execute('PRAGMA table_info(match_scorecards)')}
    missing = tmp_path / 'missing.db'
    for flags in ([], ['--apply']):
        result = subprocess.run([sys.executable, str(script), '--db', str(missing)] + flags,
                                capture_output=True, text=True)
        assert result.returncode == 1
        assert not missing.exists()


def test_failed_prerequisite_does_not_partially_migrate(tmp_path):
    path = tmp_path / 'drifted.db'
    with sqlite3.connect(str(path)) as conn:
        conn.execute('CREATE TABLE match_scorecards (id INTEGER PRIMARY KEY)')
        conn.commit()
        with pytest.raises(RuntimeError, match='required columns'):
            migrate_connection(conn, apply=True)
        assert [row[1] for row in conn.execute('PRAGMA table_info(match_scorecards)')] == ['id']
        assert not conn.in_transaction


def test_registry_rehearsal_reports_pending_then_current(tmp_path):
    from types import SimpleNamespace
    from flask import Flask
    from sqlalchemy import create_engine
    from migrations.add_batting_participation import ensure_schema
    from migrations.precheck import MIGRATIONS
    from migrations.verify import inventory, rehearse

    path = tmp_path / 'snapshot.db'
    _legacy_db(path)
    engine = create_engine('sqlite:///' + str(path))
    db = SimpleNamespace(engine=engine)
    app = Flask('participation-migration-test')
    assert 'add_batting_participation' in dict(MIGRATIONS)
    assert 'add_batting_participation' not in inventory(dict(MIGRATIONS))
    action = lambda: ensure_schema(db, app)
    try:
        assert rehearse('add_batting_participation', action, path)['status'] == 'PENDING'
        assert rehearse('add_batting_participation', action, path)['status'] == 'CURRENT'
    finally:
        engine.dispose()
