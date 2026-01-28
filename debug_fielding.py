from database import db
from database.models import MatchScorecard, Player, Team
from app import create_app

app = create_app()

with app.app_context():
    # Check total records
    total = MatchScorecard.query.count()
    print(f"Total MatchScorecard records: {total}")
    
    # Check fielding data
    print("\n=== Sample Records ===")
    records = MatchScorecard.query.limit(10).all()
    for r in records:
        print(f"Type: {r.record_type}, Catches: {r.catches}, Run Outs: {r.run_outs}")
    
    # Check if ANY records have fielding data
    with_catches = MatchScorecard.query.filter(MatchScorecard.catches > 0).count()
    with_runouts = MatchScorecard.query.filter(MatchScorecard.run_outs > 0).count()
    
    print(f"\n=== Fielding Data Summary ===")
    print(f"Records with catches > 0: {with_catches}")
    print(f"Records with run_outs > 0: {with_runouts}")
    
    # Sample some records with fielding data
    if with_catches > 0:
        print("\n=== Records with Catches ===")
        catch_records = MatchScorecard.query.filter(MatchScorecard.catches > 0).limit(5).all()
        for r in catch_records:
            print(f"Player ID: {r.player_id}, Catches: {r.catches}, Type: {r.record_type}")
    
    if with_runouts > 0:
        print("\n=== Records with Run Outs ===")
        runout_records = MatchScorecard.query.filter(MatchScorecard.run_outs > 0).limit(5).all()
        for r in runout_records:
            print(f"Player ID: {r.player_id}, Run Outs: {r.run_outs}, Type: {r.record_type}")
