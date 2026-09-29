"""Verification must never apply migrations to the source database."""
import contextlib
import json
import sqlite3
import subprocess
import sys

from flask import Flask
import pytest

from database import db
from migrations.verify import exit_code, fingerprint, inventory, rehearse, verify


@pytest.fixture
def source_db(tmp_path):
    path = tmp_path / 'source.db'
    app = Flask('verification-test')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + str(path)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        db.engine.dispose()
    return path


def test_real_registry_reports_pending_and_preserves_source(source_db):
    with contextlib.closing(sqlite3.connect(str(source_db))) as conn, conn:
        conn.execute('CREATE TABLE admin_audit_log (id INTEGER PRIMARY KEY)')
        conn.execute('INSERT INTO admin_audit_log VALUES (1)')
        conn.execute('DROP TABLE community_mentions')
    before_bytes = source_db.read_bytes()
    results = verify(source_db)
    statuses = {r['name']: r['status'] for r in results}
    assert statuses['drop_admin_audit_log'] == 'PENDING'
    assert statuses['model_schema'] == 'PENDING'
    assert statuses['community_board'] == 'PENDING'
    assert not [r for r in results if r['status'] in ('ERROR', 'UNVERIFIED')]
    assert source_db.read_bytes() == before_bytes
    assert exit_code(results) == 1


def test_wal_contents_are_included(source_db):
    with contextlib.closing(sqlite3.connect(str(source_db))) as live:
        live.execute('PRAGMA journal_mode=WAL')
        live.execute('CREATE TABLE admin_audit_log (id INTEGER PRIMARY KEY)')
        live.commit()
        results = verify(source_db)
        assert next(r for r in results if r['name'] == 'drop_admin_audit_log')['status'] == 'PENDING'
        assert live.execute("SELECT name FROM sqlite_master WHERE name='admin_audit_log'").fetchone()


def test_rehearsal_detects_data_changes_and_noop(tmp_path):
    path = tmp_path / 'rehearsal.db'
    with contextlib.closing(sqlite3.connect(str(path))) as conn, conn:
        conn.execute('CREATE TABLE sample (value INTEGER)')
        conn.execute('INSERT INTO sample VALUES (0)')

    def update():
        with contextlib.closing(sqlite3.connect(str(path))) as conn, conn:
            conn.execute('UPDATE sample SET value=1')

    assert rehearse('backfill', update, path)['status'] == 'PENDING'
    assert rehearse('backfill', update, path)['status'] == 'CURRENT'


def test_swallowed_exception_fails_check_and_restores_logger(tmp_path):
    from utils import exception_tracker
    original = exception_tracker.log_exception
    path = tmp_path / 'empty.db'
    sqlite3.connect(str(path)).close()

    def swallowed():
        exception_tracker.log_exception(ValueError('private test data'))

    result = rehearse('broken', swallowed, path)
    assert result['status'] == 'ERROR'
    assert 'private test data' not in result['detail']
    assert exception_tracker.log_exception is original


def test_unknown_migration_fails_coverage(tmp_path):
    (tmp_path / 'new_migration.py').write_text('')
    assert inventory([], tmp_path) == ['new_migration']
    assert exit_code([{'status': 'UNVERIFIED'}]) == 2
    assert exit_code([{'status': 'CURRENT'}, {'status': 'MANUAL'}]) == 0
    assert exit_code([{'status': 'MANUAL'}], strict=True) == 2


def test_missing_path_does_not_create_database(tmp_path):
    path = tmp_path / 'missing.db'
    result = subprocess.run([sys.executable, '-m', 'migrations.verify', '--db', str(path), '--json'],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(result.stdout)['checks'][0]['status'] == 'ERROR'
    assert not path.exists()


def test_fully_rehearsed_database_is_current(source_db):
    from migrations.precheck import MIGRATIONS, _loader
    from migrations.verify import EXTRA, MANUAL, _runner
    app = Flask('settled-verification-test')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + str(source_db)
    db.init_app(app)
    registry = dict(MIGRATIONS)
    steps = [(name, loader) for name, loader in MIGRATIONS if name not in MANUAL]
    steps.extend((name, _loader('migrations.' + name)) for name in EXTRA if name not in registry)
    try:
        for name, loader in steps:
            result = rehearse(name, lambda n=name, l=loader: _runner(n, l, db, app, source_db), source_db)
            assert result['status'] != 'ERROR', result
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
    results = verify(source_db)
    assert exit_code(results) == 0, [r for r in results if r['status'] not in ('CURRENT', 'MANUAL')]
    before = fingerprint(source_db)
    cli = subprocess.run([sys.executable, '-m', 'migrations.precheck', '--check', '--db', str(source_db)],
                         capture_output=True, text=True)
    assert cli.returncode == 0, cli.stdout + cli.stderr
    assert fingerprint(source_db) == before


def test_model_drift_detects_missing_index_and_foreign_key(tmp_path):
    from sqlalchemy import Column, ForeignKey, Index, Integer, MetaData, Table, create_engine
    from migrations.verify import model_drift
    metadata = MetaData()
    Table('parent', metadata, Column('id', Integer, primary_key=True))
    child = Table('child', metadata, Column('id', Integer, primary_key=True),
                  Column('parent_id', Integer, ForeignKey('parent.id', ondelete='CASCADE')))
    Index('ix_child_parent', child.c.parent_id)
    engine = create_engine('sqlite:///' + str(tmp_path / 'drift.db'))
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql('CREATE TABLE parent (id INTEGER PRIMARY KEY)')
            conn.exec_driver_sql('CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER)')
        drift = model_drift(engine, metadata)
        assert any('missing index' in d for d in drift)
        assert any('foreign key mismatch' in d for d in drift)
    finally:
        engine.dispose()


def test_cli_does_not_import_production_app(source_db):
    code = '''
import sys
from migrations.verify import verify
verify(sys.argv[1])
assert 'app' not in sys.modules
assert 'engine.match' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', code, str(source_db)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_inline_references_cascade_is_recognized(tmp_path):
    from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table, create_engine
    from migrations.verify import model_drift
    metadata = MetaData()
    Table('parent', metadata, Column('id', Integer, primary_key=True))
    Table('child', metadata, Column('id', Integer, primary_key=True),
          Column('parent_id', Integer, ForeignKey('parent.id', ondelete='CASCADE')))
    engine = create_engine('sqlite:///' + str(tmp_path / 'inline.db'))
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql('CREATE TABLE parent (id INTEGER PRIMARY KEY)')
            conn.exec_driver_sql('CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id) ON DELETE CASCADE)')
        assert model_drift(engine, metadata) == []
    finally:
        engine.dispose()
