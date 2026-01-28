"""
Database Migration Script
==========================

This script adds new columns to the database for enhanced match data capture.

Run this script ONCE to update your existing database schema.

Usage:
    python migrations/migrate_enhanced_stats.py
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db
from database.models import Match, MatchScorecard, MatchPartnership
from app import create_app

def migrate_database():
    """Apply database migrations for enhanced stats"""
    print("=" * 60)
    print("Enhanced Match Data Capture - Database Migration")
    print("=" * 60)
    print()
    
    app = create_app()
    
    with app.app_context():
        print("📊 Creating database backup...")
        # Note: User should manually backup their database before running this
        print("⚠️  Please ensure you have backed up your database!")
        print()
        
        response = input("Continue with migration? (yes/no): ")
        if response.lower() != 'yes':
            print("Migration cancelled.")
            return
        
        print()
        print("🔨 Applying schema changes...")
        
        try:
            # Create all new tables and columns
            db.create_all()
            
            print("✅ Successfully created new tables and columns:")
            print("   - Match: margin_type, margin_value, toss_winner_team_id, toss_decision, match_format, overs_per_side")
            print("   - MatchScorecard: ones, twos, threes, dot_balls, strike_rate, batting_position")
            print("   - MatchScorecard: dot_balls_bowled, wickets_bowled, wickets_caught, wickets_lbw, wickets_stumped, wickets_run_out, wickets_hit_wicket")
            print("   - MatchPartnership: New table for partnership tracking")
            print()
            
            print("🎉 Migration completed successfully!")
            print()
            print("📝 Next steps:")
            print("   1. All future matches will automatically capture enhanced stats")
            print("   2. Existing matches will have NULL values for new fields")
            print("   3. Re-simulate existing matches to populate enhanced stats")
            print()
            
        except Exception as e:
            print(f"❌ Migration failed: {e}")
            print()
            print("Please check the error message and try again.")
            print("If the error persists, restore from backup and contact support.")
            return
    
    print("=" * 60)

if __name__ == "__main__":
    migrate_database()
