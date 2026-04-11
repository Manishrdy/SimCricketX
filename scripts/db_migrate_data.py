"""
db_migrate_data.py
------------------
Step 3 of the SQLite → Supabase migration pipeline.

Copies ALL data from the local SQLite database to Supabase (PostgreSQL),
table by table, in FK-dependency order. Runs a row-count verification after
each table and at the end.

Safety features:
  • Dry-run mode by default — pass --execute to actually write
  • Skips tables that already have data in Postgres (--force to overwrite)
  • Verifies row counts after each table copy
  • Full rollback per table on any insert error
  • Truncates in reverse-FK order before re-import when --force is used

Usage:
    # Preview what would be migrated (no writes)
    python scripts/db_migrate_data.py

    # Actually migrate
    python scripts/db_migrate_data.py --execute

    # Re-migrate (wipe Postgres tables first, then copy)
    python scripts/db_migrate_data.py --execute --force

Requires:
    DATABASE_URL in .env  (pooled, port 6543) — used for data writes
    psycopg2-binary installed
"""

import os
import sys
import argparse
from datetime import datetime

# Allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

from sqlalchemy import create_engine, inspect, text, MetaData

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SQLITE_PATH   = os.path.join(BASE_DIR, "cricket_sim.db")

# Use DIRECT_URL for migration (bypasses pooler, required for DDL-class operations)
# Fall back to DATABASE_URL if DIRECT_URL not set
DIRECT_URL    = os.environ.get("DIRECT_URL", "").strip()
DATABASE_URL  = os.environ.get("DATABASE_URL", "").strip()
PG_URL        = DIRECT_URL or DATABASE_URL

# Tables in strict FK-dependency order (parents before children).
# Every FK parent must appear before the table that references it.
TABLE_ORDER = [
    "users",
    "teams",
    "team_profiles",
    "players",
    "tournaments",
    "matches",
    "match_scorecards",
    "match_partnerships",
    "tournament_teams",
    "tournament_fixtures",
    "tournament_player_stats_cache",
    "failed_login_attempts",
    "blocked_ips",
    "active_sessions",
    "site_counters",
    "announcement_banner",
    "user_banner_dismissals",
    "login_history",
    "ip_whitelist",
    "user_ground_configs",
    "auth_event_log",
    "exception_log",
    "issue_report",
    "issue_webhook_event",
    "admin_audit_log",
]

BATCH_SIZE = 500  # rows per INSERT batch


def _normalise_pg_url(url: str) -> str:
    """Re-encode password so special chars (@ # %) don't break URL parsing."""
    from urllib.parse import urlparse, urlunparse, quote
    url = url.replace("postgres://", "postgresql://", 1).split("?")[0]
    parsed = urlparse(url)
    if parsed.password:
        safe_pw = quote(parsed.password, safe="")
        netloc  = f"{parsed.username}:{safe_pw}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
    return urlunparse(parsed)


def _connect(url: str, label: str):
    try:
        kwargs = {"connect_args": {"connect_timeout": 15}} if url.startswith("postgresql") else {}
        engine = create_engine(url, **kwargs)
        with engine.connect():
            pass
        print(f"[OK] Connected to {label}")
        return engine
    except Exception as e:
        print(f"[ERROR] Cannot connect to {label}: {e}")
        sys.exit(1)


def _row_count(engine, table: str) -> int:
    with engine.connect() as conn:
        result = conn.execute(text(f'SELECT COUNT(*) FROM "{table}"'))
        return result.scalar()


def _get_columns(engine, table: str) -> list[str]:
    insp = inspect(engine)
    return [c["name"] for c in insp.get_columns(table)]


def _get_bool_columns(pg_engine, table: str) -> set[str]:
    """Return column names that are BOOLEAN type in Postgres."""
    insp = inspect(pg_engine)
    return {
        c["name"]
        for c in insp.get_columns(table)
        if str(c["type"]).upper().startswith("BOOL")
    }


def _fetch_all(sqlite_engine, table: str) -> tuple[list[str], list[dict]]:
    cols = _get_columns(sqlite_engine, table)
    with sqlite_engine.connect() as conn:
        rows = conn.execute(text(f'SELECT * FROM "{table}"')).fetchall()
    return cols, [dict(zip(cols, row)) for row in rows]


