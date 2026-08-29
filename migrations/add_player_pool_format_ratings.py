"""Add per-format ratings to the global and per-user player pools.

The original batting/bowling/fielding columns remain the canonical T20
ratings.  Existing values are copied into the new List A and First-Class
columns when each column is first added, preserving the pre-migration result
for every player.  FC's technique/temperament/stamina axes start neutral (50).

Production CLI (dry-run is the default):

    python -m migrations.add_player_pool_format_ratings --db ./cricket_sim.db
    python -m migrations.add_player_pool_format_ratings --db ./cricket_sim.db --apply

Idempotent: every column is inspected before ALTER TABLE is attempted.
"""

import argparse
import os
import sys

from sqlalchemy import create_engine, inspect, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.exception_tracker import log_exception


_FORMAT_COLUMNS = (
    ("list_a_batting_rating", "batting_rating"),
    ("list_a_bowling_rating", "bowling_rating"),
    ("list_a_fielding_rating", "fielding_rating"),
    ("fc_batting_rating", "batting_rating"),
    ("fc_bowling_rating", "bowling_rating"),
    ("fc_fielding_rating", "fielding_rating"),
    ("fc_technique_rating", None),
    ("fc_temperament_rating", None),
    ("fc_stamina_rating", None),
)

_POOL_TABLES = ("master_players", "user_players")


def _pending_columns(conn, table_name):
    existing = {column["name"] for column in inspect(conn).get_columns(table_name)}
    return [
        (column_name, legacy_source)
        for column_name, legacy_source in _FORMAT_COLUMNS
        if column_name not in existing
    ]


def _add_columns(conn, table_name):
    added = []
    for column_name, legacy_source in _pending_columns(conn, table_name):
        conn.execute(text(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} INTEGER DEFAULT 50"
        ))
        if legacy_source:
            conn.execute(text(
                f"UPDATE {table_name} SET {column_name} = "
                f"COALESCE({legacy_source}, 50)"
            ))
        added.append(column_name)
    return added


def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            table_names = set(inspect(conn).get_table_names())
            changed = []
            for table_name in _POOL_TABLES:
                if table_name in table_names:
                    added = _add_columns(conn, table_name)
                    if added:
                        changed.append(f"{table_name}: {', '.join(added)}")
            trans.commit()
            if changed:
                print("[Migration] add_player_pool_format_ratings: " + "; ".join(changed))
            else:
                print("[Migration] add_player_pool_format_ratings: already applied.")
        except Exception as exc:
            trans.rollback()
            log_exception(
                exc,
                source="sqlite",
                context={"migration": "add_player_pool_format_ratings"},
            )
            print(f"[Migration] add_player_pool_format_ratings: FAILED — {exc}")
            raise
        finally:
            conn.close()


def _print_plan(conn, db_path):
    """Print the exact schema work still required; never writes."""
    table_names = set(inspect(conn).get_table_names())
    print("=" * 72)
    print("Player Pool Format Ratings Migration — DRY RUN")
    print("=" * 72)
    print(f"Database: {db_path}")
    print()

    pending_total = 0
    missing_tables = []
    for table_name in _POOL_TABLES:
        if table_name not in table_names:
            missing_tables.append(table_name)
            print(f"[MISSING] {table_name} — table does not exist; no changes planned.")
            continue
        pending = _pending_columns(conn, table_name)
        if not pending:
            print(f"[READY]   {table_name} — all format-rating columns already exist.")
            continue
        print(f"[PENDING] {table_name} — {len(pending)} column(s):")
        for column_name, legacy_source in pending:
            if legacy_source:
                detail = f"backfill from {legacy_source}"
            else:
                detail = "initialize to neutral 50"
            print(f"          + {column_name} INTEGER DEFAULT 50 ({detail})")
        pending_total += len(pending)

    print()
    print(f"Summary: {pending_total} column addition(s) pending.")
    if missing_tables:
        print("Missing required table(s): " + ", ".join(missing_tables))
    print("DRY RUN — no database changes were made.")
    if pending_total:
        print("Re-run with --apply to execute this plan.")
    return pending_total, missing_tables


def _run_cli(db_path, apply=False):
    resolved = os.path.abspath(os.path.expanduser(db_path))
    if not os.path.isfile(resolved):
        print(f"ERROR: database file does not exist: {resolved}", file=sys.stderr)
        return 2

    engine = create_engine("sqlite:///" + resolved)
    try:
        with engine.connect() as conn:
            pending_total, missing_tables = _print_plan(conn, resolved)

        if missing_tables:
            print("ERROR: refusing to apply because required player-pool tables are missing.", file=sys.stderr)
            return 2
        if not apply:
            return 0
        if pending_total == 0:
            print("Nothing to apply; schema is already current.")
            return 0

        print()
        print("APPLYING migration in one transaction...")
        with engine.begin() as conn:
            for table_name in _POOL_TABLES:
                added = _add_columns(conn, table_name)
                print(f"[APPLIED] {table_name}: {', '.join(added) if added else 'no changes'}")
        print(f"Migration complete: {pending_total} column(s) added successfully.")
        return 0
    except Exception as exc:
        print(f"ERROR: migration failed and was rolled back: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Add per-format ratings to master_players and user_players (dry-run by default)."
    )
    parser.add_argument(
        "--db",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cricket_sim.db"),
        help="Path to the SQLite database (default: project cricket_sim.db).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the pending schema changes. Without this flag, only a dry-run report is printed.",
    )
    args = parser.parse_args()
    sys.exit(_run_cli(args.db, apply=args.apply))
