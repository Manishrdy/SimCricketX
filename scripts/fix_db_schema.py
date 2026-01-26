import sys
import os

# Add parent directory to path to import app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from database.models import User, Team, Player, Match, MatchScorecard, Tournament, TournamentTeam, TournamentFixture

def fix_db_schema():
    app = create_app()
    with app.app_context():
        print("Creating all missing database tables...")
        try:
            db.create_all()
            print("Successfully ran db.create_all().")
            
            # Verify teams table exists
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            tables = inspector.get_table_names()
            if 'teams' in tables:
                print("✅ Table 'teams' exists.")
                
                # Verify column
                columns = [c['name'] for c in inspector.get_columns('teams')]
                if 'is_draft' in columns:
                    print("✅ Column 'is_draft' exists in 'teams'.")
                else:
                    print("❌ Column 'is_draft' MISSING in 'teams'. Attempting migration...")
                    try:
                        with db.engine.connect() as conn:
                            conn.execute("ALTER TABLE teams ADD COLUMN is_draft BOOLEAN DEFAULT 0")
                        print("✅ Added 'is_draft' column to 'teams'.")
                    except Exception as e:
                        print(f"Failed to add column: {e}")

            else:
                print("❌ Table 'teams' failed to create.")

        except Exception as e:
            print(f"Error creating tables: {e}")

if __name__ == "__main__":
    fix_db_schema()
