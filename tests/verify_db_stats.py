import sys
import os
from sqlalchemy import create_engine

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from database import db
from database.models import User, Team, Player

def verify_stats():
    app = create_app()
    
    # Ensure we use the correct db URI
    basedir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    db_path = os.path.join(basedir, 'cricket_sim.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
    
    with app.app_context():
        print(f"Checking DB at {db_path}...")
        
        # Check User
        user = User.query.first()
        if not user:
            print("❌ No users found!")
            return
        print(f"✅ Found User: {user.id}")
        
        # Check Teams
        teams_count = Team.query.count()
        print(f"✅ Found {teams_count} Teams")
        
        # Check Players
        player_count = Player.query.count()
        print(f"✅ Found {player_count} Players")
        
        # Check specific stats aggregation (should be 0 initially after migration before any match)
        top_scorer = Player.query.order_by(Player.total_runs.desc()).first()
        if top_scorer:
            print(f"ℹ️ Top Scorer (Currently): {top_scorer.name} - {top_scorer.total_runs} runs")
        
        print("\nVerification Passed!")

if __name__ == "__main__":
    verify_stats()
