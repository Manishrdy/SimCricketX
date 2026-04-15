"""
Cleanup Orphaned Stats Migration
================================

Removes match_scorecard and match_partnership rows whose player_id FKs no
longer point at a live Player. These orphans are produced when a Player row is
deleted while SQLite's FK enforcement is disabled on the connection, which
bypasses the ondelete='CASCADE' rule on the scorecard/partnership tables.

Because player *names* are not stored on match_scorecards, orphaned rows
cannot be re-attached to the current per-profile Player identities — the
identity information is simply gone. These rows are silently dropped by
stats queries (inner-joined on Player.id) so they never appear in any UI,
but they inflate table size and can mislead future maintenance.

The migration runs in DRY-RUN mode by default. Pass --apply to delete.

Usage:
    python migrations/cleanup_orphaned_stats.py            # dry-run
    python migrations/cleanup_orphaned_stats.py --apply    # delete
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text


def _report(conn):
    """Print orphan counts broken down by type. Returns totals dict."""
    totals = {}

    row = conn.execute(text("""
        SELECT COUNT(*) FROM match_scorecards sc
        LEFT JOIN players p ON sc.player_id = p.id
        WHERE p.id IS NULL
    """)).fetchone()
    totals['scorecards_orphaned'] = row[0]

    row = conn.execute(text("SELECT COUNT(*) FROM match_scorecards")).fetchone()
    totals['scorecards_total'] = row[0]

    row = conn.execute(text("""
        SELECT COUNT(*) FROM match_partnerships mp
        LEFT JOIN players p1 ON mp.batsman1_id = p1.id
        LEFT JOIN players p2 ON mp.batsman2_id = p2.id
        WHERE p1.id IS NULL OR p2.id IS NULL
    """)).fetchone()
    totals['partnerships_orphaned'] = row[0]

    row = conn.execute(text("SELECT COUNT(*) FROM match_partnerships")).fetchone()
    totals['partnerships_total'] = row[0]

    rows = conn.execute(text("""
        SELECT sc.record_type, COUNT(*)
        FROM match_scorecards sc
        LEFT JOIN players p ON sc.player_id = p.id
        WHERE p.id IS NULL
        GROUP BY sc.record_type
    """)).fetchall()
    by_type = {r[0]: r[1] for r in rows}

    print("=" * 64)
    print("Orphaned Stats Report")
    print("=" * 64)
    print(f"Scorecards:    {totals['scorecards_orphaned']:>5} / {totals['scorecards_total']} orphaned")
    for t, cnt in by_type.items():
        print(f"  {t:<10}  {cnt:>5}")
    print(f"Partnerships:  {totals['partnerships_orphaned']:>5} / {totals['partnerships_total']} orphaned")
    print()

    # Matches with any orphans (helpful for narrowing what pre-migration data is affected)
    rows = conn.execute(text("""
        SELECT DISTINCT sc.match_id
        FROM match_scorecards sc
        LEFT JOIN players p ON sc.player_id = p.id
        WHERE p.id IS NULL
    """)).fetchall()
    totals['matches_affected'] = len(rows)
    print(f"Matches with at least one orphaned scorecard: {len(rows)}")

    return totals


def _delete(conn):
    """Delete orphaned rows. Run inside a transaction by caller."""
    r1 = conn.execute(text("""
        DELETE FROM match_scorecards
        WHERE player_id NOT IN (SELECT id FROM players)
    """))
    r2 = conn.execute(text("""
        DELETE FROM match_partnerships
        WHERE batsman1_id NOT IN (SELECT id FROM players)
           OR batsman2_id NOT IN (SELECT id FROM players)
    """))
    print(f"Deleted {r1.rowcount} scorecards, {r2.rowcount} partnerships.")


def run_migration(db, app, apply=False):
    """Run the cleanup migration inside the app context.

    Args:
        db: SQLAlchemy db instance.
        app: Flask app.
        apply: If False (default), only report. If True, perform deletions.
    """
    with app.app_context():
        conn = db.engine.connect()
        try:
            totals = _report(conn)
            if not apply:
                print()
                print("DRY RUN — no changes made. Pass --apply to delete.")
                return totals

            if totals['scorecards_orphaned'] == 0 and totals['partnerships_orphaned'] == 0:
                print("Nothing to delete.")
                return totals

            print()
            print("Applying deletions …")
            # _report() auto-began a read transaction on first SELECT; close it
            # before opening the write transaction.
            conn.rollback()
            trans = conn.begin()
            try:
                _delete(conn)
                trans.commit()
                print("Committed.")
            except Exception as exc:
                trans.rollback()
                print(f"FAILED — rolled back. Error: {exc}")
                raise

            print()
            print("Post-cleanup report:")
            _report(conn)
            return totals
        finally:
            conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Report or delete orphaned stats rows.")
    parser.add_argument("--apply", action="store_true", help="Actually delete orphans (default: dry-run).")
    args = parser.parse_args()

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app, apply=args.apply)