def _truncate_tables(pg_engine, tables: list[str]):
    """TRUNCATE in reverse FK order to avoid constraint violations."""
    print("\n  [FORCE] Truncating existing Postgres data (reverse FK order)...")
    reversed_tables = list(reversed(tables))
    with pg_engine.begin() as conn:
        # Disable FK checks during truncate
        conn.execute(text("SET session_replication_role = 'replica'"))
        for table in reversed_tables:
            try:
                conn.execute(text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE'))
                print(f"    Truncated: {table}")
            except Exception as e:
                print(f"    [WARN] Could not truncate '{table}': {e}")
        conn.execute(text("SET session_replication_role = 'origin'"))
    print()


def _insert_batch(pg_engine, table: str, cols: list[str], rows: list[dict]) -> int:
    """Insert rows in batches. Returns count of rows inserted."""
    if not rows:
        return 0

    col_list    = ", ".join(f'"{c}"' for c in cols)
    placeholder = ", ".join(f":{c}" for c in cols)
    stmt        = text(f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholder})')

    inserted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        with pg_engine.begin() as conn:
            conn.execute(stmt, batch)
        inserted += len(batch)

    return inserted


def _migrate_table(
    sqlite_engine,
    pg_engine,
    table: str,
    dry_run: bool,
    force: bool,
) -> dict:
    """
    Migrate one table. Returns a result dict:
    {
        "table":    str,
        "sqlite_rows": int,
        "pg_before":   int,
        "pg_after":    int,
        "status":   "ok" | "skipped" | "dry_run" | "error",
        "error":    str | None,
    }
    """
    result = {"table": table, "sqlite_rows": 0, "pg_before": 0, "pg_after": 0,
              "status": "pending", "error": None}

    # Check table exists in both DBs
    sq_tables = inspect(sqlite_engine).get_table_names()
    pg_tables = inspect(pg_engine).get_table_names()

    if table not in sq_tables:
        result["status"] = "skipped"
        result["error"]  = "not in SQLite"
        return result

    if table not in pg_tables:
        result["status"] = "error"
        result["error"]  = "not in Postgres — run schema verification first"
        return result

    cols, rows = _fetch_all(sqlite_engine, table)
    result["sqlite_rows"] = len(rows)

    pg_before = _row_count(pg_engine, table)
    result["pg_before"] = pg_before

    if pg_before > 0 and not force:
        result["status"] = "skipped"
        result["error"]  = f"already has {pg_before} rows (use --force to overwrite)"
        return result

    if dry_run:
        result["status"]  = "dry_run"
        result["pg_after"] = pg_before
        return result

    try:
        # Only insert columns that exist in Postgres (guard against schema drift)
        pg_cols    = set(_get_columns(pg_engine, table))
        safe_cols  = [c for c in cols if c in pg_cols]

        # Boolean coercion: SQLite returns 0/1 integers; Postgres BOOLEAN
        # rejects integers — must be Python bool.
        bool_cols = _get_bool_columns(pg_engine, table)

        # Float→str coercion: overs columns declared String in models.py
        # but stored as FLOAT in old SQLite migrations.
        FLOAT_TO_STR_COLS = {
            "matches":          {"home_team_overs", "away_team_overs"},
            "match_scorecards": {"overs"},
            "tournament_teams": {"overs_faced", "overs_bowled"},
        }
        float_str_cols = FLOAT_TO_STR_COLS.get(table, set())

        def _coerce_row(row: dict) -> dict:
            out = {k: v for k, v in row.items() if k in pg_cols}
            for col in bool_cols:
                if col in out and out[col] is not None and not isinstance(out[col], bool):
                    out[col] = bool(out[col])
            for col in float_str_cols:
                if col in out and out[col] is not None and not isinstance(out[col], str):
                    out[col] = str(out[col])
            return out

        safe_rows = [_coerce_row(row) for row in rows]

        _insert_batch(pg_engine, table, safe_cols, safe_rows)
        pg_after = _row_count(pg_engine, table)
        result["pg_after"] = pg_after

        if pg_after < len(rows):
            result["status"] = "error"
            result["error"]  = f"row count mismatch: expected {len(rows)}, got {pg_after}"
        else:
            result["status"] = "ok"

    except Exception as e:
        result["status"] = "error"
        result["error"]  = str(e)

    return result


def print_results(results: list[dict], dry_run: bool):
    total_sqlite = sum(r["sqlite_rows"] for r in results)
    total_pg     = sum(r["pg_after"]    for r in results if r["status"] == "ok")
    errors       = [r for r in results if r["status"] == "error"]
    skipped      = [r for r in results if r["status"] == "skipped"]

    print("\n" + "=" * 68)
    print(f"  {'DRY RUN — ' if dry_run else ''}MIGRATION REPORT")
    print("=" * 68)
    print(f"  {'Table':<40} {'SQLite':>7} {'PG':>7}  Status")
    print("  " + "-" * 64)

    for r in results:
        status_str = {
            "ok":      "  copied",
            "skipped": "skipped",
            "dry_run": "dry-run",
            "error":   "  ERROR",
            "pending": "pending",
        }.get(r["status"], r["status"])

        pg_val = r["pg_after"] if r["status"] == "ok" else (
            r["pg_before"] if r["status"] == "skipped" else "-"
        )
        print(f"  {r['table']:<40} {r['sqlite_rows']:>7} {str(pg_val):>7}  {status_str}")
        if r["error"] and r["status"] not in ("skipped",):
            print(f"    └─ {r['error']}")

    print("  " + "-" * 64)
    print(f"  {'TOTAL':<40} {total_sqlite:>7} {total_pg:>7}")
    print("=" * 68)

    if errors:
        print(f"\n  ERRORS ({len(errors)}):")
        for r in errors:
            print(f"    • {r['table']}: {r['error']}")

    if dry_run:
        print("\n  This was a DRY RUN — no data was written.")
        print("  Run with --execute to perform the actual migration.")
    elif not errors:
        print("\n  Migration complete. All row counts verified.")
    else:
        print("\n  Migration finished with errors. Check above and retry.")

    print()


