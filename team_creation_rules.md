# Team Creation Module: Rules & Limitations

This document outlines the configured rules, constraints, and limitations for creating teams in SimCricketX, based on the current codebase (`app.py`, `engine/team.py`, `engine/player.py`).

## 1. Team Configuration

### Basic Information (Required)
- **Team Name**: Non-empty string.
- **Short Code**: Non-empty string (stored as uppercase).
- **Home Ground**: Non-empty string.
- **Team Color**: Hex color code (e.g., `#ffffff`).
- **Pitch Preference**: Must be one of the following:
  - Green
  - Flat
  - Dry
  - Hard
  - Dead

### Draft vs. Active Teams
Teams can be saved in two states: **Draft** or **Active** (Published).

#### Draft Rules:
- **Minimum Players**: 1
- **Leadership**: Selection of Captain and Wicketkeeper is **optional**.

#### Active (Published) Rules:
- **Player Count**: Must be between **12 and 25** players (inclusive).
- **Role Requirements**:
  - At least **1 Wicketkeeper**.
  - At least **6 Players** with role **Bowler** or **All-rounder**.
- **Leadership**: Must select a **Captain** and a **Wicketkeeper**.

---

## 2. Player Configuration

### Attributes & Constraints
Each player must have the following attributes defined:

| Attribute | Type | Constraints |
| :--- | :--- | :--- |
| **Name** | String | Required, non-empty. |
| **Role** | Selection | Must be one of: `Batsman`, `Bowler`, `All-rounder`, `Wicketkeeper`. |
| **Batting Hand** | Selection | `Left` or `Right`. |
| **Bowling Type** | Selection | Optional. Must be one of: `Fast`, `Fast-medium`, `Medium-fast`, `Medium`, `Off spin`, `Leg spin`, `Finger spin`, `Wrist spin`. <br> *Empty if player does not bowl.* |
| **Bowling Hand** | Selection | `Left` or `Right`. <br> *Required if Bowling Type is set. Must be empty if Bowling Type is empty.* |

### Skill Ratings
All skill ratings are integers from **0 to 100**.
- **Batting Rating**
- **Bowling Rating**
- **Fielding Rating**

---

## 3. Implementation Details

- **Storage**: Teams are stored as JSON files in `data/teams/<short_code>.json` or in the SQLite database (hybrid usage observed in code, primary source of truth appears to be DB for the web app).
- **Validation**:
  - Frontend validation exists but backend validation in `app.py` (`/team/create` route) is the final authority.
  - Creating a player with invalid enum values (e.g., invalid role) will raise a `ValueError`.
