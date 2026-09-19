"""
Repair First-Class Scorecards Super-Over Flag Migration
======================================================

Standalone migration script to audit and repair First-Class (FC) match scorecard
records in the database where `is_super_over = 1` was erroneously set by the
legacy super-over flag migration.

In First-Class matches, innings 3 and 4 are legitimate innings (2nd innings of
the competing sides), NOT super overs. Leaving `is_super_over = 1` on these rows
causes stats calculations and scoreboards to discard them.

Components:
1. Dry Run (default):
   Scans the database, calculates metrics, and displays a comprehensive summary
   at the bottom showing how many rows need repair, how many non-FC rows are
   ignored/preserved, and what changes would be made without altering the DB.

2. --apply:
   Executes the transactional update, repairs the affected records to
   `is_super_over = 0`, and prints post-repair metrics with counts for success,
   failed, and ignored records.

Usage:
    # Dry Run (read-only inspection & metrics):
    python -m migrations.repair_fc_scorecard_super_over_flags
    python -m migrations.repair_fc_scorecard_super_over_flags --db ./cricket_sim.db

    # Apply changes:
    python -m migrations.repair_fc_scorecard_super_over_flags --apply
    python -m migrations.repair_fc_scorecard_super_over_flags --db ./cricket_sim.db --apply
"""

import argparse
import os
import sys
import sqlite3
from typing import Dict, Any, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, "cricket_sim.db")


def inspect_database(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Inspect and collect metrics across matches and scorecards."""
    cursor = conn.cursor()

    # Total matches and format breakdown
    cursor.execute("SELECT match_format, COUNT(*) FROM matches GROUP BY match_format")
    matches_by_format = dict(cursor.fetchall())
    total_matches = sum(matches_by_format.values())

    # Total scorecards
    cursor.execute("SELECT COUNT(*) FROM match_scorecards")
    total_scorecards = cursor.fetchone()[0]

    # First-Class scorecards breakdown by innings and is_super_over
    cursor.execute("""
        SELECT sc.innings_number, COALESCE(sc.is_super_over, 0), COUNT(*)
        FROM match_scorecards sc
        JOIN matches m ON sc.match_id = m.id
        WHERE m.match_format = 'FC'
        GROUP BY sc.innings_number, COALESCE(sc.is_super_over, 0)
    """)
    fc_scorecards_breakdown = cursor.fetchall()

    # FC scorecards incorrectly marked as super overs (TARGETS)
    cursor.execute("""
        SELECT COUNT(*)
        FROM match_scorecards sc
        JOIN matches m ON sc.match_id = m.id
        WHERE m.match_format = 'FC' AND sc.is_super_over = 1
    """)
    fc_corrupted_scorecards = cursor.fetchone()[0]

    # FC matches affected by corrupted scorecards
    cursor.execute("""
        SELECT COUNT(DISTINCT sc.match_id)
        FROM match_scorecards sc
        JOIN matches m ON sc.match_id = m.id
        WHERE m.match_format = 'FC' AND sc.is_super_over = 1
    """)
    fc_affected_matches = cursor.fetchone()[0]

    # FC scorecards already clean (is_super_over == 0)
    cursor.execute("""
        SELECT COUNT(*)
        FROM match_scorecards sc
        JOIN matches m ON sc.match_id = m.id
        WHERE m.match_format = 'FC' AND (sc.is_super_over IS NULL OR sc.is_super_over = 0)
    """)
    fc_clean_scorecards = cursor.fetchone()[0]

    # Non-FC scorecards with is_super_over == 1 (to be PRESERVED / IGNORED)
    cursor.execute("""
        SELECT COUNT(*)
        FROM match_scorecards sc
        JOIN matches m ON sc.match_id = m.id
        WHERE m.match_format != 'FC' AND sc.is_super_over = 1
    """)
    non_fc_super_over_scorecards = cursor.fetchone()[0]

    # Non-FC scorecards total
    cursor.execute("""
        SELECT COUNT(*)
        FROM match_scorecards sc
        JOIN matches m ON sc.match_id = m.id
        WHERE m.match_format != 'FC'
    """)
    non_fc_total_scorecards = cursor.fetchone()[0]

    return {
        "total_matches": total_matches,
        "matches_by_format": matches_by_format,
        "total_scorecards": total_scorecards,
        "fc_scorecards_breakdown": fc_scorecards_breakdown,
        "fc_corrupted_scorecards": fc_corrupted_scorecards,
        "fc_affected_matches": fc_affected_matches,
        "fc_clean_scorecards": fc_clean_scorecards,
        "non_fc_super_over_scorecards": non_fc_super_over_scorecards,
        "non_fc_total_scorecards": non_fc_total_scorecards,
    }


def execute_repair(conn: sqlite3.Connection) -> int:
    """Execute the repair query inside an active transaction."""
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE match_scorecards
        SET is_super_over = 0
        WHERE is_super_over = 1
          AND match_id IN (
              SELECT id FROM matches WHERE match_format = 'FC'
          )
    """)
    return cursor.rowcount


