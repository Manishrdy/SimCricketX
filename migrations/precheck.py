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

    # CLI (manual pre-deploy check, e.g. before a standalone data migration)
    python -m migrations.precheck
    python -m migrations.precheck --db /path/to/cricket_sim.db
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


def _loader(module_path: str, entry: str = "run_migration"):
    def _resolve():
        module = __import__(module_path, fromlist=[entry])
        return getattr(module, entry)
    return _resolve


MIGRATIONS: List[Tuple[str, Callable]] = [
    ("fix_db_schema",            _load_ensure_schema),
    ("add_team_profiles",        _loader("migrations.add_team_profiles")),
    ("add_tournament_format",    _loader("migrations.add_tournament_format")),
    ("add_tournament_creation_token",
     _loader("migrations.add_tournament_creation_token")),
    ("add_tours", _loader("migrations.add_tours")),
    ("add_hundred_metadata", _loader("migrations.add_hundred_metadata")),
    ("add_scheduled_overs", _loader("migrations.add_scheduled_overs")),
    # One in-flight match per fixture: tournament_fixtures.active_match_id.
    ("add_fixture_active_match",
     _loader("migrations.add_fixture_active_match")),
    ("add_account_lockout",      _loader("migrations.add_account_lockout")),
    ("add_pending_email",        _loader("migrations.add_pending_email")),
    ("add_exception_log",        _loader("migrations.add_exception_log")),
    ("add_exception_log_metadata", _loader("migrations.add_exception_log_metadata")),
    ("add_exception_log_dedup",  _loader("migrations.add_exception_log_dedup")),
    ("add_player_pool",          _loader("migrations.add_player_pool")),
    # Per-format T20/ListA/FC defaults on both global and user player pools.
    ("add_player_pool_format_ratings",
     _loader("migrations.add_player_pool_format_ratings")),
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
    # Drop leftover category/data_json columns on tournament_player_stats_cache.
    # Dry-run on boot (report only); apply via the CLI / standalone prod script.
    ("rebuild_tournament_player_stats_cache",
     _loader("migrations.rebuild_tournament_player_stats_cache")),
    # Remove the retired manual issue-report storage (replaced first by the
    # support chat, now by the community board).
    ("drop_issue_reports",       _loader("migrations.drop_issue_reports")),
    # Explicit discriminator for super-over career-stat scorecard rows
    # (previously only distinguishable by the magic innings_number=3).
    ("add_super_over_flag",      _loader("migrations.add_super_over_flag")),
    # Structured match outcome (match_status/stats_incomplete) + byes/leg_byes
    # columns so downstream code stops parsing prose or subtracting a
    # remainder to recover facts the engine already knows.
    ("add_structured_match_outcome", _loader("migrations.add_structured_match_outcome")),
    # Man of the Match — matches.motm_player_id, set once at archive time.
    ("add_motm_column",           _loader("migrations.add_motm_column")),
    # Per-format ground configs: (user_id, match_format) keying plus a v2
    # normalisation of stored blobs. Ground conditions used to be one T20-only
    # blob per user that ListA matches ignored.
    ("add_ground_config_formats", _loader("migrations.add_ground_config_formats")),
    # 2026-08-16 T20 pitch recalibration: stored blobs deep-merge OVER the
    # defaults, and the mode picker used to snapshot the whole effective config,
    # so users carry involuntary copies of the old (much higher scoring) pitch
    # matrices. Strip the copies that match the old shipped values verbatim.
    ("reset_stale_t20_pitch_tuning",
     _loader("migrations.reset_stale_t20_pitch_tuning")),
    ("reset_stale_fc_pitch_tuning",
     _loader("migrations.reset_stale_fc_pitch_tuning")),
    # T10 aggression pass: the Green/Dry/Flat/Dead scoring matrices, Dry's
    # wicket_factors and every T10 phase boost moved. Same shadowing trap as
    # the entry above — a user who so much as picked a game mode on the T10
    # ground-conditions page is carrying a full snapshot of the old numbers
    # that deep-merges over the new defaults.
    ("reset_stale_t10_pitch_tuning",
     _loader("migrations.reset_stale_t10_pitch_tuning")),
    # 2026-08-30 FC scoring acceleration: the FC scoring matrices moved again
    # (~12% more scoring mass, ~5% more wicket) to lift the run rate from
    # ~3.10 to ~3.40. Same shadowing trap as above — strip the involuntary
    # copies of the slower matrices.
    ("reset_slow_fc_scoring",
     _loader("migrations.reset_slow_fc_scoring")),
    # 2026-09-04 FC pitch ladder: Flat and Dead were producing near-identical
    # first-innings totals (429 vs 413). Flat lifted to 400+, Dead to 500-600,
    # with Dead's fresh-pitch wicket factors dropped below Flat's (they had
    # been higher, which is backwards). Same shadowing trap — strip the
    # involuntary copies of the pre-ladder Flat/Dead profiles.
    ("reset_flat_dead_fc_tuning",
     _loader("migrations.reset_flat_dead_fc_tuning")),
    # 2026-09-11 FC Flat/Dead wicket retune: those two surfaces were producing
    # a 600+ innings once every five (Dead) and one in ten (Flat), against a
    # real first-class rate nearer one in forty, with a 973 as the worst case.
    # Their wicket factors were raised; run rate is untouched, because a road
    # is a road for how fast you score on it, not for being undismissable.
    # Same shadowing trap as the entries above, but this one CORRECTS stale
    # values rather than stripping them, and leaves any value the user
    # deliberately tuned alone.
    ("fc_flat_dead_wicket_retune",
     _loader("migrations.fc_flat_dead_wicket_retune")),
    # First-Class format: matches.days/follow_on_enforced/*_innings2 columns
    # for up-to-2-innings-per-side matches.
    ("add_fc_match_columns",     _loader("migrations.add_fc_match_columns")),
    # Compact weather outcome used by completed FC scorecards. Detailed
    # interruption events stay in the generated match archive.
    ("add_match_weather_summary", _loader("migrations.add_match_weather_summary")),
    # First-Class format: players.technique_rating/temperament_rating/
    # stamina_rating, scoped per-profile like every other rating.
    ("add_fc_player_ratings",    _loader("migrations.add_fc_player_ratings")),
    # FC in tournaments: tournament_teams.drawn, tracked separately from tied.
    ("add_tournament_team_drawn_column",
     _loader("migrations.add_tournament_team_drawn_column")),
    # FC-native tournament statistics (200s/300s, BBI/BBM, 5WI/10WM and
    # detailed cache parity with direct scorecard aggregation).
    ("extend_fc_statistics_cache",
     _loader("migrations.extend_fc_statistics_cache")),
    # teams.updated_at — last-edit timestamp so /teams/manage can show
    # "Last updated ..." instead of the creation date for teams that changed.
    ("add_team_updated_at",      _loader("migrations.add_team_updated_at")),
    # Community board (replaces the 1:1 support chat). Boot applies only the
    # additive schema; dropping the old support_* tables needs an explicit
    # `python -m migrations.community_board --db <path> --apply`.
    # (add_support_messaging is no longer registered, so nothing recreates them.)
    ("community_board",          _loader("migrations.community_board", "run_on_boot")),
    # users.guide_state — onboarding guide progress (page tours seen,
    # new-user journey). NULL means nothing seen, so no backfill.
    ("add_user_guide_state",     _loader("migrations.add_user_guide_state")),
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
    parser.add_argument("--db", default=None,
                        help="SQLite database path (default: repository cricket_sim.db).")
    args = parser.parse_args()

    # Prevent module-level app bootstrap while importing app.py in CLI mode.
    os.environ["SIMCRICKETX_SKIP_GLOBAL_APP"] = "1"
    # Prevent app.create_app() from recursively invoking precheck again.
    os.environ["SIMCRICKETX_PRECHECK_RUNNING"] = "1"

    if args.db:
        db_path = os.path.abspath(args.db)
        if not os.path.isfile(db_path):
            parser.error(f"database does not exist: {db_path}")
        # Same escape hatch the standalone data migrations use: point the app
        # factory at an explicit file and suppress the normal production
        # startup side effects (backup scheduler, background workers).
        os.environ["SIMCRICKETX_TEST_MODE"] = "1"
        os.environ["SIMCRICKETX_TEST_DB_URI"] = f"sqlite:///{db_path}"
        print(f"[Precheck] Target database: {db_path}")

    from database import db as _db
    from app import create_app

    _app = create_app()
    results = run_all(_db, _app)

    if args.fail_fast:
        failures = [r for r in results if r[1] == "failed"]
        if failures:
            sys.exit(1)
