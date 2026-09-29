"""Verify migration effects on a disposable SQLite backup, never on the source.

    python3 -m migrations.verify --db ./cricket_sim.db

Exit codes: 0 = automated checks current, 1 = pending changes, 2 = failed or
unverified checks. Optional historical repairs are reported separately and
require operator review; this command cannot prove historical data completeness.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch


# These scripts require operator intent or evidence outside the target DB.
MANUAL = {
    "recover_archived_stats_from_backup": "Optional recovery needs a source backup and selected matches; not a schema prerequisite.",
    "repair_knockout_bracket_corruption": "Historical bracket repair needs review of fixtures and live match files.",
    "repair_tournament_standings": "Historical standings repair needs review of orphan/duplicate matches.",
}
EXTRA = (
    "add_exception_log_github_sync", "add_issue_webhook_events",
    "add_player_styles", "add_community_mentions", "drop_admin_audit_log",
    "repair_fc_scorecard_super_over_flags", "migrate_enhanced_stats",
)


def inventory(registered, directory=None):
    """Fail closed when a new standalone script has no verification policy."""
    directory = directory or Path(__file__).parent
    files = {p.stem for p in directory.glob('*.py') if not p.name.startswith('_')}
    known = set(registered) | set(EXTRA) | set(MANUAL) | {'precheck', 'verify'}
    return sorted(files - known)


def fingerprint(path):
    """Hash logical contents, not SQLite pages (which change on no-op writes)."""
    digest = hashlib.sha256()
    with contextlib.closing(sqlite3.connect(str(path))) as conn:
        for statement in conn.iterdump():
            digest.update(statement.encode('utf-8'))
            digest.update(b'\n')
    return digest.hexdigest()


def model_drift(engine, metadata):
    """Check structural requirements that idempotent migrations may skip."""
    from sqlalchemy import UniqueConstraint, inspect
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    missing = []
    for table in metadata.sorted_tables:
        if table.name not in tables:
            missing.append(table.name + ': missing table')
            continue
        columns = {c['name'] for c in inspector.get_columns(table.name)}
        missing.extend(table.name + '.' + c.name + ': missing column'
                       for c in table.columns if c.name not in columns)
        indexes = inspector.get_indexes(table.name)
        unique = {tuple(c['column_names']) for c in inspector.get_unique_constraints(table.name)}
        unique.update(tuple(i['column_names']) for i in indexes if i['unique']
                      and i.get('dialect_options', {}).get('sqlite_where') is None)
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint) and tuple(c.name for c in constraint.columns) not in unique:
                missing.append(table.name + ': missing unique constraint on ' + ', '.join(c.name for c in constraint.columns))
        for index in table.indexes:
            expected = tuple(c.name for c in index.columns)
            if not any(tuple(i['column_names']) == expected and bool(i['unique']) == bool(index.unique)
                       and i.get('dialect_options', {}).get('sqlite_where') is None for i in indexes):
                missing.append(table.name + ': missing index ' + str(index.name))
        # SQLAlchemy's SQLite DDL parser can omit ON DELETE from inline
        # REFERENCES clauses used by historical migrations. PRAGMA is the
        # authoritative source, including after table rebuilds.
        quoted = '"' + table.name.replace('"', '""') + '"'
        with engine.connect() as conn:
            rows = conn.exec_driver_sql('PRAGMA foreign_key_list(' + quoted + ')').fetchall()
        groups = {}
        for row in rows:
            groups.setdefault(row[0], []).append(row)
        actual = set()
        for group in groups.values():
            group.sort(key=lambda row: row[1])
            actual.add((tuple(row[3] for row in group), group[0][2],
                        tuple(row[4] for row in group), group[0][6].upper()))
        for constraint in table.foreign_key_constraints:
            expected = (tuple(c.name for c in constraint.columns),
                        constraint.elements[0].column.table.name,
                        tuple(e.column.name for e in constraint.elements),
                        (constraint.ondelete or 'NO ACTION').upper())
            if expected not in actual:
                missing.append(table.name + ': foreign key mismatch on ' + ', '.join(expected[0]))
    return missing


def _runner(name, loader, db, app, snapshot):
    if name == 'drop_admin_audit_log':
        from migrations.drop_admin_audit_log import migrate
        return migrate(str(snapshot), apply=True)
    if name == 'repair_fc_scorecard_super_over_flags':
        from migrations.repair_fc_scorecard_super_over_flags import execute_repair
        with contextlib.closing(sqlite3.connect(str(snapshot))) as conn:
            execute_repair(conn)
            conn.commit()
        return
    if name == 'migrate_enhanced_stats':
        # The legacy interactive script only calls create_all. Never import it:
        # it imports app.py at module scope.
        with app.app_context():
            db.create_all()
        return
    if name in ('community_board', 'add_community_mentions',
                'cleanup_orphaned_stats', 'rebuild_tournament_player_stats_cache'):
        module = importlib.import_module('migrations.' + name)
        return module.run_migration(db, app, apply=True)
    return loader()(db, app)


def rehearse(name, action, snapshot):
    """A swallowed migration exception must not be mistaken for success."""
    before = fingerprint(snapshot)
    logged = []
    output = io.StringIO()

    def record_error(*args, **kwargs):
        logged.append('Migration logged an exception; inspect this script before applying.')

    # Some modules retain an imported alias; patch those too. No error records
    # or outbound GitHub issue notifications should be generated by verification.
    import sys
    from utils import exception_tracker
    original_logger = exception_tracker.log_exception

    def restore_new_aliases():
        for module_name, module in list(sys.modules.items()):
            if module_name.startswith(('migrations.', 'scripts.')) and getattr(module, 'log_exception', None) is record_error:
                module.log_exception = original_logger

    with contextlib.ExitStack() as stack:
        stack.callback(restore_new_aliases)
        stack.enter_context(contextlib.redirect_stdout(output))
        stack.enter_context(contextlib.redirect_stderr(output))
        stack.enter_context(patch.object(exception_tracker, 'log_exception', record_error))
        for module_name, module in list(sys.modules.items()):
            if module_name.startswith(('migrations.', 'scripts.')) and hasattr(module, 'log_exception'):
                stack.enter_context(patch.object(module, 'log_exception', record_error))
        try:
            action()
            if logged or 'FAILED' in output.getvalue() or 'SKIPPED' in output.getvalue():
                return {'name': name, 'status': 'ERROR', 'detail': 'Migration reported an error or skipped work; completion is unverified.'}
        except Exception as exc:
            # Avoid printing SQL parameters, which can contain production data.
            return {'name': name, 'status': 'ERROR', 'detail': type(exc).__name__ + ': migration rehearsal failed; completion is unverified.'}
    changed = before != fingerprint(snapshot)
    return {'name': name, 'status': 'PENDING' if changed else 'CURRENT',
            'detail': 'Would change the snapshot.' if changed else 'No logical database changes.'}


def verify(db_path):
    from flask import Flask
    from database import db
    import database.models  # noqa: F401 -- register metadata without app.py
    from migrations.precheck import MIGRATIONS, _loader

    source = Path(db_path).resolve(strict=True)
    results = []
    registry = dict(MIGRATIONS)
    for name in inventory(registry):
        results.append({'name': name, 'status': 'UNVERIFIED',
                        'detail': 'New migration has no verifier policy. Add it to migrations.verify.'})

    with tempfile.TemporaryDirectory(prefix='simcricketx-verify-') as temp:
        snapshot = Path(temp) / 'snapshot.db'
        # backup() includes committed WAL contents. Never copy only the main file
        # and never use immutable=1, which would ignore a live WAL.
        with contextlib.closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
            with contextlib.closing(sqlite3.connect(str(snapshot))) as dest:
                src.backup(dest)

        app = Flask('migration_verifier')
        app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + str(snapshot),
                          SQLALCHEMY_TRACK_MODIFICATIONS=False, TESTING=True)
        db.init_app(app)
        try:
            with contextlib.closing(sqlite3.connect(str(snapshot))) as conn:
                integrity = conn.execute('PRAGMA integrity_check').fetchall()
                violations = conn.execute('PRAGMA foreign_key_check').fetchall()
            results.append({'name': 'sqlite_integrity',
                            'status': 'CURRENT' if integrity == [('ok',)] and not violations else 'ERROR',
                            'detail': 'Integrity and foreign keys checked; {} FK violation(s).'.format(len(violations))})
            # Check required tables/columns BEFORE any rehearsal can create them.
            with app.app_context():
                missing = model_drift(db.engine, db.metadata)
                results.append({'name': 'model_schema',
                                'status': 'PENDING' if missing else 'CURRENT',
                                'detail': '; '.join(missing) if missing else 'Required tables, columns, indexes, uniqueness and foreign keys exist.'})
            steps = [(name, loader) for name, loader in MIGRATIONS if name not in MANUAL]
            steps.extend((name, _loader('migrations.' + name)) for name in EXTRA if name not in registry)
            for name, loader in steps:
                results.append(rehearse(name, lambda n=name, l=loader: _runner(n, l, db, app, snapshot), snapshot))
        finally:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()

    for name, reason in MANUAL.items():
        results.append({'name': name, 'status': 'MANUAL', 'detail': reason})
    return results


def exit_code(results, strict=False):
    if any(r['status'] in ('ERROR', 'UNVERIFIED') or (strict and r['status'] == 'MANUAL') for r in results):
        return 2
    return 1 if any(r['status'] == 'PENDING' for r in results) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, help='Existing SQLite database; opened read-only.')
    parser.add_argument('--json', action='store_true', help='Machine-readable report.')
    parser.add_argument('--strict', action='store_true', help='Manual review items also make the check exit 2.')
    args = parser.parse_args(argv)
    try:
        results = verify(args.db)
    except Exception as exc:
        results = [{'name': 'verification', 'status': 'ERROR',
                    'detail': type(exc).__name__ + ': unable to complete verification; check database path and dependencies.'}]
    code = exit_code(results, args.strict)
    if args.json:
        print(json.dumps({'database': str(Path(args.db).resolve()), 'exit_code': code, 'checks': results}, indent=2))
    else:
        for row in results:
            print('[{status}] {name}: {detail}'.format(**row))
        print('\nSource database was not modified. Rehearsals ran on a temporary snapshot.')
        print('Automated checks current; manual items remain outside this guarantee.' if code == 0
              else 'Database alignment is NOT confirmed. Review PENDING, ERROR, UNVERIFIED and MANUAL items above.')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
