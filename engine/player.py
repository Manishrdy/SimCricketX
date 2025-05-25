# engine/player.py

BOWLING_TYPES = [t.lower() for t in [
  "Fast","Fast-medium","Medium-fast","Medium",
  "Off spin","Leg spin","Finger spin","Wrist spin"
]]

BOWLING_HANDS = ["Left", "Right"]
BATTING_HANDS = ["Left", "Right"]
PLAYER_ROLES = ["Batsman", "Bowler", "All-rounder", "Wicketkeeper"]

class Player:
    def __init__(self, name, role, batting_rating, bowling_rating, fielding_rating,
                 batting_hand, bowling_type, bowling_hand):
        self.name = name.strip()

        if role not in PLAYER_ROLES:
            raise ValueError(f"Invalid role: {role}")
        self.role = role

        for rating, label in zip(
            [batting_rating, bowling_rating, fielding_rating],
            ["batting_rating", "bowling_rating", "fielding_rating"]
        ):
            if not (0 <= rating <= 100):
                raise ValueError(f"{label} must be between 0 and 100")

        self.batting_rating = batting_rating
        self.bowling_rating = bowling_rating
        self.fielding_rating = fielding_rating

        if batting_hand not in BATTING_HANDS:
            raise ValueError(f"Invalid batting hand: {batting_hand}")
        self.batting_hand = batting_hand

        if bowling_type and bowling_type.lower() not in BOWLING_TYPES:
            raise ValueError(f"Invalid bowling type: {bowling_type}")
        self.bowling_type = bowling_type

        if bowling_hand and bowling_hand not in BOWLING_HANDS:
            raise ValueError(f"Invalid bowling hand: {bowling_hand}")
        self.bowling_hand = bowling_hand

    def to_dict(self):
        return self.__dict__

    @staticmethod
    def from_dict(data):
        return Player(**data)
