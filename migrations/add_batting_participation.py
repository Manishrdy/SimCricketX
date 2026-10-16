"""Add the nullable MatchScorecard.did_bat participation marker.

CLI is read-only by default (or with --dry-run); --apply adds the column:

    python -m migrations.add_batting_participation --db /path/to/cricket_sim.db
    python -m migrations.add_batting_participation --db /path/to/cricket_sim.db --apply

Historical rows remain NULL. Readers retain the legacy activity fallback,
but cannot distinguish an all-zero not-out from an initialized DNB row.
No speculative backfill or career/cache changes are made. Re-running is safe.
The CLI opens the selected SQLite file directly, without app startup jobs.
"""

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3


def migrate_connection(conn, apply=False):
    """Inspect or migrate one SQLite DB-API connection; return the plan."""
    if apply:
        conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(match_scorecards)")}
        if not columns:
            raise RuntimeError("match_scorecards is missing; run prerequisite migrations first")
        required = {"record_type", "runs", "balls", "is_out"}
        missing = required - columns
        if missing:
            raise RuntimeError("match_scorecards lacks required columns: " + ", ".join(sorted(missing)))
        unknown = "did_bat IS NULL" if "did_bat" in columns else "1 = 1"
        # Exclude super overs when the earlier discriminator is available.
        ordinary = "COALESCE(is_super_over, 0) = 0" if "is_super_over" in columns else "1 = 1"
        ambiguous = conn.execute(
            "SELECT COUNT(*) FROM match_scorecards WHERE record_type = 'batting' "
            "AND COALESCE(runs, 0) = 0 AND COALESCE(balls, 0) = 0 "
            "AND COALESCE(is_out, 0) = 0 AND " + unknown + " AND " + ordinary
        ).fetchone()[0]
        report = {"column_missing": "did_bat" not in columns,
                  "ambiguous_legacy_rows": ambiguous}
        print("Mode: " + ("APPLY" if apply else "DRY RUN"))
        print("did_bat column: " + ("will add" if report["column_missing"] else "already present"))
        print("Ambiguous legacy 0* / DNB rows left unknown: " + str(ambiguous))
        if apply:
            if report["column_missing"]:
                conn.execute("ALTER TABLE match_scorecards ADD COLUMN did_bat BOOLEAN")
            conn.commit()
            print("Applied. Historical data and caches were not rewritten.")
        else:
            print("No changes made. Use --apply to execute.")
        return report
    except Exception:
        if apply:
            conn.rollback()
        raise


def run_migration(db, app, apply=False):
    """Programmatic dry run by default, using the configured database."""
    with app.app_context():
        if db.engine.dialect.name != "sqlite":
            raise RuntimeError("This migration supports SQLite databases only")
        conn = db.engine.raw_connection()
        try:
            return migrate_connection(conn, apply=apply)
        finally:
            conn.close()


def ensure_schema(db, app):
    """Idempotent schema step for the existing startup migration registry."""
    return run_migration(db, app, apply=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Existing SQLite database path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Inspect only (default)")
    mode.add_argument("--apply", action="store_true", help="Add the column; leave legacy rows unknown")
    args = parser.parse_args(argv)
    path = Path(args.db).expanduser().resolve()
    # mode=rw refuses to accidentally create a database at a mistyped path;
    # mode=ro makes the dry-run guarantee independent of application code.
    uri = path.as_uri() + ("?mode=rw" if args.apply else "?mode=ro")
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            migrate_connection(conn, apply=args.apply)
    except (sqlite3.Error, RuntimeError) as exc:
        parser.exit(1, "Migration failed: " + str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
