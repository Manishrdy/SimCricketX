#!/usr/bin/env python3
"""
Standalone Migration Runner — Team / Match FK ON DELETE Actions
================================================================

Runs the schema-rebuild that adds `ON DELETE` actions to foreign-keys on
`matches`, `tournament_teams` and `tournament_fixtures` (see
`migrations/add_team_match_fk_actions.py` for the action matrix and rationale).

This script is **pure stdlib** — no Flask, no SQLAlchemy. You can copy it
onto the prod box, point it at the live DB file, and run it before the
next app deploy. It is fully idempotent: re-running on a migrated DB is a
no-op.

Usage
-----
    # 1. Dry-run (default): inspect only, never writes.
    python3 scripts/migrate_team_match_fk_actions.py --db /path/to/cricket_sim.db

    # 2. Apply against a *copy* first (recommended pre-prod sanity check).
    cp /path/to/cricket_sim.db /tmp/sim_test.db
    python3 scripts/migrate_team_match_fk_actions.py --db /tmp/sim_test.db --apply

    # 3. Apply against prod with a timestamped backup.
    python3 scripts/migrate_team_match_fk_actions.py --db /path/to/cricket_sim.db --apply --backup

The --apply path takes an exclusive lock on the DB (BEGIN IMMEDIATE) so
concurrent writers will get SQLITE_BUSY rather than corrupt state. Stop the
app process before running --apply.
"""

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
from typing import Dict, List, Tuple


# (table, column) → desired ON DELETE action. MUST stay in lock-step with
# migrations/add_team_match_fk_actions.py — this script intentionally
# duplicates the matrix so it can run without importing the project package.
TARGETS: Dict[str, Dict[str, str]] = {
    "matches": {
        "home_team_id":        "SET NULL",
        "away_team_id":        "SET NULL",
        "winner_team_id":      "SET NULL",
        "toss_winner_team_id": "SET NULL",
        "tournament_id":       "SET NULL",
    },
    "tournament_teams": {
        "team_id":             "CASCADE",
    },
    "tournament_fixtures": {
        "home_team_id":        "CASCADE",
        "away_team_id":        "CASCADE",
        "winner_team_id":      "SET NULL",
        "match_id":            "SET NULL",
    },
}


# ─── Inspection helpers ─────────────────────────────────────────────────────


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _current_fk_actions(conn: sqlite3.Connection, table: str) -> Dict[str, str]:
    """Map column → current ON DELETE action ('' if NO ACTION)."""
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    # row: (id, seq, table, from, to, on_update, on_delete, match)
    return {r[3]: (r[6] or "").upper() for r in rows}


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _user_indexes(conn: sqlite3.Connection, table: str) -> List[Tuple[str, str]]:
    """User-defined indexes (auto-indexes have NULL `sql` and SQLite recreates
    them automatically as part of CREATE TABLE)."""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,)
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


# ─── Rebuild logic ──────────────────────────────────────────────────────────


def _inject_on_delete(create_sql: str, col: str, action: str) -> str:
    """Add `ON DELETE <action>` to the FK clause for `col` in `create_sql`.

    Handles both forms emitted by SQLAlchemy / SQLite:
      A. column-inline:  `<col> TYPE REFERENCES <ref> (<refcol>)`
      B. table-clause:   `FOREIGN KEY(<col>) REFERENCES <ref> (<refcol>)`

    No-op when the FK already has any `ON DELETE` clause.
    """
    col_q = re.escape(col)

    pat_b = re.compile(
        rf"(FOREIGN\s+KEY\s*\(\s*{col_q}\s*\)\s+REFERENCES\s+\w+\s*\(\s*\w+\s*\))(?!\s+ON\s+DELETE)",
        re.IGNORECASE,
    )
    new_sql, n = pat_b.subn(rf"\1 ON DELETE {action}", create_sql)
    if n:
        return new_sql

    pat_a = re.compile(
        rf"(\b{col_q}\b\s+\w+(?:\s*\(\s*\d+\s*\))?(?:\s+NOT\s+NULL)?(?:\s+DEFAULT\s+\S+)?\s+REFERENCES\s+\w+\s*\(\s*\w+\s*\))(?!\s+ON\s+DELETE)",
        re.IGNORECASE,
    )
    new_sql, _n = pat_a.subn(rf"\1 ON DELETE {action}", create_sql)
    return new_sql


