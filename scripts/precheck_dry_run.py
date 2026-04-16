"""
Precheck Dry-Run Inspector
==========================

Read-only inspection of the database to report which migrations from
`migrations.precheck.MIGRATIONS` would be a no-op and which would make
changes. Performs zero writes.

This is intentionally decoupled from `migrations.precheck` — the real
precheck runs the actual migration modules; this tool only inspects state
so you can preview a prod deploy without touching the database.

Usage:
    python scripts/precheck_dry_run.py
    python scripts/precheck_dry_run.py --db /path/to/prod_backup.db
"""

import argparse
import os
import sys
from typing import Callable, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── DB inspection helpers ─────────────────────────────────────────────────────

def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn, table: str) -> List[str]:
    if not _table_exists(conn, table):
        return []
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def _fk_on_delete(conn, table: str, column: str) -> str:
    if not _table_exists(conn, table):
        return ""
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    # row: (id, seq, ref_table, from, to, on_update, on_delete, match)
    for r in rows:
        if r[3] == column:
            return (r[6] or "").upper()
    return ""


# ── Per-migration checks ──────────────────────────────────────────────────────
# Each check returns (status, detail).
# status: "applied" | "pending" | "partial" | "unknown"

def _check_fix_db_schema(conn) -> Tuple[str, str]:
    # ensure_schema is a drift guard with many small checks. A dry probe is
    # impractical — it's also cheap and always safe to run. Report its scope.
    return ("unknown", "drift guard — not dry-runnable; always executes")


def _check_add_team_profiles(conn) -> Tuple[str, str]:
    has_table = _table_exists(conn, "team_profiles")
    has_col = "profile_id" in _columns(conn, "players")
    if has_table and has_col:
        return ("applied", "team_profiles + players.profile_id present")
    missing = []
    if not has_table: missing.append("team_profiles table")
    if not has_col: missing.append("players.profile_id")
    return ("pending", "missing: " + ", ".join(missing))


def _check_add_tournament_format(conn) -> Tuple[str, str]:
    has = "format_type" in _columns(conn, "tournaments")
    return ("applied", "tournaments.format_type present") if has else ("pending", "tournaments.format_type missing")


def _check_add_account_lockout(conn) -> Tuple[str, str]:
    required = {"lockout_until", "lockout_count", "lockout_window_start"}
    present = set(_columns(conn, "users")) & required
    if present == required:
        return ("applied", "all 3 lockout columns present")
    return ("pending", f"missing {sorted(required - present)}")


def _check_add_pending_email(conn) -> Tuple[str, str]:
    required = {"pending_email", "pending_email_token", "pending_email_token_expires"}
    present = set(_columns(conn, "users")) & required
    if present == required:
        return ("applied", "all 3 pending_email columns present")
    return ("pending", f"missing {sorted(required - present)}")


def _check_add_exception_log(conn) -> Tuple[str, str]:
    return ("applied", "exception_log table present") if _table_exists(conn, "exception_log") else ("pending", "exception_log table missing")


def _check_add_exception_log_metadata(conn) -> Tuple[str, str]:
    required = {"severity", "source", "context_json", "request_id", "handled", "resolved", "resolved_at", "resolved_by"}
    present = set(_columns(conn, "exception_log")) & required
    if present == required:
        return ("applied", "all 8 metadata columns present")
    return ("pending", f"missing {sorted(required - present)}")


def _check_add_exception_log_dedup(conn) -> Tuple[str, str]:
    required = {"fingerprint", "occurrence_count", "first_seen_at", "last_seen_at", "github_issue_number", "github_issue_url"}
    present = set(_columns(conn, "exception_log")) & required
    if present == required:
        return ("applied", "all 6 dedup columns present")
    return ("pending", f"missing {sorted(required - present)}")


def _check_add_player_pool(conn) -> Tuple[str, str]:
    mp = _table_exists(conn, "master_players")
    up = _table_exists(conn, "user_players")
    if mp and up:
        return ("applied", "master_players + user_players present")
    missing = [n for n, ok in (("master_players", mp), ("user_players", up)) if not ok]
    return ("pending", f"missing {missing}")


def _check_link_players_to_pool(conn) -> Tuple[str, str]:
    required = {"master_player_id", "user_player_id"}
    present = set(_columns(conn, "players")) & required
    if present == required:
        return ("applied", "players.master_player_id + players.user_player_id present")
    return ("pending", f"missing {sorted(required - present)}")


def _check_add_scorecard_cascade(conn) -> Tuple[str, str]:
    action = _fk_on_delete(conn, "match_scorecards", "player_id")
    if action == "CASCADE":
        return ("applied", "match_scorecards.player_id ON DELETE CASCADE")
    if not action:
        return ("pending", "no FK found on match_scorecards.player_id")
    return ("pending", f"current ON DELETE action: {action} (expected CASCADE)")


def _check_add_scorecard_stumpings(conn) -> Tuple[str, str]:
    has = "stumpings" in _columns(conn, "match_scorecards")
    return ("applied", "match_scorecards.stumpings present") if has else ("pending", "column missing")


