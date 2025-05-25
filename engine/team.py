# ── engine/team.py ──

import os
import json
from .player import Player

PITCH_PREFERENCES = ["Green", "Flat", "Dry", "Hard", "Dead"]

def save_team(team, base_path="data/teams"):
    os.makedirs(base_path, exist_ok=True)
    path = os.path.join(base_path, f"{team.short_code}.json")
    with open(path, "w") as f:
        json.dump(team.to_dict(), f, indent=2)

def load_team(short_code, base_path="data/teams"):
    path = os.path.join(base_path, f"{short_code}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Team file '{short_code}.json' does not exist.")
    with open(path, "r") as f:
        data = json.load(f)
    return Team.from_dict(data)

def list_teams(base_path="data/teams"):
    if not os.path.exists(base_path):
        return []
    return [f.replace(".json", "") for f in os.listdir(base_path) if f.endswith(".json")]

def delete_team(short_code, base_path="data/teams"):
    path = os.path.join(base_path, f"{short_code}.json")
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


class Team:
    def __init__(self, name, short_code, home_ground, pitch_preference,
                 team_color, players, captain, wicketkeeper):
        self.name = name
        self.short_code = short_code
        self.home_ground = home_ground
        self.pitch_preference = pitch_preference
        self.team_color = team_color          # New!
        self.players = players
        self.captain = captain
        self.wicketkeeper = wicketkeeper

    def to_dict(self):
        return {
            "team_name": self.name,
            "short_code": self.short_code,
            "home_ground": self.home_ground,
            "pitch_preference": self.pitch_preference,
            "team_color": self.team_color,        # New!
            "players": [p.to_dict() for p in self.players],
            "captain": self.captain,
            "wicketkeeper": self.wicketkeeper
        }

    @staticmethod
    def from_dict(data):
        players = [Player.from_dict(p) for p in data["players"]]
        return Team(
            name=data["team_name"],
            short_code=data["short_code"],
            home_ground=data["home_ground"],
            pitch_preference=data["pitch_preference"],
            team_color=data.get("team_color", "#ffffff"),  # default white
            players=players,
            captain=data["captain"],
            wicketkeeper=data["wicketkeeper"]
        )

# (test stub unchanged)...

# if __name__ == "__main__":
#     from engine.player import Player

#     # create a dummy team
#     p1 = Player("Rohit Sharma", "Batsman", 90, 15, 80, "Right", "", "")
#     team = Team("Mumbai Indians", "MI", "Wankhede", "Flat", [p1], "Rohit Sharma", "Ishan Kishan")

#     save_team(team)
#     print("Saved!")

#     loaded = load_team("MI")
#     print("Loaded:", loaded.to_dict())

#     print("All teams:", list_teams())
