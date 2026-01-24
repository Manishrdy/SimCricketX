
import sys
import os
from tabulate import tabulate
from app import create_app
from database import db
from database.models import User, Team, Player, Match, MatchScorecard, Tournament, TournamentFixture, TournamentTeam

# Map friendly names to Models
MODELS = {
    "1": ("Users", User),
    "2": ("Teams", Team),
    "3": ("Players", Player),
    "4": ("Matches", Match),
    "5": ("Tournaments", Tournament),
    "6": ("TournamentFixtures", TournamentFixture),
    "7": ("TournamentStats (Standings)", TournamentTeam),
    "8": ("Scorecards", MatchScorecard)
}

def view_table(model_name, model_class):
    print(f"\n--- {model_name} ---\n")
    try:
        # Get all records
        records = model_class.query.all()
        
        if not records:
            print("No records found.")
            return

        # Dynamically get columns
        columns = [c.name for c in model_class.__table__.columns]
        
        data = []
        for r in records:
            row = []
            for c in columns:
                val = getattr(r, c)
                # Truncate long strings for display
                if isinstance(val, str) and len(val) > 50:
                    val = val[:47] + "..."
                row.append(val)
            data.append(row)
            
        print(tabulate(data, headers=columns, tablefmt="grid"))
        print(f"\nTotal: {len(records)} records")

    except Exception as e:
        print(f"Error viewing table: {e}")

def main():
    app = create_app()
    with app.app_context():
        while True:
            print("\n" + "="*40)
            print(" SIMCRICKETX DATABASE VIEWER")
            print("="*40)
            for key, (name, _) in MODELS.items():
                print(f"{key}. {name}")
            print("q. Quit")
            
            choice = input("\nSelect a table to view (1-8): ").strip().lower()
            
            if choice == 'q':
                break
            
            if choice in MODELS:
                name, model = MODELS[choice]
                view_table(name, model)
            else:
                print("Invalid selection.")

if __name__ == "__main__":
    main()