def print_report(metrics: Dict[str, Any], mode: str, rows_updated: int = 0, failed_count: int = 0) -> None:
    """Print formatted metrics and audit summary."""
    print("=" * 72)
    print(" FIRST-CLASS SCORECARDS SUPER-OVER AUDIT & MIGRATION REPORT")
    print("=" * 72)
    print(f" Execution Mode : {mode}")
    print("-" * 72)
    print(" DATABASE METRICS:")
    print(f"   • Total Matches in DB        : {metrics['total_matches']}")
    for fmt, count in sorted(metrics['matches_by_format'].items()):
        print(f"     - Format {fmt:<18} : {count} match(es)")
    print(f"   • Total Scorecards in DB     : {metrics['total_scorecards']}")
    print(f"   • Non-FC Scorecards          : {metrics['non_fc_total_scorecards']}")
    print(f"   • Non-FC Super-Over Rows     : {metrics['non_fc_super_over_scorecards']} (Legitimate super overs - PRESERVED)")
    print("-" * 72)
    print(" FIRST-CLASS SCORECARD BREAKDOWN:")
    for inn, is_so, count in sorted(metrics['fc_scorecards_breakdown']):
        label = "Super-Over Flagged (Corrupted)" if is_so else "Normal Innings (Clean)"
        print(f"   • Innings {inn} | {label:<32} : {count:>5} scorecard(s)")

    print("=" * 72)
    print(" MIGRATION SUMMARY & AUDIT RESULTS")
    print("=" * 72)
    if mode == "DRY RUN":
        print(f"  Total Rows Scanned       : {metrics['total_scorecards']}")
        print(f"  Target Rows to Repair    : {metrics['fc_corrupted_scorecards']}")
        print(f"  Matches Needing Repair   : {metrics['fc_affected_matches']}")
        print(f"  Already Clean FC Rows    : {metrics['fc_clean_scorecards']}")
        print(f"  Ignored / Preserved Rows : {metrics['non_fc_total_scorecards']} (All non-FC scorecards)")
        print(f"  Failed Operations        : 0")
        print("-" * 72)
        if metrics['fc_corrupted_scorecards'] > 0:
            print("  STATUS: ACTION REQUIRED")
            print(f"  --> {metrics['fc_corrupted_scorecards']} First-Class scorecard(s) have invalid is_super_over=1.")
            print("  --> Pass --apply to execute the migration and fix the records.")
        else:
            print("  STATUS: ALL CLEAN")
            print("  --> All First-Class scorecards have valid is_super_over=0 flags. No changes needed.")
    else:  # APPLIED
        print(f"  Total Rows Scanned       : {metrics['total_scorecards']}")
        print(f"  Success (Repaired Rows)  : {rows_updated}")
        print(f"  Failed Rows              : {failed_count}")
        print(f"  Ignored / Preserved Rows : {metrics['non_fc_total_scorecards']} (Non-FC rows untouched)")
        print(f"  Clean FC Rows Verified   : {metrics['fc_clean_scorecards'] + rows_updated}")
        print("-" * 72)
        if failed_count == 0:
            print("  STATUS: APPLIED SUCCESSFULLY")
            print(f"  --> Successfully reset is_super_over=0 on {rows_updated} First-Class scorecard(s).")
            print("  --> All First-Class 3rd and 4th innings are now fully visible and restored.")
        else:
            print("  STATUS: MIGRATION FAILED")
            print("  --> Database changes were rolled back.")
    print("=" * 72)


def run_migration(db_path: Optional[str] = None, apply: bool = False) -> Dict[str, Any]:
    """
    Run the standalone migration.

    Args:
        db_path: Optional path to sqlite database file.
        apply: If True, commit changes. If False, perform dry run.

    Returns:
        Dictionary of results and metrics.
    """
    path = db_path or DEFAULT_DB_PATH
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Database file not found at: {path}")

    conn = sqlite3.connect(path)
    try:
        metrics = inspect_database(conn)
        if not apply:
            print_report(metrics, mode="DRY RUN")
            return {
                "success_count": 0,
                "targets_count": metrics["fc_corrupted_scorecards"],
                "failed_count": 0,
                "ignored_count": metrics["non_fc_total_scorecards"],
                "applied": False,
            }

        # Apply mode
        mode = "APPLIED"
        try:
            conn.execute("BEGIN TRANSACTION")
            rows_updated = execute_repair(conn)
            conn.commit()
            print_report(metrics, mode=mode, rows_updated=rows_updated, failed_count=0)
            return {
                "success_count": rows_updated,
                "targets_count": metrics["fc_corrupted_scorecards"],
                "failed_count": 0,
                "ignored_count": metrics["non_fc_total_scorecards"],
                "applied": True,
            }
        except Exception as exc:
            conn.rollback()
            print_report(metrics, mode=mode, rows_updated=0, failed_count=metrics["fc_corrupted_scorecards"])
            print(f"\nERROR during migration execution: {exc}", file=sys.stderr)
            raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Audit and repair First-Class scorecard super_over flags."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply the database changes (default: dry run).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database file (defaults to cricket_sim.db).",
    )
    args = parser.parse_args()
    run_migration(db_path=args.db, apply=args.apply)


if __name__ == "__main__":
    main()