def _rebuild_table(conn: sqlite3.Connection, table: str, wanted: Dict[str, str]) -> None:
    create_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not create_sql_row:
        return
    create_sql = create_sql_row[0]

    new_sql = create_sql
    for col, action in wanted.items():
        new_sql = _inject_on_delete(new_sql, col, action)

    new_table = f"{table}_new"
    new_sql = re.sub(
        rf"^CREATE\s+TABLE\s+{re.escape(table)}\b",
        f"CREATE TABLE {new_table}",
        new_sql,
        count=1,
        flags=re.IGNORECASE,
    )

    indexes = _user_indexes(conn, table)
    cols_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    col_names = [c[1] for c in cols_info]
    cols_csv = ", ".join(col_names)

    conn.execute(new_sql)
    conn.execute(f"INSERT INTO {new_table} ({cols_csv}) SELECT {cols_csv} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {new_table} RENAME TO {table}")

    for _idx_name, idx_sql in indexes:
        try:
            conn.execute(idx_sql)
        except sqlite3.OperationalError:
            # Auto-recreated unique indexes etc. may already exist; benign.
            pass


# ─── Reporting ──────────────────────────────────────────────────────────────


def _format_action(action: str) -> str:
    return action if action and action != "NO ACTION" else "(none)"


def _print_report(conn: sqlite3.Connection, header: str) -> Dict[str, Dict[str, str]]:
    """Print per-table FK action status. Returns the snapshot."""
    print(f"\n=== {header} ===")
    print(f"{'Table':24} {'Column':22} {'Current':12} {'Desired':12} {'Status'}")
    print("-" * 86)
    snapshot: Dict[str, Dict[str, str]] = {}
    needs_change = False
    for table, wanted in TARGETS.items():
        if not _table_exists(conn, table):
            print(f"{table:24} (table missing — skipping)")
            continue
        actual = _current_fk_actions(conn, table)
        snapshot[table] = actual
        col_names = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, want in wanted.items():
            if col not in col_names:
                print(f"{table:24} {col:22} (column missing — skipping)")
                continue
            cur = actual.get(col, "")
            ok = cur == want.upper()
            if not ok:
                needs_change = True
            status = "✓ already set" if ok else "→ will update"
            print(f"{table:24} {col:22} {_format_action(cur):12} {want:12} {status}")
    print("-" * 86)
    if not needs_change:
        print("All target FK actions already in place — migration would be a no-op.")
    else:
        print("Migration would rebuild the relevant tables.")
    return snapshot


def _print_row_counts(conn: sqlite3.Connection, label: str) -> Dict[str, int]:
    counts = {}
    print(f"\n--- Row counts ({label}) ---")
    for table in TARGETS:
        if _table_exists(conn, table):
            n = _row_count(conn, table)
            counts[table] = n
            print(f"  {table:24} {n}")
    return counts


def _print_indexes_and_uniques(conn: sqlite3.Connection) -> None:
    print("\n--- Indexes and UNIQUE constraints (will be preserved) ---")
    for table in TARGETS:
        if not _table_exists(conn, table):
            continue
        idxs = _user_indexes(conn, table)
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        sql_text = sql[0] if sql else ""
        unique_clauses = re.findall(r"(?:CONSTRAINT\s+\w+\s+)?UNIQUE\s*\([^)]+\)",
                                    sql_text, re.IGNORECASE)
        print(f"  {table}:")
        if idxs:
            for n, _s in idxs:
                print(f"    index: {n}")
        if unique_clauses:
            for u in unique_clauses:
                print(f"    {u}")
        if not idxs and not unique_clauses:
            print("    (none)")


