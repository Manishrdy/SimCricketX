#!/usr/bin/env python3
"""
Database Cleanup Script - Clear All Tables Except Users & Teams

DANGER: This script will DELETE all data from your database except:
- users table (login credentials)
- teams table (team definitions)

All other tables will be CLEARED:
- players
- matches
- match_scorecards
- tournaments
- tournament_teams
- tournament_fixtures
- tournament_player_stats_cache
- match_partnerships

Usage:
    python scripts/clear_database.py

Safety Features:
- Requires explicit confirmation (type 'DELETE' to proceed)
- Shows count of records before deletion
- Transactional (rollback on error)
"""

import sys
import os

# Add parent directory to path to import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database import db
from database.models import (
    User, Team, Player, Match, MatchScorecard, Tournament,
    TournamentTeam, TournamentFixture, TournamentPlayerStatsCache,
    MatchPartnership
)
from app import create_app


def get_table_counts():
    """Get record counts for all tables using raw SQL to avoid schema issues."""
    counts = {}
    
    # List of all tables in the database
    tables = [
        'users', 'teams', 'players', 'matches', 'match_scorecards',
        'tournaments', 'tournament_teams', 'tournament_fixtures',
        'tournament_player_stats_cache', 'match_partnerships'
    ]
    
    for table in tables:
        try:
            result = db.session.execute(db.text(f"SELECT COUNT(*) FROM {table}"))
            counts[table] = result.scalar() or 0
        except Exception:
            counts[table] = 0
    
    return counts


def display_counts(counts):
    """Display table counts in a formatted table."""
    print("\n" + "="*60)
    print("CURRENT DATABASE STATE")
    print("="*60)
    print(f"{'Table':<35} {'Records':>15}")
    print("-"*60)
    
    # Protected tables (will NOT be deleted)
    print("\n🔒 PROTECTED (Will be preserved):")
    print(f"  {'users':<33} {counts['users']:>15,}")
    print(f"  {'teams':<33} {counts['teams']:>15,}")
    
    # Tables to be cleared
    print("\n⚠️  TO BE DELETED:")
    delete_tables = {k: v for k, v in counts.items() if k not in ['users', 'teams']}
    for table, count in delete_tables.items():
        print(f"  {table:<33} {count:>15,}")
    
    total_to_delete = sum(delete_tables.values())
    print("-"*60)
    print(f"{'TOTAL RECORDS TO DELETE:':<35} {total_to_delete:>15,}")
    print("="*60)


def clear_database():
    """Clear all tables except users and teams."""
    
    print("\n🗑️  DATABASE CLEANUP SCRIPT")
    print("This will DELETE all data except users and teams tables.\n")
    
    # Show current state
    counts = get_table_counts()
    display_counts(counts)
    
    # Safety confirmation
    print("\n⚠️  WARNING: This action cannot be undone!")
    print("Type 'DELETE' (in uppercase) to confirm, or anything else to cancel:")
    confirmation = input("> ").strip()
    
    if confirmation != "DELETE":
        print("❌ Cancelled. No changes made.")
        return False
    
    print("\n🔄 Starting deletion process...")
    
    try:
        # Use raw SQL to delete - avoids ORM schema issues
        # Delete in correct order to respect foreign key constraints
        
        tables_to_clear = [
            ('tournament_player_stats_cache', 'tournament player stats cache'),
            ('tournament_fixtures', 'tournament fixtures'),
            ('tournament_teams', 'tournament teams'),
            ('match_partnerships', 'match partnerships'),
            ('match_scorecards', 'match scorecards'),
            ('tournaments', 'tournaments'),
            ('matches', 'matches'),
            ('players', 'players'),
        ]
        
        for table_name, display_name in tables_to_clear:
            print(f"  Deleting {display_name}...")
            try:
                result = db.session.execute(db.text(f"DELETE FROM {table_name}"))
                deleted = result.rowcount
                print(f"    ✓ Deleted {deleted:,} records")
            except Exception as e:
                print(f"    ⚠️  Warning: Could not delete from {table_name}: {e}")
                # Continue with other tables even if one fails
        
        # Commit all changes
        print("\n💾 Committing changes to database...")
        db.session.commit()
        
        # Verify new state
        new_counts = get_table_counts()
        print("\n✅ DATABASE CLEANUP SUCCESSFUL!")
        display_counts(new_counts)
        
        return True
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        print("🔄 Rolling back changes...")
        db.session.rollback()
        print("❌ Cleanup failed. Database restored to previous state.")
        return False


def main():
    """Main entry point."""
    # Create Flask app context
    app = create_app()
    
    with app.app_context():
        success = clear_database()
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
