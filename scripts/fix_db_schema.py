import sys
import os

# Add parent directory to path to import app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from database.models import User, Team, Player, Match, MatchScorecard, Tournament, TournamentTeam, TournamentFixture

def ensure_schema(engine):
    """
    Idempotent schema guard. Safe to run at startup.
    """
    from sqlalchemy import inspect

    inspector = inspect(engine)
    tables = inspector.get_table_names()

    # Teams.is_draft
    if "teams" in tables:
        cols = [c["name"] for c in inspector.get_columns("teams")]
        if "is_draft" not in cols:
            with engine.begin() as conn:
                conn.execute("ALTER TABLE teams ADD COLUMN is_draft BOOLEAN DEFAULT 0")

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
                    conn.execute(stmt)


def fix_db_schema():
    app = create_app()
    with app.app_context():
        print("Creating all missing database tables...")
        try:
            db.create_all()
            ensure_schema(db.engine)
            print("✅ Schema check complete.")
        except Exception as e:
            print(f"Error creating tables: {e}")

if __name__ == "__main__":
    fix_db_schema()
