import random
from engine.ball_outcome import pitch_factor

# Super Over outcome probabilities (more exciting)
SUPER_OVER_SCORING_MATRIX = {
    "Dot": 0.25,      # Reduced from 35%
    "Single": 0.28,   # Slightly reduced
    "Double": 0.12,   # Slightly reduced  
    "Three": 0.05,    # Slightly reduced
    "Four": 0.15,     # Increased from 8%
    "Six": 0.08,      # Doubled from 4%
    "Wicket": 0.035,  # Slightly increased
    "Extras": 0.045   # Slightly reduced
}

def calculate_super_over_outcome(batter, bowler, pitch, streak, over_number, batter_runs):
    batting = batter["batting_rating"]
    bowling = bowler["bowling_rating"]
    fielding = bowler["fielding_rating"]
    batting_hand = batter["batting_hand"]
    bowling_hand = bowler["bowling_hand"]
    bowling_type = bowler["bowling_type"]

    outcomes = list(SUPER_OVER_SCORING_MATRIX.keys())
    weights = []

    for outcome in outcomes:
        base = SUPER_OVER_SCORING_MATRIX[outcome]

        if outcome in ["Four", "Six"]:
            prob = base * (batting / (batting + bowling)) * pitch_factor(pitch, bowling_type)
            prob *= 1.2  # Super over excitement bonus
            if streak.get("boundaries", 0) >= 3:
                prob *= 0.9
        elif outcome == "Wicket":
            prob = base * (bowling / (batting + bowling)) * (fielding / 100)
            prob *= 1.3  # Super over pressure
            if streak.get("boundaries", 0) >= 2:
                prob *= 1.4
        elif outcome == "Extras":
            prob = base * (100 - bowling) / 100 * 1.2  # More pressure = more extras
        else:
            prob = base * (batting / (batting + bowling)) * pitch_factor(pitch, bowling_type)

        weights.append(prob)

    total_weight = sum(weights)
    normalized_weights = [w / total_weight for w in weights]
    outcome_chosen = random.choices(outcomes, normalized_weights)[0]

    result = {
        "type": "run", "runs": 0, "description": "", "wicket_type": None,
        "is_extra": False, "batter_out": False
    }

    commentary_templates = {
        "Dot": ["Pressure delivery! No run.", "Dot ball under pressure."],
        "Single": ["Quick single under pressure.", "Rotates strike in super over."],
        "Double": ["Pushed into the gap for two!", "Great running, two runs."],
        "Three": ["Excellent placement for three!", "Superb running between wickets!"],
        "Four": ["BOUNDARY! Crucial four in super over!", "What a shot under pressure! FOUR!"],
        "Six": ["MASSIVE SIX! Gone into the stands!", "HUGE hit! Six runs in super over!"],
        "Wicket": ["WICKET! Pressure gets to batsman!", "OUT! Crucial breakthrough!"],
        "Extras": ["Extra runs under pressure.", "Pressure gets to bowler - extras."]
    }

    if outcome_chosen == "Wicket":
        wicket_types = ["Caught", "Bowled", "LBW", "Run Out"]
        wicket = random.choices(wicket_types, [0.5, 0.3, 0.15, 0.05])[0]
        result.update({
            "type": "wicket", "runs": 0, "wicket_type": wicket, "batter_out": True,
            "description": random.choice(commentary_templates["Wicket"])
        })
    elif outcome_chosen == "Extras":
        extra_types = ["Wide", "No Ball", "Leg Bye", "Byes"]
        extra = random.choice(extra_types)
        result.update({
            "type": "extra", "runs": 1, "is_extra": True,
            "description": random.choice(commentary_templates["Extras"]) + f" ({extra})"
        })
    else:
        runs_scored = {"Dot": 0, "Single": 1, "Double": 2, "Three": 3, "Four": 4, "Six": 6}[outcome_chosen]
        result.update({
            "type": "run", "runs": runs_scored,
            "description": random.choice(commentary_templates[outcome_chosen])
        })

    return result