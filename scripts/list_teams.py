#!/usr/bin/env python3
"""Quick script to list all teams in the database"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database import db
from database.models import Team
from app import create_app

app = create_app()

with app.app_context():
    # Use raw SQL to avoid schema mismatch issues
    result = db.session.execute(db.text("SELECT * FROM teams"))
    teams = result.fetchall()
    columns = result.keys()
    
    print("\n" + "="*80)
    print("TEAMS IN DATABASE")
    print("="*80)
    print(f"Total Teams: {len(teams)}\n")
    
    for team in teams:
        # Convert to dict for easier access
        team_dict = dict(zip(columns, team))
        print(f"ID: {team_dict.get('id')}")
        print(f"  Name: {team_dict.get('name')}")
        print(f"  Short Code: {team_dict.get('short_code')}")
        print(f"  Owner: {team_dict.get('user_id')}")
        print(f"  Home Ground: {team_dict.get('home_ground', 'N/A')}")
        print(f"  Created: {team_dict.get('created_at')}")
        print(f"  Draft: {team_dict.get('is_draft', False)}")
        print("-"*80)
