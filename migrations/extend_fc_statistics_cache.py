"""Add and backfill FC-native tournament statistics cache fields.

The command is deliberately read-only by default. Production operators must
pass ``--apply`` to add missing columns and rebuild cache rows from scorecards.

Usage from the repository root::

    python -m migrations.extend_fc_statistics_cache --db ./cricket_sim.db
    python -m migrations.extend_fc_statistics_cache --db ./cricket_sim.db --apply

The apply path is idempotent: existing columns are never added again, and
every apply rebuilds existing tournament cache rows. Retrying after a partial
deployment is therefore safe.
"""

import argparse
import os
import sys

from sqlalchemy import inspect, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.exception_tracker import log_exception


TABLE_NAME = "tournament_player_stats_cache"

_COLUMNS = (
    ("ducks", "INTEGER DEFAULT 0"),
    ("ones", "INTEGER DEFAULT 0"),
    ("twos", "INTEGER DEFAULT 0"),
    ("threes", "INTEGER DEFAULT 0"),
    ("thirties", "INTEGER DEFAULT 0"),
    ("double_centuries", "INTEGER DEFAULT 0"),
    ("triple_centuries", "INTEGER DEFAULT 0"),
    ("best_match_bowling_wickets", "INTEGER DEFAULT 0"),
    ("best_match_bowling_runs", "INTEGER DEFAULT 0"),
    ("ten_wicket_matches", "INTEGER DEFAULT 0"),
    ("dot_balls_bowled", "INTEGER DEFAULT 0"),
    ("wickets_bowled", "INTEGER DEFAULT 0"),
    ("wickets_lbw", "INTEGER DEFAULT 0"),
    ("wides", "INTEGER DEFAULT 0"),
    ("noballs", "INTEGER DEFAULT 0"),
    ("byes", "INTEGER DEFAULT 0"),
    ("leg_byes", "INTEGER DEFAULT 0"),
)


def _existing_columns(conn):
    if conn.dialect.name == "sqlite":
        return {
            row[1]
            for row in conn.execute(text(f"PRAGMA table_info({TABLE_NAME})")).fetchall()
        }
    rows = conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_name = '{TABLE_NAME}'"
    )).fetchall()
    return {row[0] for row in rows}


def _safe_count(conn, table_name):
    if not inspect(conn).has_table(table_name):
        return 0
    return conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0


def _inspect_plan(conn):
    table_exists = inspect(conn).has_table(TABLE_NAME)
    existing = _existing_columns(conn) if table_exists else set()
    missing = [name for name, _ddl in _COLUMNS if name not in existing]
    return {
        "table_exists": table_exists,
        "existing_columns": existing,
        "missing_columns": missing,
        "cache_rows": _safe_count(conn, TABLE_NAME),
        "scorecard_rows": _safe_count(conn, "match_scorecards"),
        "fixture_rows": _safe_count(conn, "tournament_fixtures"),
    }


def _print_report(report, *, apply):
    print("=" * 72)
    print("FC Statistics Cache Migration")
    print("=" * 72)
    print(f"Mode:                 {'APPLY' if apply else 'DRY RUN'}")
    print(f"Cache table present:  {'yes' if report['table_exists'] else 'no'}")
    print(f"Cache rows:           {report['cache_rows']}")
    print(f"Scorecard rows:       {report['scorecard_rows']}")
    print(f"Tournament fixtures:  {report['fixture_rows']}")
    if report["missing_columns"]:
        print("Columns to add:")
        for name in report["missing_columns"]:
            print(f"  - {name}")
    else:
        print("Columns to add:        none (schema is current)")


def _rebuild_caches(db):
    """Rebuild every existing tournament/player cache pair from scorecards."""
    from database.models import (
        MatchScorecard,
        TournamentFixture,
        TournamentPlayerStatsCache,
    )
    from engine.tournament_engine import TournamentEngine

    tournament_ids = {
        row[0]
        for row in db.session.query(TournamentPlayerStatsCache.tournament_id).distinct().all()
    }
    tournament_ids.update(
        row[0]
        for row in db.session.query(TournamentFixture.tournament_id).distinct().all()
    )

    engine = TournamentEngine()
    rebuilt_rows = 0
    rebuilt_tournaments = 0
    for tournament_id in sorted(tournament_ids):
        player_ids = {
            row[0]
            for row in db.session.query(TournamentPlayerStatsCache.player_id)
            .filter(TournamentPlayerStatsCache.tournament_id == tournament_id)
            .distinct().all()
        }
        player_ids.update(
            row[0]
            for row in db.session.query(MatchScorecard.player_id)
            .join(TournamentFixture, TournamentFixture.match_id == MatchScorecard.match_id)
            .filter(TournamentFixture.tournament_id == tournament_id)
            .distinct().all()
        )
        if not player_ids:
            continue
        engine.rebuild_player_stats_cache(tournament_id, player_ids)
        rebuilt_tournaments += 1
        rebuilt_rows += len(player_ids)

    db.session.commit()
    return rebuilt_tournaments, rebuilt_rows


def run_migration(db, app, apply=False):
    """Inspect by default; mutate schema and rebuild caches only with apply."""
    with app.app_context():
        conn = db.engine.connect()
        try:
            report = _inspect_plan(conn)
            _print_report(report, apply=apply)

            if not report["table_exists"]:
                print(f"{TABLE_NAME} does not exist; nothing can be migrated.")
                return report

            if not apply:
                print()
                print("DRY RUN — no changes made. Re-run with --apply to execute.")
                return report

            try:
                for name, ddl in _COLUMNS:
                    if name in report["existing_columns"]:
                        continue
                    conn.execute(text(
                        f"ALTER TABLE {TABLE_NAME} ADD COLUMN {name} {ddl}"
                    ))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            rebuilt_tournaments, rebuilt_rows = _rebuild_caches(db)
            post_report = _inspect_plan(conn)
            if post_report["missing_columns"]:
                raise RuntimeError(
                    "Schema verification failed; still missing: "
                    + ", ".join(post_report["missing_columns"])
                )

            print()
            print(
                "APPLIED — schema current; rebuilt "
                f"{rebuilt_rows} player cache row(s) across "
                f"{rebuilt_tournaments} tournament(s)."
            )
            return post_report
        except Exception as exc:
            db.session.rollback()
            try:
                conn.rollback()
            except Exception:
                pass
            log_exception(
                exc,
                source="sqlite",
                context={"migration": "extend_fc_statistics_cache", "apply": apply},
            )
            print(f"FAILED — rolled back cache rebuild. Error: {exc}")
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Add FC statistics cache columns and rebuild tournament caches "
            "(dry-run by default)."
        )
    )
    parser.add_argument(
        "--db",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "cricket_sim.db",
        ),
        help="SQLite database path (default: repository cricket_sim.db).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply schema changes and rebuild caches (default: dry-run).",
    )
    args = parser.parse_args()

    db_path = os.path.abspath(args.db)
    if not os.path.isfile(db_path):
        parser.error(f"database does not exist: {db_path}")

    # Use the app's ORM against the explicit target while suppressing normal
    # production startup side effects (backup/scheduler/background workers).
    os.environ["SIMCRICKETX_SKIP_GLOBAL_APP"] = "1"
    os.environ["SIMCRICKETX_PRECHECK_RUNNING"] = "1"
    os.environ["SIMCRICKETX_TEST_MODE"] = "1"
    os.environ["SIMCRICKETX_TEST_DB_URI"] = f"sqlite:///{db_path}"

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app, apply=args.apply)
