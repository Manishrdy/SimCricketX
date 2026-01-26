"""
Migration script to add tournament mode columns to existing database.
Run this once to update the schema without losing data.
"""

import sqlite3
import os

# Find the database file
DB_PATH = os.path.join(os.path.dirname(__file__), 'cricket_sim.db')

# Alternative paths to check
ALT_PATHS = [
    os.path.join(os.path.dirname(__file__), 'instance', 'simcricket.db'),
    os.path.join(os.path.dirname(__file__), 'simcricket.db'),
    os.path.join(os.path.dirname(__file__), 'database', 'simcricket.db'),
]

def find_database():
    """Find the database file."""
    if os.path.exists(DB_PATH):
        return DB_PATH
    for path in ALT_PATHS:
        if os.path.exists(path):
            return path
    return None

def get_existing_columns(cursor, table_name):
    """Get list of existing columns in a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]

def migrate():
    """Run the migration."""
    db_path = find_database()

    if not db_path:
        print("ERROR: Could not find database file!")
        print(f"Checked paths:")
        print(f"  - {DB_PATH}")
        for path in ALT_PATHS:
            print(f"  - {path}")
        return False

    print(f"Found database at: {db_path}")

    # Connect to database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # ========================================
        # MIGRATE TOURNAMENTS TABLE
        # ========================================
        print("\n--- Migrating 'tournaments' table ---")

        existing_cols = get_existing_columns(cursor, 'tournaments')
        print(f"Existing columns: {existing_cols}")

        # Add 'mode' column
        if 'mode' not in existing_cols:
            cursor.execute("""
                ALTER TABLE tournaments
                ADD COLUMN mode VARCHAR(50) DEFAULT 'round_robin' NOT NULL
            """)
            print("  Added column: mode")
        else:
            print("  Column 'mode' already exists")

        # Add 'current_stage' column
        if 'current_stage' not in existing_cols:
            cursor.execute("""
                ALTER TABLE tournaments
                ADD COLUMN current_stage VARCHAR(30) DEFAULT 'league' NOT NULL
            """)
            print("  Added column: current_stage")
        else:
            print("  Column 'current_stage' already exists")

        # Add 'playoff_teams' column
        if 'playoff_teams' not in existing_cols:
            cursor.execute("""
                ALTER TABLE tournaments
                ADD COLUMN playoff_teams INTEGER DEFAULT 4 NOT NULL
            """)
            print("  Added column: playoff_teams")
        else:
            print("  Column 'playoff_teams' already exists")

        # Add 'series_config' column
        if 'series_config' not in existing_cols:
            cursor.execute("""
                ALTER TABLE tournaments
                ADD COLUMN series_config TEXT
            """)
            print("  Added column: series_config")
        else:
            print("  Column 'series_config' already exists")

        # ========================================
        # MIGRATE TOURNAMENT_FIXTURES TABLE
        # ========================================
        print("\n--- Migrating 'tournament_fixtures' table ---")

        existing_cols = get_existing_columns(cursor, 'tournament_fixtures')
        print(f"Existing columns: {existing_cols}")

        # Add 'stage' column
        if 'stage' not in existing_cols:
            cursor.execute("""
                ALTER TABLE tournament_fixtures
                ADD COLUMN stage VARCHAR(30) DEFAULT 'league' NOT NULL
            """)
            print("  Added column: stage")
        else:
            print("  Column 'stage' already exists")

        # Add 'stage_description' column
        if 'stage_description' not in existing_cols:
            cursor.execute("""
                ALTER TABLE tournament_fixtures
                ADD COLUMN stage_description VARCHAR(100)
            """)
            print("  Added column: stage_description")
        else:
            print("  Column 'stage_description' already exists")

        # Add 'bracket_position' column
        if 'bracket_position' not in existing_cols:
            cursor.execute("""
                ALTER TABLE tournament_fixtures
                ADD COLUMN bracket_position INTEGER
            """)
            print("  Added column: bracket_position")
        else:
            print("  Column 'bracket_position' already exists")

        # Add 'winner_team_id' column
        if 'winner_team_id' not in existing_cols:
            cursor.execute("""
                ALTER TABLE tournament_fixtures
                ADD COLUMN winner_team_id INTEGER REFERENCES teams(id)
            """)
            print("  Added column: winner_team_id")
        else:
            print("  Column 'winner_team_id' already exists")

        # Add 'series_match_number' column
        if 'series_match_number' not in existing_cols:
            cursor.execute("""
                ALTER TABLE tournament_fixtures
                ADD COLUMN series_match_number INTEGER
            """)
            print("  Added column: series_match_number")
        else:
            print("  Column 'series_match_number' already exists")

        # Add 'standings_applied' column
        if 'standings_applied' not in existing_cols:
            cursor.execute("""
                ALTER TABLE tournament_fixtures
                ADD COLUMN standings_applied BOOLEAN DEFAULT 0 NOT NULL
            """)
            print("  Added column: standings_applied")
        else:
            print("  Column 'standings_applied' already exists")

        # ========================================
        # CREATE NEW INDEX
        # ========================================
        print("\n--- Creating indexes ---")

        # Check if index exists
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='index' AND name='ix_fixture_tournament_stage'
        """)
        if not cursor.fetchone():
            cursor.execute("""
                CREATE INDEX ix_fixture_tournament_stage
                ON tournament_fixtures(tournament_id, stage)
            """)
            print("  Created index: ix_fixture_tournament_stage")
        else:
            print("  Index 'ix_fixture_tournament_stage' already exists")

        # Commit all changes
        conn.commit()
        print("\n✓ Migration completed successfully!")

        # Show updated schema
        print("\n--- Updated 'tournaments' columns ---")
        for col in get_existing_columns(cursor, 'tournaments'):
            print(f"  - {col}")

        print("\n--- Updated 'tournament_fixtures' columns ---")
        for col in get_existing_columns(cursor, 'tournament_fixtures'):
            print(f"  - {col}")

        return True

    except Exception as e:
        conn.rollback()
        print(f"\nERROR: Migration failed - {e}")
        return False
    finally:
        conn.close()

if __name__ == "__main__":
    print("=" * 50)
    print("Tournament Modes Migration Script")
    print("=" * 50)
    migrate()
