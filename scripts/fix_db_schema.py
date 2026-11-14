from database import db
from utils.exception_tracker import log_exception


def ensure_schema(engine, db_obj=None):
    """
    Idempotent schema guard. Checks EVERY model column and table.
    Safe to run at startup - only adds, never drops.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = inspector.get_table_names()

    def _add_missing_cols(table_name, col_defs):
        """Check and add missing columns to an existing table."""
        if table_name not in tables:
            return
        existing = [c["name"] for c in inspector.get_columns(table_name)]
        alters = []
        for col_name, col_sql in col_defs.items():
            if col_name not in existing:
                alters.append(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_sql}")
        if alters:
            with engine.begin() as conn:
                for stmt in alters:
                    conn.execute(text(stmt))

    # ── Ensure all tables exist before running table-specific DDL/backfills ──
    all_required_tables = (
        "match_partnerships", "failed_login_attempts",
        "blocked_ips", "active_sessions", "site_counters",
        "login_history", "ip_whitelist", "announcement_banner",
        "user_banner_dismissals", "auth_event_log", "exception_log",
        "issue_webhook_event", "support_prompt_events",
    )
    missing = [t for t in all_required_tables if t not in tables]
    if missing and db_obj is not None:
        # Import all models so SQLAlchemy knows about them
        from database.models import (  # noqa: F401
            MatchPartnership,
            FailedLoginAttempt, BlockedIP, ActiveSession, SiteCounter,
            LoginHistory, IPWhitelistEntry, AnnouncementBanner,
            UserBannerDismissal, AuthEventLog, ExceptionLog,
            IssueWebhookEvent, SupportPromptEvent,
        )
        db_obj.create_all()
        inspector = inspect(engine)
        tables = inspector.get_table_names()

    # ── users ──
    # Check before adding so we know if force_email_verify is brand-new (for backfill below)
    _user_cols_before = (
        [c["name"] for c in inspector.get_columns("users")]
        if "users" in tables else []
    )
    _force_verify_is_new = "force_email_verify" not in _user_cols_before

    _add_missing_cols("users", {
        "stable_id":                   "VARCHAR(36)",
        "last_login":                  "DATETIME",
        "ip_address":                  "VARCHAR(50)",
        "mac_address":                 "VARCHAR(50)",
        "hostname":                    "VARCHAR(100)",
        "display_name":                "VARCHAR(100)",
        "referral_source":             "VARCHAR(100)",
        "is_admin":                    "BOOLEAN NOT NULL DEFAULT 0",
        "is_banned":                   "BOOLEAN NOT NULL DEFAULT 0",
        "banned_until":                "DATETIME",
        "ban_reason":                  "VARCHAR(500)",
        "force_password_reset":        "BOOLEAN NOT NULL DEFAULT 0",
        # Email verification (added for transactional email flow)
        "email_verified":              "INTEGER NOT NULL DEFAULT 0",
        "email_verify_token":          "TEXT",
        "email_verify_token_expires":  "DATETIME",
        # Password reset
        "reset_token":                 "TEXT",
        "reset_token_expires":         "DATETIME",
        # Resend-verification rate limiting
        "verify_resend_count":         "INTEGER NOT NULL DEFAULT 0",
        "verify_resend_window_start":  "DATETIME",
        # Force re-verify on next login for pre-existing users
        "force_email_verify":          "BOOLEAN NOT NULL DEFAULT 0",
    })

    # Backfill: pre-existing users (created before email verification was introduced)
    # are considered verified so they are not locked out.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE users SET email_verified = 1 WHERE email_verified = 0 AND email_verify_token IS NULL"
            ))
    except Exception:
        log_exception(source="sqlite", context={"scope": "fix_db_schema_backfill_email_verified"})
        pass

    # Backfill: when force_email_verify is first added, flag all currently-verified
    # users so they must re-verify through the new system on their next login.
    # This only runs ONCE (when the column is brand new), not on every startup.
    if _force_verify_is_new:
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE users SET force_email_verify = 1 WHERE email_verified = 1"
                ))
        except Exception:
            log_exception(source="sqlite", context={"scope": "fix_db_schema_backfill_force_verify"})
            pass

    # ── teams ──
    _add_missing_cols("teams", {
        "is_draft":       "BOOLEAN DEFAULT 0",
        "is_placeholder": "BOOLEAN DEFAULT 0",
    })

    # ── matches ──
    _add_missing_cols("matches", {
        "margin_type":        "VARCHAR(10)",
        "margin_value":       "INTEGER",
        "toss_winner_team_id":"INTEGER",
        "toss_decision":      "VARCHAR(10)",
        "match_format":       "VARCHAR(20) DEFAULT 'T20'",
        "overs_per_side":     "INTEGER DEFAULT 20",
        "is_day_night":       "BOOLEAN DEFAULT 0",
        "match_json_path":    "VARCHAR(255)",
    })

    # ── announcement_banner ──
    _add_missing_cols("announcement_banner", {
        "color_preset": "VARCHAR(20) DEFAULT 'urgent'",
        "position": "VARCHAR(10) DEFAULT 'bottom'",
    })

    # ── match_scorecards ──
    _add_missing_cols("match_scorecards", {
        "innings_number":     "INTEGER NOT NULL DEFAULT 1",
        "record_type":        "VARCHAR(20) NOT NULL DEFAULT 'batting'",
        "position":           "INTEGER",
        "wicket_taker_name":  "VARCHAR(100)",
        "fielder_name":       "VARCHAR(100)",
        "ones":               "INTEGER DEFAULT 0",
        "twos":               "INTEGER DEFAULT 0",
        "threes":             "INTEGER DEFAULT 0",
        "dot_balls":          "INTEGER DEFAULT 0",
        "strike_rate":        "FLOAT DEFAULT 0.0",
        "batting_position":   "INTEGER",
        "dot_balls_bowled":   "INTEGER DEFAULT 0",
        "wickets_bowled":     "INTEGER DEFAULT 0",
        "wickets_caught":     "INTEGER DEFAULT 0",
        "wickets_lbw":        "INTEGER DEFAULT 0",
        "wickets_stumped":    "INTEGER DEFAULT 0",
        "wickets_run_out":    "INTEGER DEFAULT 0",
        "wickets_hit_wicket": "INTEGER DEFAULT 0",
    })

    # ── tournaments ──
    _add_missing_cols("tournaments", {
        "mode":          "VARCHAR(50) DEFAULT 'round_robin'",
        "current_stage": "VARCHAR(30) DEFAULT 'league'",
        "playoff_teams": "INTEGER DEFAULT 4",
        "series_config": "TEXT",
    })

    # ── tournament_fixtures ──
    _add_missing_cols("tournament_fixtures", {
        "stage":              "VARCHAR(30) DEFAULT 'league'",
        "stage_description":  "VARCHAR(100)",
        "bracket_position":   "INTEGER",
        "winner_team_id":     "INTEGER",
        "series_match_number":"INTEGER",
        "standings_applied":  "BOOLEAN DEFAULT 0",
    })

    # ── auth_event_log ──
    _add_missing_cols("auth_event_log", {
        "event_type":  "VARCHAR(30) NOT NULL DEFAULT ''",
        "email":       "VARCHAR(120) NOT NULL DEFAULT ''",
        "user_id":     "VARCHAR(120)",
        "details":     "TEXT",
        "ip_address":  "VARCHAR(50)",
        "created_at":  "DATETIME",
        "status":      "VARCHAR(20) NOT NULL DEFAULT 'pending'",
        "admin_notes": "TEXT",
        "resolved_at": "DATETIME",
        "resolved_by": "VARCHAR(120)",
    })

    # ── exception_log ──
    _add_missing_cols("exception_log", {
        "severity": "VARCHAR(10) NOT NULL DEFAULT 'error'",
        "source": "VARCHAR(30) NOT NULL DEFAULT 'backend'",
        "context_json": "TEXT",
        "request_id": "VARCHAR(64)",
        "handled": "BOOLEAN NOT NULL DEFAULT 1",
        "resolved": "BOOLEAN NOT NULL DEFAULT 0",
        "resolved_at": "DATETIME",
        "resolved_by": "VARCHAR(120)",
        "fingerprint": "VARCHAR(64)",
        "occurrence_count": "INTEGER NOT NULL DEFAULT 1",
        "first_seen_at": "DATETIME",
        "last_seen_at": "DATETIME",
        "github_issue_number": "INTEGER",
        "github_issue_url": "VARCHAR(300)",
        "github_sync_status": "VARCHAR(20)",
        "github_sync_error": "TEXT",
        "github_last_synced_at": "DATETIME",
    })

    # Indexes for exception_log query performance
    if "exception_log" in tables:
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_exception_timestamp ON exception_log(timestamp)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_exception_type_ts ON exception_log(exception_type, timestamp)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_exception_source_ts ON exception_log(source, timestamp)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_exception_resolved_ts ON exception_log(resolved, timestamp)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_exception_fingerprint ON exception_log(fingerprint)"))
        except Exception:
            log_exception(source="sqlite", context={"scope": "fix_db_schema_exception_indexes"})
            pass

    # ── issue_webhook_event indexes ──
    if "issue_webhook_event" in tables:
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_issue_webhook_event_delivery_id ON issue_webhook_event(delivery_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_issue_webhook_event_issue_number ON issue_webhook_event(github_issue_number)"))
        except Exception:
            log_exception(source="sqlite", context={"scope": "fix_db_schema_issue_webhook_event_indexes"})
            pass


def fix_db_schema():
    from app import create_app

    app = create_app()
    with app.app_context():
        print("Creating all missing database tables...")
        try:
            db.create_all()
            ensure_schema(db.engine, db)
            print("Schema check complete.")
        except Exception as e:
            log_exception(e, source="sqlite", context={"scope": "fix_db_schema"})
            print(f"Error creating tables: {e}")


if __name__ == "__main__":
    fix_db_schema()
