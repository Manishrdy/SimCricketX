import sys
import os
import json
import glob
from sqlalchemy import create_engine
from datetime import datetime

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from database import db
from database.models import User, Team, Player, Match, MatchScorecard

def migrate_data():
    app = create_app()
    
    # Configure DB URI 
    # (We need to set this here because app.py might not have it yet during migration)
    basedir = os.path.abspath(os.path.dirname(__file__))
    db_path = os.path.join(os.path.dirname(basedir), 'cricket_sim.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    db.init_app(app)
    
    with app.app_context():
        print(f"Creating database tables at {db_path}...")
        db.create_all()
        
        # 1. Migrate Users
        print("\n--- Migrating Users ---")
        creds_path = os.path.join(os.path.dirname(basedir), 'auth', 'credentials.json')
        if os.path.exists(creds_path):
            with open(creds_path, 'r') as f:
                creds = json.load(f)
                
            for email, data in creds.items():
                if not User.query.get(email):
                    user = User(
                        id=email, 
                        password_hash=data.get('password') # In real app, re-hash if needed
                    )
                    db.session.add(user)
                    print(f"Added User: {email}")
            
            db.session.commit()
        
        # 2. Migrate Teams & Players
        print("\n--- Migrating Teams & Players ---")
        teams_dir = os.path.join(os.path.dirname(basedir), 'data', 'teams')
        json_files = glob.glob(os.path.join(teams_dir, "*.json"))
        
        for json_file in json_files:
            try:
                with open(json_file, 'r') as f:
                    data = json.load(f)
                
                # Check for required fields
                if 'short_code' not in data or 'created_by_email' not in data:
                    print(f"Skipping invalid team file: {os.path.basename(json_file)}")
                    continue
                    
                # Check if team exists (by name + owner)
                existing_team = Team.query.filter_by(
                    name=data['team_name'], 
                    user_id=data['created_by_email']
                ).first()
                
                if not existing_team:
                    new_team = Team(
                        user_id=data['created_by_email'],
                        name=data['team_name'],
                        short_code=data['short_code'],
                        home_ground=data.get('home_ground', 'Unknown'),
                        pitch_preference=data.get('pitch_preference', 'Hard'),
                        team_color=data.get('team_color', '#000000')
                    )
                    db.session.add(new_team)
                    db.session.flush() # Get ID
                    
                    # Add Players
                    for p_data in data.get('players', []):
                        player = Player(
                            team_id=new_team.id,
                            name=p_data['name'],
                            role=p_data['role'],
                            batting_rating=p_data.get('batting_rating', 0),
                            bowling_rating=p_data.get('bowling_rating', 0),
                            fielding_rating=p_data.get('fielding_rating', 0),
                            batting_hand=p_data.get('batting_hand', 'Right'),
                            bowling_type=p_data.get('bowling_type', ''),
                            bowling_hand=p_data.get('bowling_hand', 'Right'),
                            is_captain=(p_data['name'] == data.get('captain')),
                            is_wicketkeeper=(p_data['name'] == data.get('wicketkeeper'))
                        )
                        db.session.add(player)
                    
                    print(f"Migrated Team: {new_team.name} ({len(data.get('players', []))} players)")
            except Exception as e:
                print(f"Error migrating {os.path.basename(json_file)}: {e}")
        
        db.session.commit()
        print("Data migration completed successfully!")

if __name__ == "__main__":
    migrate_data()
