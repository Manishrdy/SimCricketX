"""
player.py

Defines the Player class, representing a cricketer with batting, bowling,
and fielding ratings, plus attributes for handedness and bowling style.

All ratings must be integers between 0 and 100. Batting and bowling hands must
be "Left" or "Right". Bowling type must be one of the allowed BOWLING_TYPES or
an empty string if the player does not bowl. This module provides methods to
serialize to/from dictionaries for use in match simulation.

We also expose PLAYER_ROLES, BATTING_HANDS, BOWLING_HANDS, and BOWLING_TYPES
so that app.py (and its forms/templates) can import them directly.
"""

from typing import Dict, Any, ClassVar, List

# -----------------------------------------------------------------------------
# 0) Constants needed by app.py
# -----------------------------------------------------------------------------

PLAYER_ROLES: ClassVar[List[str]] = [
    "Batsman",
    "Bowler",
    "All-rounder",
    "Wicketkeeper"
]

BATTING_HANDS: ClassVar[List[str]] = [
    "Left",
    "Right"
]

BOWLING_HANDS: ClassVar[List[str]] = [
    "Left",
    "Right"
]

BOWLING_TYPES: ClassVar[List[str]] = [
    "Fast",
    "Fast-medium",
    "Medium-fast",
    "Medium",
    "Off spin",
    "Leg spin",
    "Finger spin",
    "Wrist spin"
]

# -----------------------------------------------------------------------------
# 1) Player class definition
# -----------------------------------------------------------------------------

class Player:
    """
    Represents a single cricket player with batting, bowling, and fielding ratings,
    plus handedness and bowling style attributes.

    Attributes:
        name (str): Full name of the player.
        role (str): Primary role, e.g., "Batsman", "Bowler", "Allrounder", "Wicketkeeper".
        batting_rating (int): 0–100 rating for batting skill.
        bowling_rating (int): 0–100 rating for bowling skill.
        fielding_rating (int): 0–100 rating for fielding skill.
        batting_hand (str): Either "Left" or "Right".
        bowling_type (str): One of the BOWLING_TYPES, or empty if the player does not bowl.
        bowling_hand (str): Either "Left" or "Right". Must be empty string if bowling_type is empty.
    """

    def __init__(
        self,
        name: str,
        role: str,
        batting_rating: int,
        bowling_rating: int,
        fielding_rating: int,
        batting_hand: str,
        bowling_type: str = "",
        bowling_hand: str = ""
    ) -> None:
        # 1a) Name and role
        self.name = name.strip()
        self.role = role.strip()
        if self.role not in PLAYER_ROLES:
            raise ValueError(f"role must be one of {PLAYER_ROLES}")

        # 1b) Ratings validation
        for rating_value, label in zip(
            (batting_rating, bowling_rating, fielding_rating),
            ("batting_rating", "bowling_rating", "fielding_rating")
        ):
            if not isinstance(rating_value, int):
                raise TypeError(f"{label} must be an integer between 0 and 100.")
            if not (0 <= rating_value <= 100):
                raise ValueError(f"{label} must be between 0 and 100.")
        self.batting_rating = batting_rating
        self.bowling_rating = bowling_rating
        self.fielding_rating = fielding_rating

        # 1c) Batting hand validation
        batting_hand_clean = batting_hand.strip().title()
        if batting_hand_clean not in BATTING_HANDS:
            raise ValueError(f"batting_hand must be one of {BATTING_HANDS}.")
        self.batting_hand = batting_hand_clean

        # 1d) Bowling type & hand validation
        bowling_type_clean = bowling_type.strip()
        if bowling_type_clean:
            if bowling_type_clean not in BOWLING_TYPES:
                raise ValueError(f"bowling_type must be one of {BOWLING_TYPES} or empty string.")
            # If player bowls, bowling_hand must be valid
            bowling_hand_clean = bowling_hand.strip().title()
            if bowling_hand_clean not in BOWLING_HANDS:
                raise ValueError(f"bowling_hand must be one of {BOWLING_HANDS} for a bowler.")
            self.bowling_type = bowling_type_clean
            self.bowling_hand = bowling_hand_clean
        else:
            # If no bowling_type, enforce bowling_hand empty
            if bowling_hand.strip():
                raise ValueError("bowling_hand must be empty if bowling_type is empty.")
            self.bowling_type = ""
            self.bowling_hand = ""

    def to_dict(self) -> Dict[str, Any]:
        """
        Serializes this Player to a dictionary for JSON transport or storage.
        """
        return {
            "name": self.name,
            "role": self.role,
            "batting_rating": self.batting_rating,
            "bowling_rating": self.bowling_rating,
            "fielding_rating": self.fielding_rating,
            "batting_hand": self.batting_hand,
            "bowling_type": self.bowling_type,
            "bowling_hand": self.bowling_hand,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Player":
        """
        Constructs a Player instance from a dictionary. Expects all keys:
            - name
            - role
            - batting_rating
            - bowling_rating
            - fielding_rating
            - batting_hand
            - bowling_type
            - bowling_hand
        """
        required_keys = {
            "name",
            "role",
            "batting_rating",
            "bowling_rating",
            "fielding_rating",
            "batting_hand",
            "bowling_type",
            "bowling_hand",
        }
        missing = required_keys - set(data.keys())
        if missing:
            raise KeyError(f"Missing keys for Player.from_dict: {missing}")

        return cls(
            name=data["name"],
            role=data["role"],
            batting_rating=int(data["batting_rating"]),
            bowling_rating=int(data["bowling_rating"]),
            fielding_rating=int(data["fielding_rating"]),
            batting_hand=data["batting_hand"],
            bowling_type=data["bowling_type"],
            bowling_hand=data["bowling_hand"],
        )

    def __repr__(self) -> str:
        return (
            f"Player(name={self.name!r}, role={self.role!r}, "
            f"batting_rating={self.batting_rating}, bowling_rating={self.bowling_rating}, "
            f"fielding_rating={self.fielding_rating}, batting_hand={self.batting_hand!r}, "
            f"bowling_type={self.bowling_type!r}, bowling_hand={self.bowling_hand!r})"
        )
