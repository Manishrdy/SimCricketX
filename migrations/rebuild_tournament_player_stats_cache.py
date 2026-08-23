"""
Rebuild tournament_player_stats_cache to match TournamentPlayerStatsCache.

Drops leftover `category` / `data_json` / `updated_at` columns that the
current ORM no longer maps. Those leftover NOT NULL columns make every
cache INSERT fail (resimulate, first standings cache write).

This module is the Flask/precheck entry point. The rebuild itself lives in
`scripts/migrate_tournament_player_stats_cache.py` (stdlib, copyable to prod).

Startup precheck runs this in DRY-RUN mode (report only). Production apply
is explicit:

    python -m migrations.rebuild_tournament_player_stats_cache
    python -m migrations.rebuild_tournament_player_stats_cache --apply

Or the standalone prod script:

    python3 scripts/migrate_tournament_player_stats_cache.py --db /path/to/cricket_sim.db
    python3 scripts/migrate_tournament_player_stats_cache.py --db /path/to/cricket_sim.db --apply --backup
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.migrate_tournament_player_stats_cache import (  # noqa: E402
    _open,
    inspect_schema,
    print_report,
    run_apply,
)
from utils.exception_tracker import log_exception


def _sqlite_path(engine) -> str:
    url = engine.url
    if url.drivername != "sqlite":
        return ""
    return url.database or ""


def run_migration(db, app, apply=False):
    """Inspect (default) or rebuild the cache table.

    Precheck calls this without `apply`, so app boot never mutates the
    table. Operators pass --apply on the CLI / standalone script.
    """
    with app.app_context():
        if db.engine.dialect.name != "sqlite":
            print(
                "[Migration] rebuild_tournament_player_stats_cache: dialect "
                f"'{db.engine.dialect.name}' — skipping (run native ALTER manually)."
            )
            return

        db_path = _sqlite_path(db.engine)
        if not db_path or db_path == ":memory:":
            print(
                "[Migration] rebuild_tournament_player_stats_cache: "
                "skipped (in-memory / unnamed SQLite URI)."
            )
            return

        conn = _open(db_path)
        try:
            snapshot = inspect_schema(conn)
            print_report(
                snapshot,
                "rebuild_tournament_player_stats_cache (precheck)",
            )
            if not apply:
                print(
                    "[Migration] rebuild_tournament_player_stats_cache: "
                    "DRY RUN — pass --apply to rebuild."
                )
                return
            rc = run_apply(conn)
            if rc != 0:
                raise RuntimeError(
                    f"rebuild_tournament_player_stats_cache apply failed (rc={rc})"
                )
        except Exception as exc:
            log_exception(
                exc,
                source="sqlite",
                context={"migration": "rebuild_tournament_player_stats_cache"},
            )
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Report or rebuild tournament_player_stats_cache."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Rebuild the table (default: dry-run).",
    )
    args = parser.parse_args()

    os.environ.setdefault("SIMCRICKETX_SKIP_GLOBAL_APP", "1")
    os.environ.setdefault("SIMCRICKETX_PRECHECK_RUNNING", "1")
    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app, apply=args.apply)
