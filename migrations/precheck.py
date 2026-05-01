"""
Startup Migration Precheck
==========================

Single entry point that runs every schema-level migration in a deterministic
order. Replaces the scattered per-migration try/except blocks that used to
live inline in app.py.

Every registered step is idempotent — re-running the precheck against a
fully-migrated database is a no-op. A failure in one step does NOT abort
the chain; it is logged and reported, and subsequent steps still run. The
app start path has always tolerated individual migration failures and this
preserves that behaviour.

Usage:
    # Called from app.py during startup (normal path)
    from migrations.precheck import run_all
    run_all(db, app)

    # CLI (manual pre-deploy check)
    python -m migrations.precheck
"""

import argparse
import os
import sys
from typing import Callable, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.exception_tracker import log_exception


# Ordered migration registry. Each entry: (display_name, loader).
# The loader returns the `run_migration(db, app)`-shaped callable.
# Order matters — later migrations may assume earlier ones have applied.

def _load_ensure_schema():
    from scripts.fix_db_schema import ensure_schema

    def _run(db, app):
        with app.app_context():
            ensure_schema(db.engine, db)
    return _run


def _loader(module_path: str):
    def _resolve():
        module = __import__(module_path, fromlist=["run_migration"])
        return module.run_migration
    return _resolve


MIGRATIONS: List[Tuple[str, Callable]] = [
    ("fix_db_schema",            _load_ensure_schema),
    ("add_team_profiles",        _loader("migrations.add_team_profiles")),
    ("add_tournament_format",    _loader("migrations.add_tournament_format")),
    ("add_account_lockout",      _loader("migrations.add_account_lockout")),
    ("add_pending_email",        _loader("migrations.add_pending_email")),
    ("add_exception_log",        _loader("migrations.add_exception_log")),
    ("add_exception_log_metadata", _loader("migrations.add_exception_log_metadata")),
    ("add_exception_log_dedup",  _loader("migrations.add_exception_log_dedup")),
    ("add_player_pool",          _loader("migrations.add_player_pool")),
    # Requires player-pool tables; schema-only FK/index step, safe+idempotent.
    ("link_players_to_pool",     _loader("migrations.link_players_to_pool")),
    ("add_scorecard_cascade",    _loader("migrations.add_scorecard_cascade")),
    ("add_scorecard_stumpings",  _loader("migrations.add_scorecard_stumpings")),
    # Safety audit in dry-run mode by default (no deletes unless explicitly applied).
    ("cleanup_orphaned_stats",   _loader("migrations.cleanup_orphaned_stats")),
    # Optional recovery dry-run (only runs when PRECHECK_RECOVERY_SOURCE_DB is set).
    ("recover_archived_stats_from_backup", _loader("migrations.recover_archived_stats_from_backup")),
    # AUCTION-REDESIGN Phase 1: drops legacy auction tables, adds leagues/seasons/season_teams.
    # (The old `add_auction` migration was removed from the registry; its tables
    # are DROPped by this step and recreated with new schemas in phase 2.)
    ("redesign_auction_phase1",  _loader("migrations.redesign_auction_phase1")),
    # AUCTION-REDESIGN Phase 2: auction + auction_categories + auction_players (setup only).
    ("auction_setup_phase2",     _loader("migrations.auction_setup_phase2")),
    # AUCTION-REDESIGN Phase 3: auction_chat_messages (realtime foundation).
    ("auction_realtime_phase3",  _loader("migrations.auction_realtime_phase3")),
    # AUCTION-REDESIGN Phase 4: live runtime columns on auctions (live_player_id, lot_ends_at, ...).
    ("auction_live_phase4",      _loader("migrations.auction_live_phase4")),
    # AUCTION-REDESIGN Phase 5: draft_picks table (snake-order draft mode).
    ("auction_draft_phase5",     _loader("migrations.auction_draft_phase5")),
    # AUCTION-REDESIGN Phase 8: auction_bids + auction_audit_logs (history + moderation trail).
    ("auction_history_phase8",   _loader("migrations.auction_history_phase8")),
    # Password-change OTP columns on users (account-settings flow).
    ("add_password_change_otp",  _loader("migrations.add_password_change_otp")),
    # FK ON DELETE actions for matches/tournament_teams/tournament_fixtures so
    # team and tournament deletion no longer needs application-layer cleanup
    # to avoid IntegrityError. See migration docstring for the action matrix.
    ("add_team_match_fk_actions", _loader("migrations.add_team_match_fk_actions")),
]


def run_all(db, app):
    """Run every registered migration in order.

    Returns a list of (name, "ok" | "failed", error_or_None) triples so the
    caller can log a structured summary if desired. Individual failures are
    swallowed here (logged via log_exception) so the app can still boot with
    partially-migrated state, matching the pre-refactor behaviour.
    """
    results = []
    for name, loader in MIGRATIONS:
        try:
            if name == "fix_db_schema":
                runner = loader()
            else:
                runner = loader()
            runner(db, app)
            results.append((name, "ok", None))
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": name})
            print(f"[Precheck] {name} SKIPPED — {exc}")
            results.append((name, "failed", str(exc)))

    ok = sum(1 for _, s, _ in results if s == "ok")
    print(f"[Precheck] {ok}/{len(results)} migrations completed.")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run all schema migrations in order.")
    parser.add_argument("--fail-fast", action="store_true",
                        help="Exit immediately on first migration failure (default: continue).")
    args = parser.parse_args()

    # Prevent module-level app bootstrap while importing app.py in CLI mode.
    os.environ["SIMCRICKETX_SKIP_GLOBAL_APP"] = "1"
    # Prevent app.create_app() from recursively invoking precheck again.
    os.environ["SIMCRICKETX_PRECHECK_RUNNING"] = "1"
    from database import db as _db
    from app import create_app

    _app = create_app()
    results = run_all(_db, _app)

    if args.fail_fast:
        failures = [r for r in results if r[1] == "failed"]
        if failures:
            sys.exit(1)
