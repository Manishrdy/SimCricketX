import random

# Outcome probabilities (editable scoring matrix)
SCORING_MATRIX = {
    "Dot": 0.35,      # 35 balls - Lower dot percentage due to aggressive batting
    "Single": 0.30,   # 30 balls - Still most common but reduced
    "Double": 0.14,   # 14 balls - More doubles due to aggressive running
    "Three": 0.06,    # 6 balls - More adventurous running
    "Four": 0.08,     # 8 balls - Higher boundary rate
    "Six": 0.04,      # 4 balls - Much higher six rate in T20
    "Wicket": 0.022,  # 2.2 balls - Slightly lower as batsmen take more risks
    "Extras": 0.048   # 4.8 balls - More extras due to pressure bowling
}

# Helper function: adjust probability based on ratings
def adjust_for_rating(base_prob, batting, bowling, pitch_factor):
    prob = base_prob * (batting / (batting + bowling))
    prob *= pitch_factor
    return prob

# Pitch modifiers
PITCH_FACTORS = {
    "Green": {
        "Fast": 1.2, "Fast-medium": 1.15, "Medium-fast": 1.1,
        "default": 0.9
    },
    "Flat": {"default": 1.2},
    "Dry": {
        "Off spin": 1.2, "Leg spin": 1.2, "Finger spin": 1.15, "Wrist spin": 1.15,
        "default": 0.95
    },
    "Hard": {"default": 1.0},
    "Dead": {"default": 1.25}
}

# Calculate pitch factor
def pitch_factor(pitch, bowling_type):
    return PITCH_FACTORS.get(pitch, {}).get(bowling_type, PITCH_FACTORS[pitch]["default"])

# Main outcome function
def calculate_outcome(batter, bowler, pitch, streak, over_number, batter_runs):
    batting = batter["batting_rating"]
    bowling = bowler["bowling_rating"]
    fielding = bowler["fielding_rating"]
    batting_hand = batter["batting_hand"]
    bowling_hand = bowler["bowling_hand"]
    bowling_type = bowler["bowling_type"]

    outcomes = list(SCORING_MATRIX.keys())
    weights = []

    # Adjust weights based on ratings, pitch, and hand matchups
    for outcome in outcomes:
        base = SCORING_MATRIX[outcome]

        if outcome in ["Four", "Six"]:
            prob = adjust_for_rating(base, batting, bowling, pitch_factor(pitch, bowling_type))
            # Higher streak increases wicket chance slightly
            if streak.get("boundaries", 0) >= 2:
                prob *= 0.8
        elif outcome == "Wicket":
            prob = base * (bowling / (batting + bowling)) * (fielding / 100)
            # Increase wicket chance on high streak
            if streak.get("boundaries", 0) >= 2:
                prob *= 1.5
            if pitch == "Green" and bowling_hand == "Left" and batting_hand == "Right":
                prob *= 1.2
        elif outcome == "Extras":
            prob = base * (100 - bowling) / 100
        else:  # Dot, Single, Double, Three
            prob = adjust_for_rating(base, batting, bowling, pitch_factor(pitch, bowling_type))

        weights.append(prob)

    total_weight = sum(weights)
    normalized_weights = [w / total_weight for w in weights]

    outcome_chosen = random.choices(outcomes, normalized_weights)[0]

    # Build result
    result = {
        "type": "run",
        "runs": 0,
        "description": "",
        "wicket_type": None,
        "is_extra": False,
        "batter_out": False
    }

    # Outcome details
    commentary_templates = {
        "Dot": ["Good length, no run.", "Well defended."],
        "Single": ["Tapped away for a quick single.", "Pushes gently for one."],
        "Double": ["Driven into the gap for two.", "Quick running, two runs."],
        "Three": ["Excellently placed, three runs taken!"],
        "Four": ["Beautifully struck boundary!", "Cracking shot for four!"],
        "Six": ["That's a huge six!", "Launched into the stands!"],
        "Wicket": ["He's out! Brilliant delivery!", "Gone! A crucial wicket falls!"],
        "Extras": ["Wide delivery, extras added.", "No-ball called by umpire."]
    }

    if outcome_chosen == "Wicket":
        wicket_types = ["Caught", "Bowled", "LBW", "Run Out"]
        wicket = random.choices(wicket_types, [0.4, 0.3, 0.2, 0.1])[0]
        result.update({
            "type": "wicket",
            "runs": 0,
            "wicket_type": wicket,
            "batter_out": True,
            "description": random.choice(commentary_templates["Wicket"])
        })
    elif outcome_chosen == "Extras":
        extra_types = ["Wide", "No Ball", "Leg Bye", "Byes"]
        extra = random.choice(extra_types)
        result.update({
            "type": "extra",
            "runs": 1,
            "is_extra": True,
            "description": random.choice(commentary_templates["Extras"]) + f" ({extra})"
        })
    else:
        runs_scored = {
            "Dot": 0, "Single": 1, "Double": 2,
            "Three": 3, "Four": 4, "Six": 6
        }[outcome_chosen]
        result.update({
            "type": "run",
            "runs": runs_scored,
            "description": random.choice(commentary_templates[outcome_chosen])
        })

    return result