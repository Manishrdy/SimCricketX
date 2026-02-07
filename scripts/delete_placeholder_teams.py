#!/usr/bin/env python3
"""Delete specific teams (BYE and TBD) from the database"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database import db
from app import create_app

app = create_app()

with app.app_context():
    # Delete BYE and TBD teams using raw SQL
    print("\n🗑️  Deleting placeholder teams...")
    
    # Get teams before deletion
    result = db.session.execute(db.text("SELECT id, name, short_code FROM teams WHERE short_code IN ('BYE', 'TBD')"))
    teams_to_delete = result.fetchall()
    
    if not teams_to_delete:
        print("❌ No BYE or TBD teams found.")
    else:
        print(f"\nFound {len(teams_to_delete)} teams to delete:")
        for team in teams_to_delete:
            print(f"  - ID {team[0]}: {team[1]} ({team[2]})")
        
        # Delete the teams
        result = db.session.execute(db.text("DELETE FROM teams WHERE short_code IN ('BYE', 'TBD')"))
        deleted_count = result.rowcount
        
        # Commit the changes
        db.session.commit()
        
        print(f"\n✅ Successfully deleted {deleted_count} teams")
    
    # Show remaining teams
    print("\n" + "="*80)
    print("REMAINING TEAMS")
    print("="*80)
    result = db.session.execute(db.text("SELECT id, name, short_code, home_ground FROM teams"))
    remaining_teams = result.fetchall()
    
    print(f"Total: {len(remaining_teams)} teams\n")
    for team in remaining_teams:
        print(f"ID {team[0]}: {team[1]} ({team[2]})")
        print(f"  Home: {team[3] or 'N/A'}")
        print("-"*80)
