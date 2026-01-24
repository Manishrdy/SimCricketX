import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.ball_outcome import calculate_outcome

def run_simulation(pitch_type, num_balls=1000, over_phase="Middle"):
    print(f"\n--- Simulation: Pitch={pitch_type}, Phase={over_phase} ---")
    
    batter = {
        "name": "Test Batter",
        "batting_rating": 85,
        "batting_hand": "Right"
    }
    
    bowler = {
        "name": "Test Bowler",
        "bowling_rating": 85,
        "fielding_rating": 80,
        "bowling_hand": "Right",
        "bowling_type": "Fast"
    }
    
    # Determine over number based on phase
    if over_phase == "Powerplay":
        over_range = range(0, 6)
    elif over_phase == "Middle":
        over_range = range(6, 16)
    else: # Death
        over_range = range(16, 20)
        
    stats = {
        "runs": 0,
        "wickets": 0,
        "fours": 0,
        "sixes": 0,
        "dots": 0
    }
    
    for _ in range(num_balls):
        over_num = list(over_range)[_ % len(over_range)]
        
        outcome = calculate_outcome(
            batter=batter,
            bowler=bowler,
            pitch=pitch_type,
            streak={}, # simplified
            over_number=over_num,
            batter_runs=25, # Assume set batter
            innings=1
        )
        
        if outcome["batter_out"]:
            stats["wickets"] += 1
        else:
            stats["runs"] += outcome["runs"]
            if outcome["runs"] == 0:
                stats["dots"] += 1
            elif outcome["runs"] == 4:
                stats["fours"] += 1
            elif outcome["runs"] == 6:
                stats["sixes"] += 1

    # Analysis
    avg_score_per_over = (stats['runs'] / num_balls) * 6
    avg_wickets_per_match = (stats['wickets'] / num_balls) * 120
    
    print(f"Total Balls: {num_balls}")
    print(f"Total Runs: {stats['runs']} (RR: {avg_score_per_over:.2f})")
    print(f"Total Wickets: {stats['wickets']} (Wickets/20ov: {avg_wickets_per_match:.2f})")
    print(f"Boundaries: 4s={stats['fours']}, 6s={stats['sixes']}")
    print(f"Dot Ball %: {stats['dots']/num_balls*100:.1f}%")

if __name__ == "__main__":
    # Test Hard Pitch (Should be Batting Dominant but balanced 80/20)
    run_simulation("Hard", num_balls=2000, over_phase="Middle")
    
    # Test Flat Pitch (Should be high scoring)
    run_simulation("Flat", num_balls=2000, over_phase="Middle")
    
    # Test Green Pitch (Should handle wickets)
    run_simulation("Green", num_balls=2000, over_phase="Middle")
    
    # Test Powerplay on Hard
    run_simulation("Hard", num_balls=2000, over_phase="Powerplay")
    
    # Test Death Overs on Hard
    run_simulation("Hard", num_balls=2000, over_phase="Death")
