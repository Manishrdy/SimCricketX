import os
import json
import csv
import shutil
from datetime import datetime
from tabulate import tabulate
from typing import Dict, List, Any

class MatchArchiver:
    def __init__(self, match_data: Dict[str, Any], match_instance):
        self.match_data = match_data
        self.match = match_instance
        self.match_id = match_data.get('match_id')
        self.username = match_data.get('created_by')
        self.timestamp = match_data.get('timestamp')
        
        # Extract team names
        self.team_home = match_data.get('team_home', '').split('_')[0]
        self.team_away = match_data.get('team_away', '').split('_')[0]
        
        # Generate folder and file names
        self.folder_name = f"playing_{self.team_home}_vs_{self.team_away}_{self.username}_{self.timestamp}"
        self.archive_path = os.path.join("data", self.folder_name)
        
        # File paths
        self.json_filename = f"playing_{self.team_home}_vs_{self.team_away}_{self.username}_{self.timestamp}.json"
        self.txt_filename = f"playing_{self.team_home}_vs_{self.team_away}_{self.username}_{self.timestamp}.txt"

    def create_archive(self, original_json_path: str, commentary_log: List[str]):
        """Create complete match archive with all files"""
        try:
            # Create archive directory
            os.makedirs(self.archive_path, exist_ok=True)
            print(f"📁 Created archive directory: {self.archive_path}")
            
            # 1. Copy original JSON file (keep original intact)
            self._copy_json_file(original_json_path)
            
            # 2. Create comprehensive text file
            self._create_commentary_text_file(commentary_log)
            
            # 3. Create all CSV files
            self._create_all_csv_files()
            
            # NOTE: HTML report is now handled by frontend "Save Report" button
            
            print(f"✅ Match archive created successfully: {self.folder_name}")
            return True
            
        except Exception as e:
            print(f"❌ Error creating match archive: {e}")
            return False

    def _copy_json_file(self, original_path: str):
        """Copy original JSON file to archive folder (preserving original)"""
        if os.path.exists(original_path):
            destination = os.path.join(self.archive_path, self.json_filename)
            shutil.copy2(original_path, destination)  # copy2 preserves metadata
            print(f"📄 Copied JSON file to archive: {self.json_filename}")
        else:
            print(f"⚠️ Original JSON file not found: {original_path}")

    def _create_commentary_text_file(self, commentary_log: List[str]):
        """Create comprehensive text file with playing XI, commentary, and scorecards"""
        txt_path = os.path.join(self.archive_path, self.txt_filename)
        
        with open(txt_path, 'w', encoding='utf-8') as f:
            # Header
            f.write("=" * 80 + "\n")
            f.write(f"CRICKET MATCH RECORD\n")
            f.write(f"{self.team_home} vs {self.team_away}\n")
            f.write(f"Match ID: {self.match_id}\n")
            f.write(f"Date: {self.timestamp}\n")
            f.write(f"Stadium: {self.match_data.get('stadium', 'N/A')}\n")
            f.write(f"Pitch: {self.match_data.get('pitch', 'N/A')}\n")
            f.write("=" * 80 + "\n\n")
            
            # Playing XI
            f.write(self._format_playing_xi())
            
            # Live Commentary
            f.write("\nLIVE COMMENTARY\n")
            f.write("=" * 50 + "\n")
            for comment in commentary_log:
                # Remove HTML tags for clean text
                clean_comment = self._clean_html(comment)
                f.write(f"{clean_comment}\n")
            
            # Scorecards
            f.write(self._format_scorecards())
        
        print(f"📝 Created commentary text file: {self.txt_filename}")

    def _format_playing_xi(self) -> str:
        """Format playing XI for both teams"""
        output = []
        
        # Team 1 Playing XI
        output.append(f"TEAM 1 - {self.team_home} PLAYING XI:")
        output.append("-" * 30)
        for i, player in enumerate(self.match_data['playing_xi']['home'], 1):
            bowling_info = " (Bowling)" if player.get('will_bowl', False) else ""
            output.append(f"{i:2}. {player['name']} ({player['role']}){bowling_info}")
        
        output.append("")
        
        # Team 2 Playing XI
        output.append(f"TEAM 2 - {self.team_away} PLAYING XI:")
        output.append("-" * 30)
        for i, player in enumerate(self.match_data['playing_xi']['away'], 1):
            bowling_info = " (Bowling)" if player.get('will_bowl', False) else ""
            output.append(f"{i:2}. {player['name']} ({player['role']}){bowling_info}")
        
        output.append("\n")
        return "\n".join(output)

    def _format_scorecards(self) -> str:
        """Format scorecards in tabular format using tabulate"""
        output = []
        output.append("\n" + "=" * 80)
        output.append("MATCH SCORECARDS")
        output.append("=" * 80)
        
        # Get team names
        team_home = self.match_data["team_home"].split("_")[0]
        team_away = self.match_data["team_away"].split("_")[0]
        
        # Determine team order based on who batted first
        if hasattr(self.match, 'first_batting_team_name') and self.match.first_batting_team_name:
            first_batting_team = self.match.first_batting_team_name
            first_bowling_team = self.match.first_bowling_team_name
            second_batting_team = first_bowling_team
            second_bowling_team = first_batting_team
        else:
            # Fallback
            first_batting_team = team_home
            first_bowling_team = team_away
            second_batting_team = team_away  
            second_bowling_team = team_home
        
        # 1st Innings
        if hasattr(self.match, 'first_innings_batting_stats') and self.match.first_innings_batting_stats:
            output.append(f"\n1ST INNINGS - {first_batting_team} BATTING")
            output.append("-" * 50)
            output.append(self._create_batting_table(self.match.first_innings_batting_stats))
            
            output.append(f"\n1ST INNINGS - {first_bowling_team} BOWLING")
            output.append("-" * 50)
            output.append(self._create_bowling_table(self.match.first_innings_bowling_stats))
        
        # 2nd Innings
        if hasattr(self.match, 'second_innings_batting_stats') and self.match.second_innings_batting_stats:
            output.append(f"\n2ND INNINGS - {second_batting_team} BATTING")
            output.append("-" * 50)
            output.append(self._create_batting_table(self.match.second_innings_batting_stats))
            
            output.append(f"\n2ND INNINGS - {second_bowling_team} BOWLING")
            output.append("-" * 50)
            output.append(self._create_bowling_table(self.match.second_innings_bowling_stats))
        elif hasattr(self.match, 'batsman_stats') and self.match.innings >= 2:
            # Fallback: use current stats if second innings stats not saved
            output.append(f"\n2ND INNINGS - {second_batting_team} BATTING")
            output.append("-" * 50)
            output.append(self._create_batting_table(self.match.batsman_stats))
            
            output.append(f"\n2ND INNINGS - {second_bowling_team} BOWLING")
            output.append("-" * 50)
            output.append(self._create_bowling_table(self.match.bowler_stats))
        
        # Match Result
        if hasattr(self.match, 'result') and self.match.result:
            output.append(f"\nMATCH RESULT: {self.match.result}")
        
        return "\n".join(output)

    def _create_batting_table(self, batting_stats: Dict) -> str:
        """Create batting scorecard table"""
        headers = ['Player', 'Runs', 'Balls', '1s', '2s', '3s', '4s', '6s', 'Dots', 'S/R', 'Status']
        rows = []
        
        # Determine batting order - try to get it from match instance
        batting_order = []
        if hasattr(self.match, 'home_xi') and hasattr(self.match, 'away_xi'):
            # Try to determine which team this scorecard is for
            # This is a bit complex since we need to figure out which team's stats these are
            # For now, include all players from stats, then add missing ones
            stats_players = list(batting_stats.keys())
            
            # Add players from both teams to be safe, we'll filter out the wrong team's players
            all_possible_players = []
            for player in self.match.home_xi + self.match.away_xi:
                if player['name'] in batting_stats or len([p for p in stats_players if p == player['name']]) > 0:
                    all_possible_players.append(player)
            
            # If we can't determine, just use the order from stats
            if all_possible_players:
                batting_order = all_possible_players
            else:
                batting_order = [{'name': name} for name in batting_stats.keys()]
        else:
            batting_order = [{'name': name} for name in batting_stats.keys()]
        
        # Add all players, including those who didn't bat
        for player in batting_order:
            player_name = player['name']
            
            if player_name in batting_stats:
                stats = batting_stats[player_name]
                # Only include players who actually have some activity (batted or got out)
                if stats['balls'] > 0 or stats['wicket_type']:
                    sr = f"{(stats['runs'] * 100 / stats['balls']):.1f}" if stats['balls'] > 0 else "0.0"
                    status = stats['wicket_type'] if stats['wicket_type'] else "not out"
                    
                    rows.append([
                        player_name,
                        stats['runs'],
                        stats['balls'],
                        stats.get('ones', 0),      # ✅ Added 1s
                        stats.get('twos', 0),      # ✅ Added 2s
                        stats.get('threes', 0),    # ✅ Added 3s
                        stats['fours'],
                        stats['sixes'],
                        stats.get('dots', 0),      # ✅ Added dots
                        sr,
                        status
                    ])
                else:
                    # Player in stats but didn't bat
                    rows.append([
                        player_name,
                        "-",
                        "-", 
                        "-",
                        "-",
                        "-",
                        "-",
                        "-",
                        "-",
                        "-",
                        "did not bat"
                    ])
        
        # If no rows (empty stats), add a placeholder
        if not rows:
            rows.append(["No batting data available", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-"])
        
        return tabulate(rows, headers=headers, tablefmt="grid")

    def _create_bowling_table(self, bowling_stats: Dict) -> str:
        """Create bowling scorecard table"""
        headers = ['Bowler', 'Overs', 'Maidens', 'Runs', 'Wickets', 'Economy', 'Wides', 'No Balls']
        rows = []
        
        for bowler_name, stats in bowling_stats.items():
            if stats['balls_bowled'] > 0:  # Only include bowlers who bowled
                total_balls = stats['overs'] * 6 + (stats['balls_bowled'] % 6)
                overs_display = f"{stats['overs']}.{stats['balls_bowled'] % 6}" if stats['balls_bowled'] % 6 > 0 else str(stats['overs'])
                economy = f"{(stats['runs'] * 6 / total_balls):.2f}" if total_balls > 0 else "0.00"
                
                rows.append([
                    bowler_name,
                    overs_display,
                    stats['maidens'],
                    stats['runs'],
                    stats['wickets'],
                    economy,
                    stats['wides'],
                    stats['noballs']
                ])
        
        return tabulate(rows, headers=headers, tablefmt="grid")

    def _create_all_csv_files(self):
        """Create all 4 CSV files for batting and bowling stats"""
        # Get team names for proper CSV naming
        team_home = self.match_data["team_home"].split("_")[0] 
        team_away = self.match_data["team_away"].split("_")[0]
        
        # Determine which team batted first
        if hasattr(self.match, 'first_batting_team_name') and self.match.first_batting_team_name:
            first_batting_team = self.match.first_batting_team_name
            first_bowling_team = self.match.first_bowling_team_name
            second_batting_team = first_bowling_team  # Second batting team is opposite of first
            second_bowling_team = first_batting_team  # Second bowling team is opposite of first
        else:
            # Fallback: assume home team batted first
            first_batting_team = team_home
            first_bowling_team = team_away  
            second_batting_team = team_away
            second_bowling_team = team_home
        
        print(f"📊 Creating CSV files:")
        print(f"   1st Innings: {first_batting_team} batting, {first_bowling_team} bowling")
        print(f"   2nd Innings: {second_batting_team} batting, {second_bowling_team} bowling")
        
        # Create all 4 CSV files with correct team names
        # First innings
        self._create_batting_csv(
            f"{self.match_id}_{self.username}_{first_batting_team}_batting.csv",
            getattr(self.match, 'first_innings_batting_stats', {})
        )
        self._create_bowling_csv(
            f"{self.match_id}_{self.username}_{first_bowling_team}_bowling.csv", 
            getattr(self.match, 'first_innings_bowling_stats', {})
        )
        
        # Second innings  
        self._create_batting_csv(
            f"{self.match_id}_{self.username}_{second_batting_team}_batting.csv",
            getattr(self.match, 'second_innings_batting_stats', {})
        )
        self._create_bowling_csv(
            f"{self.match_id}_{self.username}_{second_bowling_team}_bowling.csv",
            getattr(self.match, 'second_innings_bowling_stats', {})
        )

    def _create_batting_csv(self, filename: str, stats: Dict):
        """Create batting statistics CSV file"""
        csv_path = os.path.join(self.archive_path, filename)
        
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            
            # Headers - Include 1s, 2s, 3s columns
            headers = ['Player Name', 'Runs', 'Balls', '1s', '2s', '3s', 'Fours', 'Sixes', 'Dots', 'Strike Rate', 'Status', 'Bowler Out', 'Fielder Out']
            writer.writerow(headers)
            
            # Determine which team's batting order to use
            if hasattr(self.match, 'home_xi') and hasattr(self.match, 'away_xi'):
                # Get the correct batting team for this innings
                if filename.endswith(f"{self.team_home}_batting.csv"):
                    batting_order = self.match.home_xi
                else:
                    batting_order = self.match.away_xi
            else:
                # Fallback: use stats keys
                batting_order = [{'name': name} for name in stats.keys()]
            
            # Include ALL players from batting team, not just those who batted
            for player in batting_order:
                player_name = player['name']
                
                if player_name in stats:
                    player_stats = stats[player_name]
                    # Player has stats (batted or got out)
                    strike_rate = f"{(player_stats['runs'] * 100 / player_stats['balls']):.1f}" if player_stats['balls'] > 0 else "0.0"
                    status = player_stats.get('wicket_type', '') if player_stats.get('wicket_type') else "not out"
                    
                    writer.writerow([
                        player_name,
                        player_stats.get('runs', 0),
                        player_stats.get('balls', 0),
                        player_stats.get('ones', 0),      # ✅ Added 1s
                        player_stats.get('twos', 0),      # ✅ Added 2s  
                        player_stats.get('threes', 0),    # ✅ Added 3s
                        player_stats.get('fours', 0),
                        player_stats.get('sixes', 0),
                        player_stats.get('dots', 0),      # ✅ Added dots
                        strike_rate,
                        status,
                        player_stats.get('bowler_out', ''),
                        player_stats.get('fielder_out', '')
                    ])
                else:
                    # Player didn't bat - add with empty values
                    writer.writerow([
                        player_name,
                        "",  # Runs
                        "",  # Balls
                        "",  # 1s
                        "",  # 2s
                        "",  # 3s
                        "",  # Fours
                        "",  # Sixes
                        "",  # Dots
                        "",  # Strike Rate
                        "did not bat",  # Status
                        "",  # Bowler Out
                        ""   # Fielder Out
                    ])
        
        print(f"📊 Created batting CSV: {filename}")

    def _create_bowling_csv(self, filename: str, stats: Dict):
        """Create bowling statistics CSV file"""
        csv_path = os.path.join(self.archive_path, filename)
        
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            
            # Headers
            headers = ['Bowler Name', 'Overs', 'Maidens', 'Runs', 'Wickets', 'Economy', 'Wides', 'No Balls']
            writer.writerow(headers)
            
            # Data rows
            for bowler_name, bowler_stats in stats.items():
                if bowler_stats.get('balls_bowled', 0) > 0:
                    total_balls = bowler_stats['overs'] * 6 + (bowler_stats['balls_bowled'] % 6)
                    overs_display = f"{bowler_stats['overs']}.{bowler_stats['balls_bowled'] % 6}" if bowler_stats['balls_bowled'] % 6 > 0 else str(bowler_stats['overs'])
                    economy = f"{(bowler_stats['runs'] * 6 / total_balls):.2f}" if total_balls > 0 else "0.00"
                    
                    writer.writerow([
                        bowler_name,
                        overs_display,
                        bowler_stats.get('maidens', 0),
                        bowler_stats.get('runs', 0),
                        bowler_stats.get('wickets', 0),
                        economy,
                        bowler_stats.get('wides', 0),
                        bowler_stats.get('noballs', 0)
                    ])
        
        print(f"📊 Created bowling CSV: {filename}")

    def _clean_html(self, text: str) -> str:
        """Remove HTML tags and clean text for file output"""
        import re
        
        # STEP 1: Fix end-of-over formatting BEFORE general HTML cleaning
        text = self._fix_end_of_over_formatting(text)
        
        # STEP 2: Convert <br> tags to double line breaks for better spacing
        clean = re.sub(r'<br\s*/?>', '\n\n', text, flags=re.IGNORECASE)
        
        # STEP 3: Remove other HTML tags
        clean = re.sub('<[^<]+?>', '', clean)
        
        # STEP 4: Replace HTML entities
        clean = clean.replace('&nbsp;', ' ')
        clean = clean.replace('&amp;', '&')
        clean = clean.replace('&lt;', '<')
        clean = clean.replace('&gt;', '>')
        
        return clean.strip()

    def _create_html_report(self, commentary_log: List[str]):
        """This will be called by frontend to save complete webpage"""
        # This method will be called when frontend sends the complete HTML
        print(f"🌐 HTML report will be created by frontend capture")

    def save_complete_webpage(self, html_content: str):
        """Save the complete webpage HTML as sent from frontend"""
        html_path = os.path.join(self.archive_path, self.html_filename)
        
        try:
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            
            print(f"🌐 Saved complete webpage: {self.html_filename}")
            return True
            
        except Exception as e:
            print(f"❌ Error saving webpage: {e}")
            return False

    def _fix_end_of_over_formatting(self, text: str) -> str:
        """Fix end-of-over statistics formatting"""
        import re
        
        # DEBUG: Print original text to see what we're working with
        if "End of over" in text:
            print(f"🐛 BEFORE formatting: {text[:200]}...")
        
        original_text = text
        
        # Pattern 1: After "End of over X** (Score: ...)" add line break before player name
        # Matches: ")PlayerName" where PlayerName starts with capital letter
        text = re.sub(r'(\*\*End of over \d+\*\* \([^)]+\))([A-Z][a-z])', r'\1<br>\2', text)
        
        # Pattern 2: After player stats "]PlayerName" add line break
        # Matches: "] PlayerName" or "]PlayerName"
        text = re.sub(r'(\]\s*)([A-Z][a-z]+\s+[A-Z])', r'\1<br>\2', text)
        
        # Pattern 3: After player stats with no space "]PlayerName"
        text = re.sub(r'(\])([A-Z][a-z]+)', r'\1<br>\2', text)
        
        # Pattern 4: Before bowler stats (has pattern like "X.X-X-X-X")
        # Matches: "PlayerName 2.0-0-17-0"
        text = re.sub(r'(\]\s*)([A-Z][a-z]+[^0-9]*\d+\.\d+-\d+-\d+-\d+)', r'\1<br>\2', text)
        
        # Pattern 5: Specific fix for your exact format - between stats and player names
        # Matches things like "6]Manish" or "0]Bumrah"
        text = re.sub(r'(\d\])([A-Z][a-z]+)', r'\1<br>\2', text)
        
        # Pattern 6: Between player stats ending with ") [" and next player
        text = re.sub(r'(\)\s*\[[^\]]+\])([A-Z][a-z]+)', r'\1<br>\2', text)
        
        # DEBUG: Show if any changes were made
        if text != original_text and "End of over" in text:
            print(f"🐛 AFTER formatting: {text[:300]}...")
            print(f"🐛 Changes made: {text != original_text}")
        
        return text

def find_original_json_file(match_id: str, base_path: str = "data/matches") -> str:
    """Find the original JSON file for a given match_id"""
    if not os.path.exists(base_path):
        return None
    
    for filename in os.listdir(base_path):
        if filename.endswith('.json'):
            filepath = os.path.join(base_path, filename)
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    if data.get('match_id') == match_id:
                        return filepath
            except Exception:
                continue
    
    return None