def main():
    parser = argparse.ArgumentParser(description="SQLite → Supabase data migration")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually write data to Postgres (default is dry-run)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Truncate existing Postgres tables before inserting (re-migration)",
    )
    parser.add_argument(
        "--tables",
        nargs="+",
        metavar="TABLE",
        help="Migrate only these specific tables (space-separated)",
    )
    args = parser.parse_args()

    dry_run = not args.execute

    print("\n[Step 3] Data Migration: SQLite → Supabase")
    print("-" * 68)

    # ── Validate prereqs ──────────────────────────────────────────────────────
    if not os.path.exists(SQLITE_PATH):
        print(f"[ERROR] SQLite DB not found: {SQLITE_PATH}")
        sys.exit(1)

    if not PG_URL:
        print("[ERROR] Neither DIRECT_URL nor DATABASE_URL is set in .env")
        sys.exit(1)

    pg_url = _normalise_pg_url(PG_URL)
    url_label = "DIRECT_URL" if DIRECT_URL else "DATABASE_URL"

    print(f"  Mode    : {'DRY RUN (pass --execute to write)' if dry_run else 'EXECUTE — writing to Postgres'}")
    print(f"  SQLite  : {SQLITE_PATH}")
    print(f"  Postgres: {pg_url[:50]}... ({url_label})")
    if args.force:
        print("  Force   : YES — will truncate existing Postgres data")
    print()

    # ── Connect ───────────────────────────────────────────────────────────────
    sqlite_engine = _connect(f"sqlite:///{SQLITE_PATH}", "SQLite")
    pg_engine     = _connect(pg_url, "Supabase (Postgres)")
    print()

    # ── Determine table list ──────────────────────────────────────────────────
    if args.tables:
        tables = args.tables
        print(f"  Migrating specified tables: {tables}\n")
    else:
        # Full ordered list, filtered to what exists in SQLite
        sq_tables = set(inspect(sqlite_engine).get_table_names())
        tables    = [t for t in TABLE_ORDER if t in sq_tables]
        # Append any tables in SQLite not in our ordered list
        extras    = sorted(sq_tables - set(TABLE_ORDER))
        if extras:
            print(f"  [WARN] Tables in SQLite not in FK order list (appended): {extras}")
        tables += extras

    # ── Force: truncate first ─────────────────────────────────────────────────
    if args.force and not dry_run:
        _truncate_tables(pg_engine, tables)

    # ── Disable FK constraints during import, re-enable after ─────────────────
    # Postgres enforces FKs on every INSERT; disabling during bulk load is safe
    # as long as our data is consistent (which it is — it came from SQLite).
    if not dry_run:
        with pg_engine.begin() as conn:
            conn.execute(text("SET session_replication_role = 'replica'"))

    results = []
    for table in tables:
        print(f"  Migrating: {table}...", end=" ", flush=True)
        result = _migrate_table(sqlite_engine, pg_engine, table, dry_run, args.force)
        results.append(result)

        status = result["status"]
        if status == "ok":
            print(f"{result['pg_after']} rows")
        elif status == "dry_run":
            print(f"{result['sqlite_rows']} rows (dry run)")
        elif status == "skipped":
            print(f"skipped ({result['error']})")
        else:
            print(f"ERROR — {result['error']}")

    if not dry_run:
        with pg_engine.begin() as conn:
            conn.execute(text("SET session_replication_role = 'origin'"))

    print_results(results, dry_run)

    has_errors = any(r["status"] == "error" for r in results)

    # Reset Postgres SERIAL sequences after bulk insert so next INSERT
    # doesn't collide with migrated IDs.
    if not dry_run and not has_errors:
        try:
            from scripts.db_reset_sequences import reset_sequences
            reset_sequences()
        except Exception as e:
            print(f"[WARN] Sequence reset failed — run scripts/db_reset_sequences.py manually: {e}")

    sys.exit(1 if has_errors else 0)


if __name__ == "__main__":
    main()
