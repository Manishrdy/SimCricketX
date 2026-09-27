"""Remove the retired SQLite admin audit table and all its rows.

Deploy the code that removes audit-table readers/writers first, and stop old
application workers before applying. Take your normal production backup first.
No application imports or startup hooks are executed by this migration.

  python migrations/drop_admin_audit_log.py --db /absolute/path/cricket_sim.db
  python migrations/drop_admin_audit_log.py --db /absolute/path/cricket_sim.db --apply

Default is a read-only dry run. --apply is transactional and safe to repeat.
Earlier admin UI removals require no table drops; auction_audit_logs is unrelated.
"""
import argparse
import sqlite3
from pathlib import Path


def migrate(db_path, apply=False):
    path = Path(db_path).resolve(strict=True)
    mode = 'rw' if apply else 'ro'
    with sqlite3.connect(f'{path.as_uri()}?mode={mode}', uri=True, timeout=30) as conn:
        if apply:
            conn.execute('BEGIN IMMEDIATE')
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_audit_log'"
        ).fetchone()
        if not exists:
            print('admin_audit_log is already absent; nothing to do.')
            return 0
        count = conn.execute('SELECT COUNT(*) FROM admin_audit_log').fetchone()[0]
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (table,) in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            for fk in conn.execute(f'PRAGMA foreign_key_list({quoted})'):
                if fk[2] == 'admin_audit_log':
                    raise RuntimeError(f'Refusing to drop: {table} references admin_audit_log')
        print(f'{path}: admin_audit_log contains {count} rows.')
        if apply:
            conn.execute('DROP TABLE admin_audit_log')
            print('Applied: removed admin_audit_log, its indexes, and all rows.')
        else:
            print('DRY RUN: would drop admin_audit_log and all rows. Use --apply to execute.')
        return count


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', required=True, help='Existing production/local SQLite database path')
    parser.add_argument('--apply', action='store_true', help='Actually drop the table (default: dry run)')
    args = parser.parse_args()
    try:
        migrate(args.db, args.apply)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        parser.exit(1, f'Migration failed: {exc}\n')


if __name__ == '__main__':
    main()
