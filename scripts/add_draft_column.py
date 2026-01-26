import sqlite3
import sys
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'cricket_sim.db')

def migrate():
    print(f"Migrating database at: {DB_PATH}")
    if not os.path.exists(DB_PATH):
        print("Database not found!")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        # Check if 'is_draft' column exists in 'teams' table
        cursor.execute("PRAGMA table_info(teams)")
        columns = [info[1] for info in cursor.fetchall()]
        
        if 'is_draft' not in columns:
            print("Adding 'is_draft' column to 'teams' table...")
            cursor.execute("ALTER TABLE teams ADD COLUMN is_draft BOOLEAN DEFAULT 0")
            conn.commit()
            print("Successfully added 'is_draft' column.")
        else:
            print("'is_draft' column already exists.")

    except Exception as e:
        print(f"Error during migration: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()
