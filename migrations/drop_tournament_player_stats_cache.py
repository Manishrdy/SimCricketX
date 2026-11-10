"""Retire tournament_player_stats_cache — a derived copy of match_scorecards.

The cache was rebuilt inside update_standings, before the match's scorecards
were archived, so on a normal completion it found no players and never moved.
Rows froze at whatever a re-simulation last wrote, and the Statistics page
served them ("18 matches played, 2 shown"). Every reader now aggregates
match_scorecards directly, so the table has no purpose. Nothing here touches
scorecards, career totals or standings: the cache held no original data.

Two steps, because the model is gone but the table's foreign keys are not:

  * On boot (registered in migrations/precheck.py) the leftover ROWS are
    deleted. They reference tournaments/players/teams with no ON DELETE
    action, and with the ORM cascade removed they would otherwise block
    deleting any tournament, team or player they point at.
  * The TABLE is dropped only by an explicit --apply, which refuses while any
    process holding the database is not running the checked-out code (see
    utils/runtime_registry.py). Deploy, restart, then drop.

  python migrations/drop_tournament_player_stats_cache.py --db /absolute/path/cricket_sim.db
  python migrations/drop_tournament_player_stats_cache.py --db /absolute/path/cricket_sim.db --apply

Default is a read-only dry run. --apply is transactional and safe to repeat.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.runtime_registry import StaleServerError, assert_no_stale_holders  # noqa: E402 - needs the path above

TABLE = 'tournament_player_stats_cache'
_EXISTS_SQL = "SELECT 1 FROM sqlite_master WHERE type='table' AND name='" + TABLE + "'"


def migrate(db_path, apply=False):
    """Drop the table. Returns the number of rows it held (0 when absent)."""
    path = Path(db_path).resolve(strict=True)
    mode = 'rw' if apply else 'ro'
    if apply:
        assert_no_stale_holders(str(path), 'drop ' + TABLE)
    with sqlite3.connect(f'{path.as_uri()}?mode={mode}', uri=True, timeout=30) as conn:
        if apply:
            conn.execute('BEGIN IMMEDIATE')
        if not conn.execute(_EXISTS_SQL).fetchone():
            print(TABLE + ' is already absent; nothing to do.')
            return 0
        count = conn.execute('SELECT COUNT(*) FROM ' + TABLE).fetchone()[0]
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (table,) in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            for fk in conn.execute(f'PRAGMA foreign_key_list({quoted})'):
                if fk[2] == TABLE:
                    raise RuntimeError(f'Refusing to drop: {table} references {TABLE}')
        print(f'{path}: {TABLE} contains {count} rows.')
        if apply:
            conn.execute('DROP TABLE ' + TABLE)
            print(f'Applied: removed {TABLE}, its indexes, and all rows.')
        else:
            print(f'DRY RUN: would drop {TABLE} and all rows. Use --apply to execute.')
        return count


def run_migration(db, app):
    """Precheck entry: empty the retired table; never drop it on boot."""
    from sqlalchemy import text
    from utils.exception_tracker import log_exception

    with app.app_context():
        conn = db.engine.connect()
        try:
            if not conn.execute(text(_EXISTS_SQL)).fetchone():
                return 0
            removed = conn.execute(text('DELETE FROM ' + TABLE)).rowcount or 0
            conn.commit()
            if removed:
                print(f'[Migration] drop_tournament_player_stats_cache: cleared {removed} retired cache row(s).')
            print('[Migration] drop_tournament_player_stats_cache: empty table still present — run '
                  '`python migrations/drop_tournament_player_stats_cache.py --db <path> --apply` to drop it.')
            return removed
        except Exception as exc:
            conn.rollback()
            log_exception(exc, source='sqlite', context={'migration': 'drop_tournament_player_stats_cache'})
            raise
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', required=True, help='Existing production/local SQLite database path')
    parser.add_argument('--apply', action='store_true', help='Actually drop the table (default: dry run)')
    args = parser.parse_args()
    try:
        migrate(args.db, args.apply)
    except StaleServerError as exc:
        parser.exit(2, f'{exc}\n')
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        parser.exit(1, f'Migration failed: {exc}\n')


if __name__ == '__main__':
    main()
