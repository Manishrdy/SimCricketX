from database import db


def ensure_schema(engine, db_obj=None):
    """
    Idempotent schema guard. Safe to run at startup.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = inspector.get_table_names()

    # Teams.is_draft
    if "teams" in tables:
        cols = [c["name"] for c in inspector.get_columns("teams")]
        if "is_draft" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE teams ADD COLUMN is_draft BOOLEAN DEFAULT 0"))

    # match_scorecards required columns
    if "match_scorecards" in tables:
        cols = [c["name"] for c in inspector.get_columns("match_scorecards")]
        alters = []
        if "innings_number" not in cols:
            alters.append("ALTER TABLE match_scorecards ADD COLUMN innings_number INTEGER NOT NULL DEFAULT 1")
        if "record_type" not in cols:
            alters.append("ALTER TABLE match_scorecards ADD COLUMN record_type VARCHAR(20) NOT NULL DEFAULT 'batting'")
        if "position" not in cols:
            alters.append("ALTER TABLE match_scorecards ADD COLUMN position INTEGER")
        if alters:
            with engine.begin() as conn:
                for stmt in alters:
                    conn.execute(text(stmt))

    # Ensure tournament_player_stats_cache exists
    if "tournament_player_stats_cache" not in tables:
        from database.models import TournamentPlayerStatsCache  # noqa: F401
        if db_obj is not None:
            db_obj.create_all()

    # Ensure admin_audit_log exists
    if "admin_audit_log" not in tables and db_obj is not None:
        db_obj.create_all()

    # User ban/suspend and force password reset columns
    if "users" in tables:
        cols = [c["name"] for c in inspector.get_columns("users")]
        alters = []
        if "is_banned" not in cols:
            alters.append("ALTER TABLE users ADD COLUMN is_banned BOOLEAN NOT NULL DEFAULT 0")
        if "banned_until" not in cols:
            alters.append("ALTER TABLE users ADD COLUMN banned_until DATETIME")
        if "ban_reason" not in cols:
            alters.append("ALTER TABLE users ADD COLUMN ban_reason VARCHAR(500)")
        if "force_password_reset" not in cols:
            alters.append("ALTER TABLE users ADD COLUMN force_password_reset BOOLEAN NOT NULL DEFAULT 0")
        if alters:
            with engine.begin() as conn:
                for stmt in alters:
                    conn.execute(text(stmt))

    # New tables for admin features and site counters
    missing_new = [t for t in ("failed_login_attempts", "blocked_ips", "active_sessions", "site_counters") if t not in tables]
    if missing_new and db_obj is not None:
        from database.models import FailedLoginAttempt, BlockedIP, ActiveSession, SiteCounter  # noqa: F401
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
