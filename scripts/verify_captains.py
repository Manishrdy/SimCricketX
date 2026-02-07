#!/usr/bin/env python3
"""Test captain selection fix"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database import db
from database.models import Team, Player
from app import create_app

app = create_app()

print("\n" + "="*80)
print("CAPTAIN SELECTION VERIFICATION")
print("="*80)

with app.app_context():
    teams = db.session.query(Team).all()
    
    for team in teams:
        players = Player.query.filter_by(team_id=team.id).all()
        if not players:
            continue
            
        print(f"\nTeam: {team.name} ({team.short_code})")
        
        captain = next((p for p in players if p.is_captain), None)
        if captain:
            print(f"  Captain: {captain.name}")
        else:
            print(f"  No captain designated")
        
        print(f"  First player (old logic would use): {players[0].name if players else 'N/A'}")
        
        if captain and players and captain.name != players[0].name:
            print(f"  ** FIXED: Captain is NOT first player! **")
        
        print("-"*80)

print("\nVerification complete!")
