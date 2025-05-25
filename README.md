
# 📘 System Design Document (SDD) for IPL Cricket Simulator

## 1. Overview

### 1.1 Project Name
IPL Cricket Simulator

### 1.2 Objective
Simulate realistic IPL-style T20 cricket matches (20 overs) with player ratings, pitch conditions, rain interruptions, Duckworth-Lewis adjustments, detailed commentary, and dramatic match scenarios.

### 1.3 Target Users
PlanetCricket forum community users.

### 1.4 Technology Stack
- **Backend**: Python + Flask
- **Frontend**: HTML, CSS (Bootstrap), JavaScript
- **Storage**: JSON (local), Google Sheets (remote auth)
- **Deployment**: Docker, PyInstaller

## 2. Application Architecture

### 2.1 Folder Structure
```
/ipl_simulator
├── app.py
├── templates/
│   ├── home.html
│   ├── team.html
│   ├── match.html
├── static/
│   ├── css/
│   ├── js/
│   ├── assets/
├── data/
│   ├── teams/
│   ├── matches/
│   ├── commentary_bank.json
│   └── ratings_config.json
├── logs/
│   ├── execution.log
│   └── matches/
├── auth/
│   ├── credentials.json
│   └── user_auth.py
```

## 3. Class and Module Design

### 3.1 Player Class
- **Attributes**: name, role, batting_rating, bowling_rating, fielding_rating, batting_hand, bowling_type, bowling_hand

### 3.2 Team Class
- **Attributes**: name, short_code, home_ground, pitch_preference, players[], captain, wicketkeeper

### 3.3 Match Class
- **Attributes**: match_id, teams[], innings[], toss_winner, result
- **Methods**: simulate_toss(), simulate_innings(), apply_rain(), calculate_dls(), generate_scorecards()

### 3.4 BallOutcome Engine
- **Methods**: calculate_outcome(batsman, bowler, pitch_type)

### 3.5 Rain & DLS Module
- **Methods**: trigger_rain(), adjust_target(), handle_interrupt()

### 3.6 Commentary Engine
- **Methods**: generate_commentary(outcome, placeholders)

## 4. Simulation Flow

1. Load Teams
2. User Authentication (Google Sheets)
3. Select Playing XI (drag-and-drop UI)
4. Choose Pitch
5. Match Setup
6. Toss Simulation
7. Innings Simulation
8. Rain/DLS Handling (manual trigger)
9. Ball-by-Ball Simulation
10. Commentary and Logging
11. Generate and Store Match Statistics
12. Match Completion

## 5. Data Management

### 5.1 Local Storage
- JSON files for teams, match stats
- Match logs in `.txt` format

### 5.2 Remote Storage
- User authentication data via Google Sheets

## 6. Logging and Error Handling

### 6.1 Execution Logging
- Logs stored in `execution.log`

### 6.2 Error Handling
- Create unique match folder
- Store traceback in `execution.log`

## 7. UI/UX Design

### 7.1 Frontend Technology
- HTML5, Bootstrap, minimal JavaScript

### 7.2 UI Layout
- **Home**: Login/Register
- **Team Creation**: Drag-and-drop selection
- **Match Simulation**: Split-screen UI
  - Left: Match controls, player/team data
  - Right: Ball-by-ball commentary ticker

### 7.3 Theme
- Dark/Light toggle (Dracula-like theme)
- Toggle at top of UI

## 8. Deployment & Packaging

### 8.1 Docker Container
- Environment consistency

### 8.2 PyInstaller
- Standalone executable
- Launch script (`.bat`/`.sh`) opens Flask server in browser

## 9. Commentary & Match Display

### 9.1 Commentary Storage
- `commentary_bank.json` with placeholders

### 9.2 Output
- Real-time ticker (right panel)
- Match log (`match_log.txt`)

## 10. Match Archiving & Output

- Unique folder per match
```
match_<id>_<team1>_vs_<team2>_<timestamp>/
├── <Team1>_players.json
├── <Team2>_players.json
├── match_log.txt
├── execution.log
├── <Team1>_batsman_stats.json
├── <Team1>_bowlers_stats.json
├── <Team2>_batsman_stats.json
├── <Team2>_bowlers_stats.json
```

## 11. Game Rules & Recommendations

- Max 4 overs per bowler
- No consecutive overs
- Batting order: auto by rating, user can reorder pre-match
- Bowling rotation: System-controlled, automated logic
