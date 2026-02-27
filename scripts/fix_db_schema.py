from database import db


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

    # ── users ──
    _add_missing_cols("users", {
        "stable_id":            "VARCHAR(36)",
        "last_login":           "DATETIME",
        "ip_address":           "VARCHAR(50)",
        "mac_address":          "VARCHAR(50)",
        "hostname":             "VARCHAR(100)",
        "display_name":         "VARCHAR(100)",
        "is_admin":             "BOOLEAN NOT NULL DEFAULT 0",
        "is_banned":            "BOOLEAN NOT NULL DEFAULT 0",
        "banned_until":         "DATETIME",
        "ban_reason":           "VARCHAR(500)",
        "force_password_reset": "BOOLEAN NOT NULL DEFAULT 0",
    })

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

    # ── tournament_player_stats_cache ──
    # Older DBs may have an early, partial version of this table.
    # Keep definitions sqlite-safe (avoid NOT NULL on ALTER for existing rows).
    _add_missing_cols("tournament_player_stats_cache", {
        "team_id": "INTEGER",
        "matches_played": "INTEGER DEFAULT 0",
        "innings_batted": "INTEGER DEFAULT 0",
        "runs_scored": "INTEGER DEFAULT 0",
        "balls_faced": "INTEGER DEFAULT 0",
        "fours": "INTEGER DEFAULT 0",
        "sixes": "INTEGER DEFAULT 0",
        "not_outs": "INTEGER DEFAULT 0",
        "highest_score": "INTEGER DEFAULT 0",
        "fifties": "INTEGER DEFAULT 0",
        "centuries": "INTEGER DEFAULT 0",
        "batting_average": "FLOAT DEFAULT 0.0",
        "batting_strike_rate": "FLOAT DEFAULT 0.0",
        "innings_bowled": "INTEGER DEFAULT 0",
        "overs_bowled": "VARCHAR(10) DEFAULT '0.0'",
        "runs_conceded": "INTEGER DEFAULT 0",
        "wickets_taken": "INTEGER DEFAULT 0",
        "maidens": "INTEGER DEFAULT 0",
        "best_bowling_wickets": "INTEGER DEFAULT 0",
        "best_bowling_runs": "INTEGER DEFAULT 0",
        "five_wicket_hauls": "INTEGER DEFAULT 0",
        "bowling_average": "FLOAT DEFAULT 0.0",
        "bowling_economy": "FLOAT DEFAULT 0.0",
        "bowling_strike_rate": "FLOAT DEFAULT 0.0",
        "catches": "INTEGER DEFAULT 0",
        "run_outs": "INTEGER DEFAULT 0",
        "stumpings": "INTEGER DEFAULT 0",
    })

    # Best-effort backfill for newly added team_id using players.team_id.
    # Safe to run repeatedly; only fills NULL values.
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE tournament_player_stats_cache
                SET team_id = (
                    SELECT players.team_id
                    FROM players
                    WHERE players.id = tournament_player_stats_cache.player_id
                )
                WHERE team_id IS NULL
            """))
    except Exception:
        # Keep schema guard non-fatal during startup.
        pass

    # ── Ensure all tables exist (creates any missing ones) ──
    all_required_tables = (
        "tournament_player_stats_cache", "admin_audit_log",
        "match_partnerships", "failed_login_attempts",
        "blocked_ips", "active_sessions", "site_counters",
        "login_history", "ip_whitelist",
    )
    missing = [t for t in all_required_tables if t not in tables]
    if missing and db_obj is not None:
        # Import all models so SQLAlchemy knows about them
        from database.models import (  # noqa: F401
            TournamentPlayerStatsCache, AdminAuditLog, MatchPartnership,
            FailedLoginAttempt, BlockedIP, ActiveSession, SiteCounter,
            LoginHistory, IPWhitelistEntry,
        )
        db_obj.create_all()


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
            print(f"Error creating tables: {e}")


if __name__ == "__main__":
    fix_db_schema()
