#!/usr/bin/env python3
"""
Standalone Migration — Rebuild tournament_player_stats_cache
============================================================

The live SQLite table on some DBs was created from an older cache design:

    category VARCHAR(20) NOT NULL
    data_json TEXT NOT NULL
    updated_at DATETIME

`ensure_schema` later ALTER-added the denormalized stat columns the ORM
uses today, but it never drops leftover columns. The current
`TournamentPlayerStatsCache` model does not map `category` / `data_json`,
so every INSERT (resimulate, first cache write) fails:

    IntegrityError: NOT NULL constraint failed: tournament_player_stats_cache.category

This script rebuilds the table to match the current model:

    one row per (tournament_id, player_id)
    no ghost columns
    UNIQUE (tournament_id, player_id)
    INDEX on tournament_id
    team_id backfilled from players.team_id when NULL

It is **pure stdlib** — no Flask, no SQLAlchemy. Copy it onto the prod box
and point it at the live DB file. Fully idempotent: a migrated DB is a no-op.

Usage
-----
    # Step 1 — Dry-run (default): inspect only, never writes.
    python3 scripts/migrate_tournament_player_stats_cache.py --db /path/to/cricket_sim.db

    # Step 2 — Apply against a *copy* first (recommended pre-prod check).
    cp /path/to/cricket_sim.db /tmp/sim_test.db
    python3 scripts/migrate_tournament_player_stats_cache.py --db /tmp/sim_test.db --apply

    # Step 2 — Apply against prod with a timestamped backup.
    python3 scripts/migrate_tournament_player_stats_cache.py --db /path/to/cricket_sim.db --apply --backup

Stop the app process before running --apply. The apply path takes an
exclusive lock (BEGIN IMMEDIATE) so concurrent writers get SQLITE_BUSY
rather than corrupt state.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple


TABLE = "tournament_player_stats_cache"
NEW_TABLE = "tournament_player_stats_cache_new"

GHOST_COLUMNS = ("category", "data_json", "updated_at")

UNIQUE_NAME = "uq_tournament_player_cache"
TOURNAMENT_INDEX = "ix_tournament_player_cache_tournament_id"

# Column order matches database.models.TournamentPlayerStatsCache.
# Values are the SQL DEFAULT used when the live table is missing the column.
WANTED_COLUMNS: List[Tuple[str, str]] = [
    ("id", "INTEGER NOT NULL"),
    ("tournament_id", "INTEGER NOT NULL"),
    ("player_id", "INTEGER NOT NULL"),
    ("team_id", "INTEGER NOT NULL"),
    ("matches_played", "INTEGER DEFAULT 0"),
    ("innings_batted", "INTEGER DEFAULT 0"),
    ("runs_scored", "INTEGER DEFAULT 0"),
    ("balls_faced", "INTEGER DEFAULT 0"),
    ("fours", "INTEGER DEFAULT 0"),
    ("sixes", "INTEGER DEFAULT 0"),
    ("not_outs", "INTEGER DEFAULT 0"),
    ("highest_score", "INTEGER DEFAULT 0"),
    ("fifties", "INTEGER DEFAULT 0"),
    ("centuries", "INTEGER DEFAULT 0"),
    ("batting_average", "FLOAT DEFAULT 0.0"),
    ("batting_strike_rate", "FLOAT DEFAULT 0.0"),
    ("innings_bowled", "INTEGER DEFAULT 0"),
    ("overs_bowled", "VARCHAR(10) DEFAULT '0.0'"),
    ("runs_conceded", "INTEGER DEFAULT 0"),
    ("wickets_taken", "INTEGER DEFAULT 0"),
    ("maidens", "INTEGER DEFAULT 0"),
    ("best_bowling_wickets", "INTEGER DEFAULT 0"),
    ("best_bowling_runs", "INTEGER DEFAULT 0"),
    ("five_wicket_hauls", "INTEGER DEFAULT 0"),
    ("bowling_average", "FLOAT DEFAULT 0.0"),
    ("bowling_economy", "FLOAT DEFAULT 0.0"),
    ("bowling_strike_rate", "FLOAT DEFAULT 0.0"),
    ("catches", "INTEGER DEFAULT 0"),
    ("run_outs", "INTEGER DEFAULT 0"),
    ("stumpings", "INTEGER DEFAULT 0"),
    # FC-native statistics (migrations/extend_fc_statistics_cache.py). These
    # MUST be listed here: this script rebuilds the table from scratch, so a
    # column it does not know about is silently dropped, and every cache read
    # then fails until an app restart lets the precheck ALTER it back.
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
]

WANTED_COL_NAMES = [name for name, _ in WANTED_COLUMNS]

# Fallback literals when a wanted column is absent on the live table.
_SELECT_DEFAULTS: Dict[str, str] = {
    "matches_played": "0",
    "innings_batted": "0",
    "runs_scored": "0",
    "balls_faced": "0",
    "fours": "0",
    "sixes": "0",
    "not_outs": "0",
    "highest_score": "0",
    "fifties": "0",
    "centuries": "0",
    "batting_average": "0.0",
    "batting_strike_rate": "0.0",
    "innings_bowled": "0",
    "overs_bowled": "'0.0'",
    "runs_conceded": "0",
    "wickets_taken": "0",
    "maidens": "0",
    "best_bowling_wickets": "0",
    "best_bowling_runs": "0",
    "five_wicket_hauls": "0",
    "bowling_average": "0.0",
    "bowling_economy": "0.0",
    "bowling_strike_rate": "0.0",
    "catches": "0",
    "run_outs": "0",
    "stumpings": "0",
    "ducks": "0",
    "ones": "0",
    "twos": "0",
    "threes": "0",
    "thirties": "0",
    "double_centuries": "0",
    "triple_centuries": "0",
    "best_match_bowling_wickets": "0",
    "best_match_bowling_runs": "0",
    "ten_wicket_matches": "0",
    "dot_balls_bowled": "0",
    "wickets_bowled": "0",
    "wickets_lbw": "0",
    "wides": "0",
    "noballs": "0",
    "byes": "0",
    "leg_byes": "0",
}


# Built from WANTED_COLUMNS rather than spelled out a second time. The two
# used to be separate hand-maintained lists, and when the FC statistics
# columns were added to the model neither was updated — so this script
# rebuilt the table without them and every cache read failed until an app
# restart ALTERed them back. One list, one source of truth.
_COLUMN_DDL = ",\n    ".join(f"{name} {decl}" for name, decl in WANTED_COLUMNS)

NEW_TABLE_SQL = f"""
CREATE TABLE {NEW_TABLE} (
    {_COLUMN_DDL},
    PRIMARY KEY (id),
    CONSTRAINT {UNIQUE_NAME} UNIQUE (tournament_id, player_id),
    FOREIGN KEY(tournament_id) REFERENCES tournaments (id),
    FOREIGN KEY(player_id) REFERENCES players (id),
    FOREIGN KEY(team_id) REFERENCES teams (id)
)
"""


# ─── Inspection ─────────────────────────────────────────────────────────────


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _unique_covers(conn: sqlite3.Connection, table: str, cols: Sequence[str]) -> bool:
    """True if a UNIQUE index/constraint exists on exactly `cols` (order-insensitive)."""
    wanted = set(cols)
    for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
        # idx: (seq, name, unique, origin, partial)
        if not idx[2]:
            continue
        # index_info row: (seqno, cid, name)
        indexed = {
            r[2] for r in conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()
        }
        if indexed == wanted:
            return True
    return False


def _has_named_index(conn: sqlite3.Connection, table: str, name: str) -> bool:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND name=?",
        (table, name),
    ).fetchall()
    return bool(rows)


def inspect_schema(conn: sqlite3.Connection) -> Dict:
    """Return a structured snapshot of how far the live table has drifted."""
    exists = _table_exists(conn, TABLE)
    if not exists:
        return {
            "table_exists": False,
            "columns": [],
            "ghost_columns": [],
            "missing_columns": list(WANTED_COL_NAMES),
            "has_unique": False,
            "has_tournament_index": False,
            "row_count": 0,
            "duplicate_groups": 0,
            "duplicate_extra_rows": 0,
            "null_team_ids": 0,
            "orphan_player_rows": 0,
            "needs_rebuild": False,
            "skip_reason": "table missing — create_all / ensure_schema will create the current schema",
        }

    cols = _columns(conn, TABLE)
    colset = set(cols)
    ghosts = [c for c in GHOST_COLUMNS if c in colset]
    missing = [c for c in WANTED_COL_NAMES if c not in colset]
    has_unique = _unique_covers(conn, TABLE, ("tournament_id", "player_id"))
    has_index = _has_named_index(conn, TABLE, TOURNAMENT_INDEX)
    row_count = _row_count(conn, TABLE)

    dup = conn.execute(
        f"""
        SELECT COUNT(*), COALESCE(SUM(cnt - 1), 0)
        FROM (
            SELECT tournament_id, player_id, COUNT(*) AS cnt
            FROM {TABLE}
            GROUP BY tournament_id, player_id
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()
    duplicate_groups = dup[0] or 0
    duplicate_extra_rows = dup[1] or 0

    if "team_id" in colset:
        null_team_ids = conn.execute(
            f"SELECT COUNT(*) FROM {TABLE} WHERE team_id IS NULL"
        ).fetchone()[0]
    else:
        null_team_ids = row_count

    orphan_player_rows = conn.execute(
        f"""
        SELECT COUNT(*) FROM {TABLE} c
        LEFT JOIN players p ON p.id = c.player_id
        WHERE p.id IS NULL
        """
    ).fetchone()[0]

    needs_rebuild = bool(
        ghosts
        or missing
        or not has_unique
        or not has_index
        or null_team_ids
        or duplicate_groups
    )

    return {
        "table_exists": True,
        "columns": cols,
        "ghost_columns": ghosts,
        "missing_columns": missing,
        "has_unique": has_unique,
        "has_tournament_index": has_index,
        "row_count": row_count,
        "duplicate_groups": duplicate_groups,
        "duplicate_extra_rows": duplicate_extra_rows,
        "null_team_ids": null_team_ids,
        "orphan_player_rows": orphan_player_rows,
        "needs_rebuild": needs_rebuild,
        "skip_reason": None,
    }


def print_report(snapshot: Dict, header: str) -> None:
    print(f"\n=== {header} ===")
    if not snapshot["table_exists"]:
        print(f"  table: {TABLE} — ABSENT")
        print(f"  {snapshot['skip_reason']}")
        return

    def _yn(ok: bool) -> str:
        return "yes" if ok else "NO"

    print(f"  rows:                  {snapshot['row_count']}")
    print(f"  ghost columns:         {snapshot['ghost_columns'] or '(none)'}")
    print(f"  missing model columns: {snapshot['missing_columns'] or '(none)'}")
    print(f"  unique (tournament, player): {_yn(snapshot['has_unique'])}")
    print(f"  index {TOURNAMENT_INDEX}: {_yn(snapshot['has_tournament_index'])}")
    print(f"  NULL team_id rows:     {snapshot['null_team_ids']}")
    print(f"  orphan player_id rows: {snapshot['orphan_player_rows']}")
    print(
        f"  duplicate groups:      {snapshot['duplicate_groups']} "
        f"({snapshot['duplicate_extra_rows']} extra row(s) to collapse)"
    )
    if snapshot["needs_rebuild"]:
        print("  status:                → will rebuild")
    else:
        print("  status:                ✓ already matches the current model")


# ─── Rebuild ────────────────────────────────────────────────────────────────


def _select_expr(col: str, existing: Sequence[str]) -> str:
    if col == "id":
        return "id"
    if col == "tournament_id":
        return "tournament_id"
    if col == "player_id":
        return "player_id"
    if col == "team_id":
        if "team_id" in existing:
            return (
                "COALESCE(team_id, "
                "(SELECT p.team_id FROM players p WHERE p.id = player_id))"
            )
        return "(SELECT p.team_id FROM players p WHERE p.id = player_id)"
    if col in existing:
        return f"COALESCE({col}, {_SELECT_DEFAULTS[col]})"
    return _SELECT_DEFAULTS[col]


def _order_expr(existing: Sequence[str]) -> str:
    parts = []
    for col in ("matches_played", "runs_scored", "wickets_taken"):
        if col in existing:
            parts.append(f"COALESCE({col}, 0) DESC")
    parts.append("id ASC")
    return ", ".join(parts)


def apply_rebuild(conn: sqlite3.Connection) -> Dict:
    """Rebuild the table in the current transaction. Caller owns COMMIT.

    Returns a small result dict (copied, dropped_orphans, dropped_null_team).
    No-op if inspect_schema says nothing to do.
    """
    before = inspect_schema(conn)
    if not before["table_exists"] or not before["needs_rebuild"]:
        return {
            "rebuilt": False,
            "copied": before.get("row_count", 0),
            "dropped_orphans": 0,
            "dropped_null_team": 0,
        }

    existing = before["columns"]
    select_list = ",\n            ".join(
        f"{_select_expr(col, existing)} AS {col}" for col in WANTED_COL_NAMES
    )
    cols_csv = ", ".join(WANTED_COL_NAMES)

    # Rows we will refuse to copy (no resolvable team_id). Count first so
    # the apply report can distinguish "collapsed dupes" from "dropped".
    dropped_null_team = conn.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT {_select_expr('team_id', existing)} AS team_id
            FROM {TABLE}
        )
        WHERE team_id IS NULL
        """
    ).fetchone()[0]

    conn.execute(f"DROP TABLE IF EXISTS {NEW_TABLE}")
    conn.execute(NEW_TABLE_SQL)
    conn.execute(
        f"""
        INSERT INTO {NEW_TABLE} ({cols_csv})
        SELECT {cols_csv}
        FROM (
            SELECT
                {select_list},
                ROW_NUMBER() OVER (
                    PARTITION BY tournament_id, player_id
                    ORDER BY {_order_expr(existing)}
                ) AS _rn
            FROM {TABLE}
        ) _src
        WHERE _rn = 1 AND team_id IS NOT NULL
        """
    )
    copied = _row_count(conn, NEW_TABLE)

    conn.execute(f"DROP TABLE {TABLE}")
    conn.execute(f"ALTER TABLE {NEW_TABLE} RENAME TO {TABLE}")
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {TOURNAMENT_INDEX} "
        f"ON {TABLE} (tournament_id)"
    )

    return {
        "rebuilt": True,
        "copied": copied,
        "dropped_orphans": before["orphan_player_rows"],
        "dropped_null_team": dropped_null_team,
        "collapsed_extra": before["duplicate_extra_rows"],
    }


def verify_schema(conn: sqlite3.Connection) -> Tuple[bool, Dict]:
    after = inspect_schema(conn)
    ok = after["table_exists"] and not after["needs_rebuild"]
    return ok, after


# ─── CLI ────────────────────────────────────────────────────────────────────


def _backup_db(src: str) -> str:
    ts = time.strftime("%Y%m%dT%H%M%S")
    dst = f"{src}.bak.{ts}"
    if os.path.exists(dst):
        raise FileExistsError(f"backup target already exists: {dst}")
    shutil.copy2(src, dst)
    return dst


def _open(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn


def run_dry_run(conn: sqlite3.Connection) -> int:
    snapshot = inspect_schema(conn)
    print_report(snapshot, "tournament_player_stats_cache (BEFORE)")
    print("\n[DRY-RUN] No changes written. Re-run with --apply to migrate.")
    return 0


def run_apply(conn: sqlite3.Connection) -> int:
    before = inspect_schema(conn)
    print_report(before, "tournament_player_stats_cache (BEFORE)")

    if not before["table_exists"]:
        print("\n[SKIP] Table is absent — nothing to rebuild.")
        return 0
    if not before["needs_rebuild"]:
        print("\n[SKIP] Schema already matches the current model — no-op.")
        return 0

    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        print(
            f"\n[ERROR] Could not acquire write lock — is the app running? {exc}",
            file=sys.stderr,
        )
        return 3

    try:
        result = apply_rebuild(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        print("\n[ROLLBACK] Migration failed; DB left unchanged.")
        raise

    print(
        f"\n[OK] Rebuilt. Copied {result['copied']} row(s) "
        f"(collapsed {result.get('collapsed_extra', 0)} duplicate extra, "
        f"dropped {result['dropped_null_team']} with no team_id)."
    )

    conn.execute("PRAGMA foreign_keys = ON")
    ok, after = verify_schema(conn)
    print_report(after, "tournament_player_stats_cache (AFTER)")

    violations = [
        v for v in conn.execute("PRAGMA foreign_key_check").fetchall()
        if v[0] == TABLE
    ]
    if violations:
        print(
            f"\n[WARN] foreign_key_check on {TABLE} returned "
            f"{len(violations)} violation row(s) — first 10:"
        )
        for v in violations[:10]:
            print(f"   {v}")
    else:
        print(f"\n[OK] foreign_key_check ({TABLE}): no violations.")

    # Prove the failure mode is gone: an INSERT that omits category/data_json
    # must be accepted. Rolled back immediately so apply is not a data write.
    try:
        conn.execute("BEGIN")
        conn.execute(
            f"""
            INSERT INTO {TABLE} (
                tournament_id, player_id, team_id, matches_played
            ) VALUES (-1, -1, -1, 0)
            """
        )
        conn.execute("ROLLBACK")
        print("[OK] Probe INSERT without category/data_json succeeded (rolled back).")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        # -1/-1/-1 will fail FK if FKs are on, which is fine — we only care
        # that the error is NOT the ghost-column NOT NULL.
        msg = str(exc).lower()
        if "category" in msg or "data_json" in msg:
            print(f"\n[FAIL] Probe INSERT still hits ghost NOT NULL: {exc}")
            return 1
        print(f"[OK] Probe INSERT rejected for a non-ghost reason (expected): {exc}")

    if not ok:
        print("\n[FAIL] Post-apply schema still reports drift.")
        return 1

    print("\n[DONE] Migration applied and verified successfully.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to the SQLite DB file.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the rebuild. Without this flag the script only inspects.",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="With --apply, copy the DB to <path>.bak.<timestamp> first.",
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.db):
        print(f"[ERROR] DB file not found: {args.db}", file=sys.stderr)
        return 2

    if args.backup and not args.apply:
        print("[NOTE] --backup is meaningless without --apply; ignoring.")

    print(f"DB: {args.db}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN (step 1)'}")

    conn = _open(args.db)
    try:
        if not args.apply:
            return run_dry_run(conn)

        if args.backup:
            backup_path = _backup_db(args.db)
            print(f"\n[BACKUP] Wrote snapshot: {backup_path}")

        return run_apply(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
