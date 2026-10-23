"""Feature 4 uses disposable SQLite fixtures, never live accounts or databases."""
from datetime import datetime, timedelta, timezone
import csv
import io
import json
import sqlite3

import pytest

from utils.identity_scan import scan
from scripts.scan_duplicate_accounts import main, render

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'source.db'
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE users (id TEXT PRIMARY KEY, stable_id TEXT, canonical_email TEXT, password_hash TEXT);
        CREATE TABLE identity_device_sightings (
            user_stable_id TEXT, key_id TEXT, device_hash TEXT, fingerprint_hash TEXT,
            fingerprint_status TEXT, cookie_confirmed INTEGER, first_seen TEXT, last_seen TEXT);
        CREATE TABLE teams (user_id TEXT);
    ''')
    db.executemany('INSERT INTO users VALUES (?,?,?,?)', [
        ('a.b@gmail.com', 'one', 'stale', 'SECRET_PASSWORD'),
        ('ab+old@googlemail.com', 'two', None, 'SECRET_PASSWORD'),
        ('c@example.com', 'three', 'stale', 'SECRET_PASSWORD'),
    ])
    db.executemany('INSERT INTO teams VALUES (?)', [('a.b@gmail.com',), ('a.b@gmail.com',)])
    db.commit()
    yield path, db
    db.close()


def sight(db, owner, *, key='k1', device='SECRET_DEVICE', fp='SECRET_FINGERPRINT',
          status='ok', confirmed=1, first=None, last=None):
    db.execute('INSERT INTO identity_device_sightings VALUES (?,?,?,?,?,?,?,?)',
               (owner, key, device, fp, status, confirmed,
                first or (NOW - timedelta(days=2)).isoformat(), last or NOW.isoformat()))
    db.commit()


def reasons(report):
    return {(p['left']['id'], p['right']['id']): [e['reason'] for e in p['evidence']] for p in report['pairs']}


def test_email_recomputed_counts_and_source_unchanged(source):
    path, db = source
    before = path.read_bytes()
    logical = list(db.iterdump())
    report = scan(path, at=NOW)
    assert reasons(report) == {('a.b@gmail.com', 'ab+old@googlemail.com'): ['canonical_email']}
    assert report['pairs'][0]['data_counts']['a.b@gmail.com']['teams'] == 2
    assert report['pairs'][0]['data_counts']['ab+old@googlemail.com']['teams'] == 0
    assert report['available_count_tables'] == ['teams']
    assert 'SECRET' not in json.dumps(report)
    assert path.read_bytes() == before
    assert list(db.iterdump()) == logical


def test_device_matches_key_rotation_and_no_transitive_pairs(source):
    path, db = source
    sight(db, 'two')
    sight(db, 'three')
    sight(db, 'two', key='k2', device='other', fp='other')
    sight(db, 'three', key='k2', device='other', fp='other')
    report = scan(path, at=NOW)
    assert len(report['pairs']) == 2
    pair = report['pairs'][1]
    assert pair['priority'] == 2
    assert [e['reason'] for e in pair['evidence']] == ['confirmed_device', 'fingerprint']
    assert pair['evidence'][0]['observations']['c@example.com']['last_seen'] == NOW.isoformat()
    assert 'SECRET' not in json.dumps(report)


@pytest.mark.parametrize('override', [
    {'last': (NOW-timedelta(days=180)).isoformat(), 'first': (NOW-timedelta(days=181)).isoformat()},
    {'last': (NOW+timedelta(seconds=1)).isoformat()},
    {'last': 'invalid'}, {'first': 'invalid'},
    {'first': (NOW+timedelta(days=1)).isoformat()},
    {'key': 'different'}, {'key': ''},
    {'device': '', 'fp': ''}, {'confirmed': 0, 'status': 'missing'},
])
def test_ineligible_evidence_not_matched(source, override):
    path, db = source
    sight(db, 'two')
    sight(db, 'three', **override)
    assert len(scan(path, at=NOW)['pairs']) == 1


def test_fingerprint_independent_of_cookie(source):
    path, db = source
    sight(db, 'two', confirmed=0)
    sight(db, 'three', confirmed=0)
    report = scan(path, at=NOW)
    assert report['pairs'][1]['priority'] == 3
    assert [e['reason'] for e in report['pairs'][1]['evidence']] == ['fingerprint']


def test_cookie_independent_of_missing_fingerprint(source):
    path, db = source
    sight(db, 'two', status='missing', fp='')
    sight(db, 'three', status='missing', fp='')
    assert list(reasons(scan(path, at=NOW)).values())[1] == ['confirmed_device']


def test_orphan_and_ambiguous_identity_not_attributed(source):
    path, db = source
    sight(db, 'two')
    sight(db, 'orphan')
    db.execute("UPDATE users SET stable_id='two' WHERE stable_id='three'")
    db.commit()
    report = scan(path, at=NOW)
    assert len(report['pairs']) == 1
    assert report['skipped_device_rows'] == 2


def test_legacy_schema_and_invalid_address(tmp_path):
    path = tmp_path/'legacy.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE users (id TEXT)')
        db.executemany('INSERT INTO users VALUES (?)', [('a.b@gmail.com',), ('ab@gmail.com',), ('bad',)])
    report = scan(path, at=NOW)
    assert len(report['pairs']) == 1
    assert report['invalid_email_count'] == 1
    assert not report['device_evidence_available']
    assert report['warnings']
    assert report['available_count_tables'] == []


def test_wal_committed_rows_visible_uncommitted_rows_not_visible(source):
    path, db = source
    db.execute('PRAGMA journal_mode=WAL')
    db.execute("INSERT INTO users VALUES ('ab+wal@gmail.com', 'four', NULL, NULL)")
    db.commit()
    before = {str(p): p.read_bytes() for p in (path, path.with_name(path.name+'-wal'))}
    db.execute("INSERT INTO users VALUES ('ab+pending@gmail.com', 'five', NULL, NULL)")
    assert scan(path, at=NOW)['user_count'] == 4
    assert {name: __import__('pathlib').Path(name).read_bytes() for name in before} == before
    db.rollback()


@pytest.mark.parametrize('format', ['console', 'csv', 'json'])
def test_formats_private_files_and_no_overwrite(source, tmp_path, format):
    path, db = source
    output = tmp_path/f'report.{format}'
    assert main([str(path), '--format', format, '--output', str(output)]) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    content = output.read_text()
    assert 'SECRET' not in content
    assert main([str(path), '--output', str(output)]) == 1
    assert output.read_text() == content


@pytest.mark.parametrize('suffix', ['', '-wal', '-shm', '-journal'])
def test_output_cannot_target_database_or_sidecars(source, suffix):
    path, db = source
    before = path.read_bytes()
    assert main([str(path), '--output', str(path)+suffix]) == 1
    assert path.read_bytes() == before


def test_csv_formula_escaping(source):
    path, db = source
    db.executemany('INSERT INTO users VALUES (?,?,NULL,NULL)', [('=x@gmail.com', 'four'), ('=x@googlemail.com', 'five')])
    db.commit()
    rows = list(csv.DictReader(io.StringIO(render(scan(path, at=NOW), 'csv'))))
    assert any(r['left'].startswith("'=x") for r in rows)


def test_missing_db_not_created(tmp_path):
    path = tmp_path/'missing.db'
    assert main([str(path)]) == 1
    assert not path.exists()


def test_no_supported_schema(tmp_path):
    path = tmp_path/'empty.db'
    sqlite3.connect(path).close()
    with pytest.raises(ValueError):
        scan(path, at=NOW)


def test_evidence_just_inside_retention_boundary(source):
    path, db = source
    last = (NOW-timedelta(days=180)+timedelta(microseconds=1)).isoformat()
    first = (NOW-timedelta(days=181)).isoformat()
    sight(db, 'two', first=first, last=last)
    sight(db, 'three', first=first, last=last)
    assert len(scan(path, at=NOW)['pairs']) == 2


def test_partial_device_schema_reports_unavailable(source):
    path, db = source
    db.execute('DROP TABLE identity_device_sightings')
    db.execute('CREATE TABLE identity_device_sightings (user_stable_id TEXT)')
    db.commit()
    report = scan(path, at=NOW)
    assert len(report['pairs']) == 1
    assert not report['device_evidence_available']
    assert report['warnings']


def test_empty_report_and_retention_metadata(tmp_path):
    path = tmp_path/'empty-users.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE users (id TEXT PRIMARY KEY)')
    report = scan(path, at=NOW)
    assert report['pairs'] == []
    assert report['delete_by'] == (NOW+timedelta(days=30)).isoformat()
    assert len(list(csv.reader(io.StringIO(render(report, 'csv'))))) == 1
    assert json.loads(render(report, 'json')) == report


def test_pair_expiry_uses_matching_key_not_unrelated_latest_observations(source):
    path, db = source
    old = (NOW-timedelta(days=179)).isoformat()
    first = (NOW-timedelta(days=180)).isoformat()
    sight(db, 'two', key='k1', first=first)
    sight(db, 'three', key='k1', first=first, last=old)
    sight(db, 'two', key='k2', first=first, last=old)
    sight(db, 'three', key='k2', first=first)
    pair = scan(path, at=NOW)['pairs'][1]
    assert all(e['expires_at'] == (NOW+timedelta(days=1)).isoformat() for e in pair['evidence'])