def _verify_post_apply(conn: sqlite3.Connection,
                        before_counts: Dict[str, int]) -> bool:
    """Return True iff every targeted FK has the desired action AND row counts
    are unchanged."""
    after_counts = _print_row_counts(conn, "after migration")
    print("\n--- FK action verification ---")
    all_ok = True
    for table, wanted in TARGETS.items():
        if not _table_exists(conn, table):
            continue
        actual = _current_fk_actions(conn, table)
        for col, want in wanted.items():
            cur = actual.get(col, "")
            ok = cur == want.upper()
            mark = "✓" if ok else "✗"
            if not ok:
                all_ok = False
            print(f"  {mark} {table}.{col}: {cur or '(none)'} (wanted {want})")

    print("\n--- Row count verification ---")
    for t, before in before_counts.items():
        after = after_counts.get(t, -1)
        ok = before == after
        if not ok:
            all_ok = False
        print(f"  {'✓' if ok else '✗'} {t}: {before} → {after}")
    return all_ok


# ─── Main ───────────────────────────────────────────────────────────────────


def _backup_db(src: str) -> str:
    ts = time.strftime("%Y%m%dT%H%M%S")
    dst = f"{src}.bak.{ts}"
    if os.path.exists(dst):
        # Vanishingly unlikely with second-resolution timestamp + sequential runs.
        raise FileExistsError(f"backup target already exists: {dst}")
    shutil.copy2(src, dst)
    return dst


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="Path to the SQLite DB file.")
    parser.add_argument("--apply", action="store_true",
                        help="Perform the migration. Without this flag the script "
                             "only inspects and reports.")
    parser.add_argument("--backup", action="store_true",
                        help="With --apply, copy the DB to <path>.bak.<timestamp> "
                             "before mutating. Strongly recommended for prod.")
    args = parser.parse_args()

    if not os.path.isfile(args.db):
        print(f"[ERROR] DB file not found: {args.db}", file=sys.stderr)
        return 2

    if args.backup and not args.apply:
        print("[NOTE] --backup is meaningless without --apply; ignoring.")

    print(f"DB: {args.db}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")

    # Open with FK enforcement OFF so the integrity-snapshot phase doesn't
    # trip cascade actions. We re-enable before exit.
    conn = sqlite3.connect(args.db)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")

        before_counts = _print_row_counts(conn, "current")
        _print_indexes_and_uniques(conn)
        _print_report(conn, "Foreign-key status (BEFORE)")

        if not args.apply:
            print("\n[DRY-RUN] No changes written. Re-run with --apply to migrate.")
            return 0

        # ── APPLY ──
        if args.backup:
            backup_path = _backup_db(args.db)
            print(f"\n[BACKUP] Wrote snapshot: {backup_path}")

        # Take an immediate write lock — concurrent writers see SQLITE_BUSY
        # rather than racing into the rebuild.
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            print(f"\n[ERROR] Could not acquire write lock — is the app running? {exc}",
                  file=sys.stderr)
            return 3

        rebuilt = []
        try:
            for table, wanted in TARGETS.items():
                if not _table_exists(conn, table):
                    continue
                col_names = {r[1] for r in conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()}
                applicable = {c: a for c, a in wanted.items() if c in col_names}
                if not applicable:
                    continue
                actions = _current_fk_actions(conn, table)
                if all(actions.get(c, "") == a.upper() for c, a in applicable.items()):
                    print(f"[SKIP] {table}: already correct")
                    continue
                print(f"[REBUILD] {table}")
                _rebuild_table(conn, table, applicable)
                rebuilt.append(table)

            conn.execute("COMMIT")
            print(f"\n[OK] Committed. Rebuilt {len(rebuilt)} table(s): "
                  f"{', '.join(rebuilt) if rebuilt else '(none)'}")
        except Exception:
            conn.execute("ROLLBACK")
            print("\n[ROLLBACK] Migration failed; DB left unchanged.")
            raise

        # Re-enable FKs and verify.
        conn.execute("PRAGMA foreign_keys = ON")
        all_ok = _verify_post_apply(conn, before_counts)

        # Sanity: ask SQLite itself whether all FKs hold under the new actions.
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            print(f"\n[WARN] foreign_key_check returned {len(violations)} "
                  f"violation row(s) — first 10:")
            for v in violations[:10]:
                print(f"   {v}")
            print("These predate this migration (the rebuild only changed actions, "
                  "not data) but should be cleaned up.")
        else:
            print("\n[OK] foreign_key_check: no violations.")

        if not all_ok:
            print("\n[WARN] Verification reported issues above. Review carefully.")
            return 1

        print("\n[DONE] Migration applied and verified successfully.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