def _check_cleanup_orphaned_stats(conn) -> Tuple[str, str]:
    if not _table_exists(conn, "match_scorecards") or not _table_exists(conn, "players"):
        return ("unknown", "requires match_scorecards + players tables")

    sc_orphans = conn.execute(
        """
        SELECT COUNT(*) FROM match_scorecards sc
        LEFT JOIN players p ON sc.player_id = p.id
        WHERE p.id IS NULL
        """
    ).fetchone()[0]

    mp_orphans = 0
    if _table_exists(conn, "match_partnerships"):
        mp_orphans = conn.execute(
            """
            SELECT COUNT(*) FROM match_partnerships mp
            LEFT JOIN players p1 ON mp.batsman1_id = p1.id
            LEFT JOIN players p2 ON mp.batsman2_id = p2.id
            WHERE p1.id IS NULL OR p2.id IS NULL
            """
        ).fetchone()[0]

    if sc_orphans == 0 and mp_orphans == 0:
        return ("applied", "no orphaned scorecards/partnerships detected")
    return ("pending", f"orphans detected: scorecards={sc_orphans}, partnerships={mp_orphans}")


def _check_recover_archived_stats_from_backup(conn) -> Tuple[str, str]:
    return ("unknown", "requires external source backup DB path at runtime")


def _check_redesign_auction_phase1(conn) -> Tuple[str, str]:
    required = ["leagues", "seasons", "season_teams"]
    missing = [t for t in required if not _table_exists(conn, t)]
    if missing:
        return ("pending" if len(missing) == len(required) else "partial", f"missing tables: {missing}")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(teams)").fetchall()}
    if "season_id" not in cols:
        return ("partial", "teams.season_id column missing")
    return ("applied", "leagues/seasons/season_teams present, teams.season_id present")


CHECKS: Dict[str, Callable] = {
    "fix_db_schema":                 _check_fix_db_schema,
    "add_team_profiles":             _check_add_team_profiles,
    "add_tournament_format":         _check_add_tournament_format,
    "add_account_lockout":           _check_add_account_lockout,
    "add_pending_email":             _check_add_pending_email,
    "add_exception_log":             _check_add_exception_log,
    "add_exception_log_metadata":    _check_add_exception_log_metadata,
    "add_exception_log_dedup":       _check_add_exception_log_dedup,
    "add_player_pool":               _check_add_player_pool,
    "link_players_to_pool":          _check_link_players_to_pool,
    "add_scorecard_cascade":         _check_add_scorecard_cascade,
    "add_scorecard_stumpings":       _check_add_scorecard_stumpings,
    "cleanup_orphaned_stats":        _check_cleanup_orphaned_stats,
    "recover_archived_stats_from_backup": _check_recover_archived_stats_from_backup,
    "redesign_auction_phase1":       _check_redesign_auction_phase1,
}


# ── Entry point ───────────────────────────────────────────────────────────────

STATUS_GLYPH = {
    "applied": "✓",
    "pending": "→",
    "partial": "~",
    "unknown": "?",
}


# Declared order — must stay in sync with migrations/precheck.py MIGRATIONS.
# Duplicated here (not imported) so the dry-run is a pure sqlite3 script
# and doesn't require Flask/SQLAlchemy to be installed.
MIGRATION_ORDER: List[str] = [
    "fix_db_schema",
    "add_team_profiles",
    "add_tournament_format",
    "add_account_lockout",
    "add_pending_email",
    "add_exception_log",
    "add_exception_log_metadata",
    "add_exception_log_dedup",
    "add_player_pool",
    "link_players_to_pool",
    "add_scorecard_cascade",
    "add_scorecard_stumpings",
    "cleanup_orphaned_stats",
    "recover_archived_stats_from_backup",
    "redesign_auction_phase1",
]


def inspect(db_path: str) -> List[Tuple[str, str, str]]:
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        results = []
        for name in MIGRATION_ORDER:
            check = CHECKS.get(name)
            if check is None:
                results.append((name, "unknown", "no check defined"))
                continue
            status, detail = check(conn)
            results.append((name, status, detail))
        return results
    finally:
        conn.close()


def _print_report(results: List[Tuple[str, str, str]], db_path: str) -> int:
    print(f"Precheck dry-run against: {db_path}")
    print("=" * 72)
    name_w = max(len(n) for n, _, _ in results)
    for name, status, detail in results:
        glyph = STATUS_GLYPH.get(status, "?")
        print(f"  {glyph} {name.ljust(name_w)}  {status.upper():<8}  {detail}")
    print("=" * 72)

    applied = sum(1 for _, s, _ in results if s == "applied")
    pending = sum(1 for _, s, _ in results if s in ("pending", "partial"))
    unknown = sum(1 for _, s, _ in results if s == "unknown")
    print(f"Summary: {applied} applied, {pending} pending, {unknown} unknown (of {len(results)})")
    print("\nNo writes performed. Run `python -m migrations.precheck` to actually migrate.")
    return 1 if pending else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only dry-run report of schema migrations.")
    parser.add_argument("--db", default=None, help="Path to SQLite DB (default: project cricket_sim.db).")
    args = parser.parse_args()

    db_path = args.db
    if not db_path:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(project_root, "cricket_sim.db")

    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        sys.exit(2)

    results = inspect(db_path)
    sys.exit(_print_report(results, db_path))
