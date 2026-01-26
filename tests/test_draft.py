import sys
import os
import unittest
from flask_login import current_user

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db, User as AppUser
from database.models import User, Team, Player

class TestDraftFeature(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        
        # Mock user loader for testing to bypass credentials.json check
        @self.app.login_manager.user_loader
        def load_user(user_id):
            return AppUser(user_id)

        self.client = self.app.test_client()
        
        with self.app.app_context():
            db.create_all()
            # Create dummy user
            user = User(id="test@example.com")
            user.password_hash = "dummy"
            db.session.add(user)
            db.session.commit()
            
    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def login(self):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = "test@example.com"

    def test_create_draft_team(self):
        self.login()
        with self.app.app_context():
             # Data for a minimal team (5 players) - Should fail publish, pass draft
            data = {
                "team_name": "Draft Kings",
                "short_code": "DK",
                "home_ground": "Test Ground",
                "pitch_preference": "Flat",
                "team_color": "#000000",
                "action": "save_draft",
                "player_name": ["P1", "P2", "P3", "P4", "P5"],
                "player_role": ["Batsman"]*5,
                "batting_rating": ["50"]*5,
                "bowling_rating": ["50"]*5,
                "fielding_rating": ["50"]*5,
                "batting_hand": ["Right"]*5,
                "bowling_type": [""]*5,
                "bowling_hand": [""]*5,
                "captain": "P1",
                "wicketkeeper": "P2"
            }
            
            # 1. Test Draft Save
            resp = self.client.post('/team/create', data=data, follow_redirects=True)
            if resp.status_code != 200 or b"Team saved as draft" not in resp.data: # Flash message check might be tricky if not rendered
                 print(f"Response Status: {resp.status_code}")
                 print(f"Response Data: {resp.data}")

            self.assertEqual(resp.status_code, 200)
            
            # Verify DB
            team = Team.query.filter_by(short_code="DK").first()
            if not team:
                print("❌ Team DK not found in DB")
            self.assertIsNotNone(team)
            self.assertTrue(team.is_draft)
            self.assertEqual(len(team.players), 5)
            print("✅ Draft creation with 5 players successful")

    def test_publish_validation(self):
        self.login()
        with self.app.app_context():
            # Try to publish with 5 players (Should fail)
            data = {
                "team_name": "Fail Team",
                "short_code": "FT",
                "home_ground": "Test Ground",
                "pitch_preference": "Flat",
                "team_color": "#000000",
                "action": "publish",
                 "player_name": ["P1", "P2", "P3", "P4", "P5"],
                "player_role": ["Batsman"]*5,
                "batting_rating": ["50"]*5,
                "bowling_rating": ["50"]*5,
                "fielding_rating": ["50"]*5,
                "batting_hand": ["Right"]*5,
                "bowling_type": [""]*5,
                "bowling_hand": [""]*5,
                "captain": "P1",
                "wicketkeeper": "P2"
            }
            resp = self.client.post('/team/create', data=data, follow_redirects=True)
            self.assertIn(b"You must enter between 12 and 25 players", resp.data)
            print("✅ Strict validation caught invalid team size")

    def test_successful_publish(self):
        self.login()
        with self.app.app_context():
            # 12 Players, Valid Roles
            names = [f"P{i}" for i in range(12)]
            roles = ["Batsman"] * 5 + ["Wicketkeeper"] + ["Bowler"] * 6
            print(f"Testing with {len(names)} players...")
            
            data = {
                "team_name": "Valid Team",
                "short_code": "VT",
                "home_ground": "Test Ground",
                "pitch_preference": "Flat",
                "team_color": "#000000",
                "action": "publish",
                "player_name": names,
                "player_role": roles,
                "batting_rating": ["50"]*12,
                "bowling_rating": ["50"]*12,
                "fielding_rating": ["50"]*12,
                "batting_hand": ["Right"]*12,
                "bowling_type": [""]*12,
                "bowling_hand": [""]*12,
                "captain": "P0",
                "wicketkeeper": "P5"
            }
            resp = self.client.post('/team/create', data=data, follow_redirects=True)
            if resp.status_code != 200:
                print(f"Response Status: {resp.status_code}")
                print(f"Response Data: {resp.data}")
            
            # Verify DB
            team = Team.query.filter_by(short_code="VT").first()
            if not team:
                print("❌ Team VT not found in DB")
                # Print response to see errors
                print(resp.data)
            self.assertIsNotNone(team)
            self.assertFalse(team.is_draft)
            print("✅ Valid team published successfully")

if __name__ == '__main__':
    unittest.main()
