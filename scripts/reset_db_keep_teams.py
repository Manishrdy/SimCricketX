import sys
import os
import logging

# Add parent directory to path to import app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from database import db
from database.models import MatchScorecard, TournamentFixture, TournamentTeam, Match, Tournament, Player

def reset_database():
    app = create_app()
    with app.app_context():
        print("Starting database cleanup (preserving Teams/Players)...")
        
        try:
            # 1. Delete Scorecards
            deleted_scorecards = MatchScorecard.query.delete()
            print(f"Deleted {deleted_scorecards} match scorecards.")

            # 2. Delete Tournament Fixtures
            deleted_fixtures = TournamentFixture.query.delete()
            print(f"Deleted {deleted_fixtures} tournament fixtures.")

            # 3. Delete Tournament Teams (Stats)
            deleted_tourn_teams = TournamentTeam.query.delete()
            print(f"Deleted {deleted_tourn_teams} tournament team entries.")

            # 4. Delete Matches
            deleted_matches = Match.query.delete()
            print(f"Deleted {deleted_matches} matches.")

            # 5. Delete Tournaments
            deleted_tournaments = Tournament.query.delete()
            print(f"Deleted {deleted_tournaments} tournaments.")

            # 6. Reset Player Stats
            print("Resetting player stats...")
            players = Player.query.all()
            for p in players:
                # Batting
                p.matches_played = 0
                p.total_runs = 0
                p.total_balls_faced = 0
                p.total_fours = 0
                p.total_sixes = 0
                p.total_fifties = 0
                p.total_centuries = 0
                p.highest_score = 0
                p.not_outs = 0
                
                # Bowling
                p.total_balls_bowled = 0
                p.total_runs_conceded = 0
                p.total_wickets = 0
                p.total_maidens = 0
                p.five_wicket_hauls = 0
                p.best_bowling_wickets = 0
                p.best_bowling_runs = 0
                
            print(f"Reset stats for {len(players)} players.")

            db.session.commit()
            print("Database cleanup completed successfully!")
            
        except Exception as e:
            db.session.rollback()
            print(f"Error during cleanup: {e}")
            sys.exit(1)

if __name__ == "__main__":
    reset_database()
