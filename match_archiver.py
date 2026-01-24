"""
Production-Level Match Archiver
===============================

Comprehensive cricket match archiving system that creates complete match records
including JSON data, commentary text, CSV statistics, HTML webpage, and ZIP packaging.

Features:
- Complete match data preservation
- Multiple output formats (JSON, TXT, CSV, HTML)
- Automatic ZIP packaging
- Robust error handling and validation
- Production-level logging
- Security and performance optimizations
- Offline-compatible HTML generation

Author: Cricket Simulation System
Version: 1.0.0 (Production)
"""

import os
import json
import csv
import shutil
import re
import tempfile
import logging
from datetime import datetime
from pathlib import Path
from tabulate import tabulate
from tabulate import tabulate
from typing import Dict, List, Any, Optional, Union
import zipfile

from database import db
from database.models import Match as DBMatch, MatchScorecard, Team as DBTeam, Player as DBPlayer

# ─── Define PROJECT_ROOT so that we can write to /<project_root>/data/… ─────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent


class MatchArchiverError(Exception):
    """Custom exception for MatchArchiver-specific errors"""
    pass


class MatchArchiver:
    """
    Production-level cricket match archiver with comprehensive error handling,
    validation, and multi-format output generation.
    """
    
    # Class constants
    REQUIRED_MATCH_FIELDS = ['match_id', 'created_by', 'timestamp', 'team_home', 'team_away']
    MIN_HTML_SIZE = 1000  # Minimum expected HTML size
    MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB max file size
    SUPPORTED_FORMATS = ['json', 'txt', 'csv', 'html', 'zip']
    
    def __init__(self, match_data: Dict[str, Any], match_instance: Any):
        """
        Initialize MatchArchiver with match data and instance.
        
        Args:
            match_data: Dictionary containing match metadata
            match_instance: Match object with game state and statistics
            
        Raises:
            MatchArchiverError: If required data is missing or invalid
        """
        self.logger = logging.getLogger(__name__)
        self.match_data = self._validate_match_data(match_data)
        self.match = match_instance
        
        # Extract core identifiers
        self.match_id = self.match_data.get('match_id')
        self.username = self.match_data.get('created_by')
        self.timestamp = self.match_data.get('timestamp')
        
        # Extract and validate team names
        self.team_home = self._extract_team_name(self.match_data.get('team_home', ''))
        self.team_away = self._extract_team_name(self.match_data.get('team_away', ''))
        
        if not self.team_home or not self.team_away:
            raise MatchArchiverError("Invalid team names in match data")
        
        # Generate standardized names
        self.folder_name = self._generate_folder_name()
        self.archive_path = Path("data") / self.folder_name
        
        # Generate all file names
        self.filenames = self._generate_filenames()
        
        # Initialize tracking
        self.created_files = []
        self.temp_files = []
        
        self.logger.info(f"MatchArchiver initialized for {self.team_home} vs {self.team_away} (ID: {self.match_id})")



    def _include_scorecard_images(self):
        """Include scorecard images in the archive if they exist"""
        temp_dir = Path("data") / "temp_scorecard_images"
        
        if not temp_dir.exists():
            return
        
        # Look for scorecard images for this match
        first_innings_img = temp_dir / f"{self.match_id}_first_innings_scorecard.png"
        second_innings_img = temp_dir / f"{self.match_id}_second_innings_scorecard.png"
        
        if first_innings_img.exists():
            dest_path = self.archive_path / f"{self.team_home}_vs_{self.team_away}_first_innings_scorecard.png"
            shutil.copy2(first_innings_img, dest_path)
            self.created_files.append(dest_path)
            self.logger.debug(f"Added first innings scorecard image: {dest_path.name}")
        
        if second_innings_img.exists():
            dest_path = self.archive_path / f"{self.team_home}_vs_{self.team_away}_second_innings_scorecard.png"
            shutil.copy2(second_innings_img, dest_path)
            self.created_files.append(dest_path)
            self.logger.debug(f"Added second innings scorecard image: {dest_path.name}")


    def _validate_match_data(self, match_data: Dict[str, Any]) -> Dict[str, Any]:
        """Validate match data contains required fields"""
        if not isinstance(match_data, dict):
            raise MatchArchiverError("Match data must be a dictionary")
        
        missing_fields = [field for field in self.REQUIRED_MATCH_FIELDS if not match_data.get(field)]
        if missing_fields:
            raise MatchArchiverError(f"Missing required match data fields: {missing_fields}")
        
        return match_data

    def _extract_team_name(self, team_identifier: str) -> str:
        """Extract clean team name from team identifier"""
        if not team_identifier:
            return ""
        return team_identifier.split('_')[0]

    def _generate_folder_name(self) -> str:
        """Generate standardized folder name for the archive"""
        # Sanitize components
        safe_home = re.sub(r'[^\w]', '', self.team_home)
        safe_away = re.sub(r'[^\w]', '', self.team_away)
        safe_username = re.sub(r'[^\w@.]', '', self.username)
        safe_timestamp = re.sub(r'[^\w]', '', self.timestamp)
        
        return f"playing_{safe_home}_vs_{safe_away}_{safe_username}_{safe_timestamp}"

    def _generate_filenames(self) -> Dict[str, str]:
        """Generate all required filenames with consistent naming"""
        base_name = f"playing_{self.team_home}_vs_{self.team_away}_{self.username}_{self.timestamp}"
        
        return {
            'json': f"{base_name}.json",
            'txt': f"{base_name}.txt",
            'html': f"{base_name}.html",
            'zip': f"{self.folder_name}.zip"
        }

    def create_archive(self, 
                  original_json_path: str, 
                  commentary_log: List[str], 
                  html_content: Optional[str] = None,
                  cleanup_temp: bool = True) -> bool:
        """
        Create complete match archive with all formats and ZIP packaging.
        
        Args:
            original_json_path: Path to original match JSON file
            commentary_log: List of commentary entries
            html_content: Complete HTML webpage content (optional)
            cleanup_temp: Whether to clean up temporary files (default: True)
            
        Returns:
            bool: True if archive creation successful, False otherwise
        """
        try:
            self.logger.info(f"Starting archive creation for match {self.match_id}")
            
            # Create archive directory
            self._create_archive_directory()
            
            # Create all individual files
            self._copy_json_file(original_json_path)
            self._create_commentary_text_file(commentary_log)
            self._create_all_csv_files()
            
            # Copy CSV files to data folder before any cleanup
            self._copy_csv_files_to_data_folder()
            
            self._include_scorecard_images()
            
            if html_content:
                self._create_html_file(html_content)
            
            # Create ZIP archive
            zip_path = self._create_zip_archive()
            
            # Validate final archive
            if not self._validate_archive(zip_path):
                raise MatchArchiverError("Archive validation failed")
            
            # Cleanup if requested
            if cleanup_temp:
                self._cleanup_temporary_files()
            
            # Save to Database
            self._save_to_database()
            self.logger.info(f"Match {self.match_id} saved to database")

            archive_size = os.path.getsize(zip_path)
            self.logger.info(f"Archive creation completed successfully: {zip_path} ({archive_size:,} bytes)")
            
            return True
            
        except Exception as e:
            self.logger.error(f"Archive creation failed: {e}", exc_info=True)
            self._cleanup_on_error()
            return False

    def get_csv_files_in_data_folder(self) -> List[str]:
        """Get list of CSV file paths that were copied to the data folder"""
        data_folder = Path("data")
        csv_files = []
        
        try:
            team_order = self._determine_team_batting_order()
            
            expected_csv_files = [
                f"{self.match_id}_{self.username}_{team_order['first_batting']}_batting.csv",
                f"{self.match_id}_{self.username}_{team_order['first_bowling']}_bowling.csv", 
                f"{self.match_id}_{self.username}_{team_order['second_batting']}_batting.csv",
                f"{self.match_id}_{self.username}_{team_order['second_bowling']}_bowling.csv"
            ]
            
            for filename in expected_csv_files:
                csv_path = data_folder / filename
                if csv_path.exists():
                    csv_files.append(str(csv_path))
            
        except Exception as e:
            self.logger.warning(f"Error getting CSV file paths: {e}")
        
        return csv_files

    def _create_archive_directory(self) -> None:
        """Create archive directory with proper permissions"""
        try:
            self.archive_path.mkdir(parents=True, exist_ok=True)
            
            # Verify directory is writable
            test_file = self.archive_path / ".test_write"
            test_file.touch()
            test_file.unlink()
            
            self.logger.debug(f"Archive directory created: {self.archive_path}")
            
        except Exception as e:
            raise MatchArchiverError(f"Failed to create archive directory: {e}")

    def _save_to_database(self) -> None:
        """Save match results and stats to SQLite database"""
        try:
            # 1. Resolve Team IDs from "SHORT_email" strings
            # Only splitting once, expecting standard format
            h_code, h_user = self.match_data['team_home'].split('_')
            a_code, a_user = self.match_data['team_away'].split('_')
            
            home_team = DBTeam.query.filter_by(short_code=h_code, user_id=h_user).first()
            away_team = DBTeam.query.filter_by(short_code=a_code, user_id=a_user).first()
            
            if not home_team or not away_team:
                self.logger.error("Could not resolve teams for DB save")
                return

            # Determine Winner (Logic restored)
            winner_team = None
            if self.match.result:
                if self.team_home in self.match.result:
                    winner_team = home_team
                elif self.team_away in self.match.result:
                    winner_team = away_team

            # 2. Check for Existing Match Record
            db_match = DBMatch.query.get(self.match_id)
            
            if db_match:
                self.logger.info(f"Match {self.match_id} already exists in DB. Updating record.")
                # Update existing fields
                db_match.user_id = self.username
                db_match.home_team_id = home_team.id
                db_match.away_team_id = away_team.id
                db_match.winner_team_id = winner_team.id if winner_team else None
                db_match.venue = self.match_data.get('stadium')
                db_match.pitch_type = self.match_data.get('pitch')
                db_match.date = datetime.utcnow()
                db_match.result_description = self.match.result
                db_match.match_json_path = self.filenames['json']
                
                # Clear existing scorecards to avoid duplication/stale data
                MatchScorecard.query.filter_by(match_id=self.match_id).delete()
            else:
                self.logger.info(f"Creating new DB record for Match {self.match_id}")
                db_match = DBMatch(
                    id=self.match_id,
                    user_id=self.username,
                    home_team_id=home_team.id,
                    away_team_id=away_team.id,
                    winner_team_id=winner_team.id if winner_team else None,
                    venue=self.match_data.get('stadium'),
                    pitch_type=self.match_data.get('pitch'),
                    date=datetime.utcnow(),
                    result_description=self.match.result,
                    # home_team_score will be set below
                    match_json_path=self.filenames['json']
                )
                db.session.add(db_match)
            
            # Extract scores and overs accurately
            first_bat_name = self.match.first_batting_team_name
            
            home_batting_stats = {}
            away_batting_stats = {}
            
            # Helper to calculate total overs faced
            def calc_overs(stats_dict):
                balls = sum(p.get('balls', 0) for p in stats_dict.values())
                return round(balls // 6 + (balls % 6) / 10.0, 1)

            if first_bat_name == self.match.match_data["team_home"].split('_')[0]:
                db_match.home_team_score = self.match.first_innings_score
                db_match.home_team_wickets = sum(1 for p in self.match.first_innings_batting_stats.values() if p.get('wicket_type'))
                db_match.home_team_overs = calc_overs(self.match.first_innings_batting_stats)
                home_batting_stats = self.match.first_innings_batting_stats
                
                db_match.away_team_score = self.match.score
                db_match.away_team_wickets = self.match.wickets
                db_match.away_team_overs = calc_overs(self.match.second_innings_batting_stats)
                away_batting_stats = self.match.second_innings_batting_stats
            else:
                db_match.away_team_score = self.match.first_innings_score
                db_match.away_team_wickets = sum(1 for p in self.match.first_innings_batting_stats.values() if p.get('wicket_type'))
                db_match.away_team_overs = calc_overs(self.match.first_innings_batting_stats)
                away_batting_stats = self.match.first_innings_batting_stats
                
                db_match.home_team_score = self.match.score
                db_match.home_team_wickets = self.match.wickets
                db_match.home_team_overs = calc_overs(self.match.second_innings_batting_stats)
                home_batting_stats = self.match.second_innings_batting_stats

            db.session.flush() 
            
            # 3. Update Tournament Fixture & Standings
            # Ensure tournament_id is set
            if self.match_data.get('tournament_id'):
                db_match.tournament_id = self.match_data.get('tournament_id')
                from engine.tournament_engine import TournamentEngine
                engine = TournamentEngine()
                try:
                    engine.update_standings(db_match)
                    self.logger.info(f"Updated tournament standings for match {self.match_id}")
                except Exception as te:
                    self.logger.error(f"Failed to update tournament standings: {te}")

            # 4. Save Scorecards
            def save_stats(stats_dict, team_id, batting=True):
                if not stats_dict: return
                for p_name, s in stats_dict.items():
                    # Find player ID
                    player = DBPlayer.query.filter_by(name=p_name, team_id=team_id).first()
                    if not player:
                        continue
                    
                    card = MatchScorecard.query.filter_by(match_id=self.match_id, player_id=player.id).first()
                    if not card:
                        card = MatchScorecard(
                            match_id=self.match_id,
                            player_id=player.id,
                            team_id=team_id
                        )
                        db.session.add(card)
                    
                    if batting:
                        card.runs = s.get('runs', 0)
                        card.balls = s.get('balls', 0)
                        card.fours = s.get('fours', 0)
                        card.sixes = s.get('sixes', 0)
                        card.is_out = bool(s.get('wicket_type'))
                        card.wicket_type = s.get('wicket_type')
                    else:
                        card.overs = s.get('overs', 0)
                        card.balls_bowled = s.get('balls_bowled', 0)
                        card.runs_conceded = s.get('runs', 0)
                        card.wickets = s.get('wickets', 0)
                        card.maidens = s.get('maidens', 0)
                        card.wides = s.get('wides', 0)
                        card.noballs = s.get('noballs', 0)

            # Process Home Stats (Batting)
            save_stats(home_batting_stats, home_team.id, batting=True)
            
            # Process Home Stats (Bowling - when Away batted)
            h_bowl_stats = self.match.second_innings_bowling_stats if first_bat_name == self.match.match_data["team_home"].split('_')[0] else self.match.first_innings_bowling_stats
            save_stats(h_bowl_stats, home_team.id, batting=False)
            
            # Process Away Stats (Batting)
            save_stats(away_batting_stats, away_team.id, batting=True)
            
            # Process Away Stats (Bowling - when Home batted)
            a_bowl_stats = self.match.first_innings_bowling_stats if first_bat_name == self.match.match_data["team_home"].split('_')[0] else self.match.second_innings_bowling_stats
            save_stats(a_bowl_stats, away_team.id, batting=False)
            
            # Update Player Aggregates
            for card in [c for c in db.session.new if isinstance(c, MatchScorecard)]:
                # relationship loading fallback
                p = DBPlayer.query.get(card.player_id)
                if not p: continue
                
                p.matches_played += 1
                p.total_runs += card.runs
                p.total_balls_faced += card.balls
                p.total_fours += card.fours
                p.total_sixes += card.sixes
                if card.runs >= 50 and card.runs < 100: p.total_fifties += 1
                if card.runs >= 100: p.total_centuries += 1
                if card.runs > p.highest_score: p.highest_score = card.runs
                if not card.is_out and card.balls > 0: p.not_outs += 1
                
                p.total_balls_bowled += card.balls_bowled
                p.total_runs_conceded += card.runs_conceded
                p.total_wickets += card.wickets
                p.total_maidens += card.maidens
                if card.wickets >= 5: p.five_wicket_hauls += 1
                
                if card.wickets > p.best_bowling_wickets:
                    p.best_bowling_wickets = card.wickets
                    p.best_bowling_runs = card.runs_conceded
                elif card.wickets == p.best_bowling_wickets:
                    if card.runs_conceded < p.best_bowling_runs:
                        p.best_bowling_runs = card.runs_conceded

            db.session.commit()
            
        except Exception as e:
            self.logger.error(f"DB Save Error: {e}", exc_info=True)
            db.session.rollback()

    def _copy_json_file(self, original_path: str) -> None:
        """Copy original JSON file to archive with validation"""
        if not os.path.exists(original_path):
            raise MatchArchiverError(f"Original JSON file not found: {original_path}")
        
        destination = self.archive_path / self.filenames['json']
        
        try:
            # Validate JSON before copying
            with open(original_path, 'r', encoding='utf-8') as f:
                json.load(f)  # Validate JSON format
            
            shutil.copy2(original_path, destination)
            self.created_files.append(destination)
            
            self.logger.debug(f"JSON file copied: {self.filenames['json']}")
            
        except json.JSONDecodeError as e:
            raise MatchArchiverError(f"Invalid JSON in original file: {e}")
        except Exception as e:
            raise MatchArchiverError(f"Failed to copy JSON file: {e}")

    def _create_commentary_text_file(self, commentary_log: List[str]) -> None:
        """Create comprehensive text file with commentary and statistics"""
        txt_path = self.archive_path / self.filenames['txt']
        
        try:
            with open(txt_path, 'w', encoding='utf-8') as f:
                # Write header
                f.write(self._generate_text_header())
                
                # Write playing XIs
                f.write(self._format_playing_xi())
                
                # Write live commentary
                f.write(self._format_commentary_section(commentary_log))
                
                # Write detailed scorecards
                f.write(self._format_detailed_scorecards())
                
                # Write match summary
                f.write(self._format_match_summary())
            
            self.created_files.append(txt_path)
            self.logger.debug(f"Text file created: {self.filenames['txt']}")
            
        except Exception as e:
            raise MatchArchiverError(f"Failed to create text file: {e}")

    def _generate_text_header(self) -> str:
        """Generate formatted header for text file"""
        header_lines = [
            "=" * 80,
            "CRICKET MATCH ARCHIVE - OFFICIAL RECORD",
            "=" * 80,
            f"Match: {self.team_home} vs {self.team_away}",
            f"Match ID: {self.match_id}",
            f"Date: {self.timestamp}",
            f"Created by: {self.username}",
            f"Stadium: {self.match_data.get('stadium', 'N/A')}",
            f"Pitch: {self.match_data.get('pitch', 'N/A')}",
            f"Rain Probability: {(self.match_data.get('rain_probability', 0) * 100):.1f}%",
            f"Archive Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 80,
            ""
        ]
        return "\n".join(header_lines)

    def _format_playing_xi(self) -> str:
        """Format playing XI for both teams"""
        lines = []
        
        try:
            # Team 1 Playing XI
            lines.extend([
                f"TEAM 1 - {self.team_home} PLAYING XI:",
                "-" * 40
            ])
            
            home_xi = self.match_data.get('playing_xi', {}).get('home', [])
            for i, player in enumerate(home_xi, 1):
                bowling_info = " (Bowling)" if player.get('will_bowl', False) else ""
                lines.append(f"{i:2}. {player.get('name', 'Unknown')} ({player.get('role', 'Unknown')}){bowling_info}")
            
            lines.extend(["", f"TEAM 2 - {self.team_away} PLAYING XI:", "-" * 40])
            
            away_xi = self.match_data.get('playing_xi', {}).get('away', [])
            for i, player in enumerate(away_xi, 1):
                bowling_info = " (Bowling)" if player.get('will_bowl', False) else ""
                lines.append(f"{i:2}. {player.get('name', 'Unknown')} ({player.get('role', 'Unknown')}){bowling_info}")
            
            lines.extend(["", ""])
            
        except Exception as e:
            self.logger.warning(f"Error formatting playing XI: {e}")
            lines.extend(["Playing XI information unavailable", "", ""])
        
        return "\n".join(lines)

    def _format_commentary_section(self, commentary_log: List[str]) -> str:
        """Format commentary section with proper cleaning"""
        lines = [
            "LIVE COMMENTARY",
            "=" * 50,
            ""
        ]
        
        for comment in commentary_log:
            cleaned_comment = self._clean_html_for_text(comment)
            if cleaned_comment.strip():
                lines.append(cleaned_comment)
                lines.append("")  # Add spacing between comments
        
        lines.extend(["", ""])
        return "\n".join(lines)

    def _format_detailed_scorecards(self) -> str:
        """Format comprehensive scorecards using tabulate"""
        lines = [
            "=" * 80,
            "DETAILED MATCH SCORECARDS",
            "=" * 80,
            ""
        ]
        
        try:
            # Determine team batting order
            team_order = self._determine_team_batting_order()
            
            # First innings scorecard
            if hasattr(self.match, 'first_innings_batting_stats'):
                lines.extend(self._format_innings_scorecard(
                    innings_num=1,
                    batting_team=team_order['first_batting'],
                    bowling_team=team_order['first_bowling'],
                    batting_stats=self.match.first_innings_batting_stats,
                    bowling_stats=self.match.first_innings_bowling_stats
                ))
            
            # Second innings scorecard
            if hasattr(self.match, 'second_innings_batting_stats'):
                lines.extend(self._format_innings_scorecard(
                    innings_num=2,
                    batting_team=team_order['second_batting'],
                    bowling_team=team_order['second_bowling'],
                    batting_stats=self.match.second_innings_batting_stats,
                    bowling_stats=self.match.second_innings_bowling_stats
                ))
            
            # Match result
            if hasattr(self.match, 'result') and self.match.result:
                lines.extend([
                    "",
                    "=" * 80,
                    f"MATCH RESULT: {self.match.result}",
                    "=" * 80
                ])
        
        except Exception as e:
            self.logger.warning(f"Error formatting scorecards: {e}")
            lines.append("Scorecard information unavailable due to data formatting issues")
        
        return "\n".join(lines)

    def _determine_team_batting_order(self) -> Dict[str, str]:
        """Determine which team batted first"""
        if hasattr(self.match, 'first_batting_team_name') and self.match.first_batting_team_name:
            first_batting = self.match.first_batting_team_name
            first_bowling = self.match.first_bowling_team_name
        else:
            # Fallback logic
            first_batting = self.team_home
            first_bowling = self.team_away
        
        return {
            'first_batting': first_batting,
            'first_bowling': first_bowling,
            'second_batting': first_bowling,
            'second_bowling': first_batting
        }

    def _format_innings_scorecard(self, innings_num: int, batting_team: str, 
                                bowling_team: str, batting_stats: Dict, 
                                bowling_stats: Dict) -> List[str]:
        """Format a single innings scorecard"""
        lines = [
            f"{innings_num}{'ST' if innings_num == 1 else 'ND'} INNINGS - {batting_team} BATTING",
            "-" * 60
        ]
        
        # Batting table
        lines.append(self._create_batting_table(batting_stats))
        lines.extend([
            "",
            f"{innings_num}{'ST' if innings_num == 1 else 'ND'} INNINGS - {bowling_team} BOWLING",
            "-" * 60
        ])
        
        # Bowling table
        lines.append(self._create_bowling_table(bowling_stats))
        lines.extend(["", ""])
        
        return lines

    def _create_batting_table(self, batting_stats: Dict) -> str:
        """Create formatted batting statistics table"""
        if not batting_stats:
            return "No batting statistics available"
        
        headers = ['Player', 'Runs', 'Balls', '1s', '2s', '3s', '4s', '6s', 'Dots', 'S/R', 'Status']
        rows = []
        
        for player_name, stats in batting_stats.items():
            if stats.get('balls', 0) > 0 or stats.get('wicket_type'):
                strike_rate = f"{(stats['runs'] * 100 / stats['balls']):.1f}" if stats['balls'] > 0 else "0.0"
                status = stats.get('wicket_type', '') or "not out"
                
                rows.append([
                    player_name,
                    stats.get('runs', 0),
                    stats.get('balls', 0),
                    stats.get('ones', 0),
                    stats.get('twos', 0),
                    stats.get('threes', 0),
                    stats.get('fours', 0),
                    stats.get('sixes', 0),
                    stats.get('dots', 0),
                    strike_rate,
                    status
                ])
        
        if not rows:
            return "No batting data available"
        
        return tabulate(rows, headers=headers, tablefmt="grid")

    def _create_bowling_table(self, bowling_stats: Dict) -> str:
        """Create formatted bowling statistics table"""
        if not bowling_stats:
            return "No bowling statistics available"
        
        headers = ['Bowler', 'Overs', 'Maidens', 'Runs', 'Wickets', 'Economy', 'Wides', 'No Balls']
        rows = []
        
        for bowler_name, stats in bowling_stats.items():
            if stats.get('balls_bowled', 0) > 0:
                total_balls = stats['overs'] * 6 + (stats['balls_bowled'] % 6)
                overs_display = f"{stats['overs']}.{stats['balls_bowled'] % 6}" if stats['balls_bowled'] % 6 > 0 else str(stats['overs'])
                economy = f"{(stats['runs'] * 6 / total_balls):.2f}" if total_balls > 0 else "0.00"
                
                rows.append([
                    bowler_name,
                    overs_display,
                    stats.get('maidens', 0),
                    stats.get('runs', 0),
                    stats.get('wickets', 0),
                    economy,
                    stats.get('wides', 0),
                    stats.get('noballs', 0)
                ])
        
        if not rows:
            return "No bowling data available"
        
        return tabulate(rows, headers=headers, tablefmt="grid")

    def _format_match_summary(self) -> str:
        """Generate match summary statistics"""
        lines = [
            "=" * 80,
            "MATCH SUMMARY & STATISTICS",
            "=" * 80,
            ""
        ]
        
        try:
            # Add toss information
            toss_winner = self.match_data.get('toss_winner', 'Unknown')
            toss_decision = self.match_data.get('toss_decision', 'Unknown')
            lines.extend([
                f"Toss: {toss_winner} won and chose to {toss_decision}",
                ""
            ])
            
            # Add match conditions
            if self.match_data.get('rain_probability', 0) > 0:
                lines.append(f"Rain Probability: {(self.match_data['rain_probability'] * 100):.1f}%")
            
            # Add any rain delays if occurred
            if hasattr(self.match, 'rain_affected') and self.match.rain_affected:
                lines.extend([
                    "Match affected by rain - DLS method applied",
                    ""
                ])
            
            lines.extend([
                f"Archive created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"Total files in archive: {len(self.created_files) + 1}",  # +1 for upcoming ZIP
                ""
            ])
        
        except Exception as e:
            self.logger.warning(f"Error generating match summary: {e}")
            lines.append("Match summary unavailable")
        
        return "\n".join(lines)

    def _copy_csv_files_to_data_folder(self) -> None:
        """Copy all CSV files to the main data folder for permanent storage"""
        try:
            data_folder = Path("data")
            data_folder.mkdir(parents=True, exist_ok=True)
            
            csv_files_copied = 0
            for file_path in self.created_files:
                if file_path.suffix.lower() == '.csv':
                    destination = data_folder / file_path.name
                    try:
                        shutil.copy2(file_path, destination)
                        csv_files_copied += 1
                        self.logger.debug(f"CSV copied to data folder: {file_path.name}")
                    except Exception as copy_error:
                        self.logger.warning(f"Failed to copy CSV {file_path.name} to data folder: {copy_error}")
            
            self.logger.info(f"Copied {csv_files_copied} CSV files to data folder")
            
        except Exception as e:
            self.logger.warning(f"Error copying CSV files to data folder: {e}")
            # Don't raise exception as this is not critical for archive creation

    def _create_all_csv_files(self) -> None:
        """Create all CSV files for batting and bowling statistics"""
        try:
            team_order = self._determine_team_batting_order()
            
            # Get full team lineups
            home_xi = self.match_data.get('playing_xi', {}).get('home', [])
            away_xi = self.match_data.get('playing_xi', {}).get('away', [])
            
            # Determine which team's lineup to use for each innings
            if team_order['first_batting'] == self.team_home:
                first_batting_lineup = home_xi
                second_batting_lineup = away_xi
            else:
                first_batting_lineup = away_xi
                second_batting_lineup = home_xi
            
            if team_order['first_bowling'] == self.team_home:
                first_bowling_lineup = home_xi
                second_bowling_lineup = away_xi
            else:
                first_bowling_lineup = away_xi
                second_bowling_lineup = home_xi
            
            # Create CSV files for both innings with team names and full lineups
            csv_files = [
                (f"{self.match_id}_{self.username}_{team_order['first_batting']}_batting.csv", 
                getattr(self.match, 'first_innings_batting_stats', {}),
                team_order['first_batting'], 'batting', first_batting_lineup),
                (f"{self.match_id}_{self.username}_{team_order['first_bowling']}_bowling.csv", 
                getattr(self.match, 'first_innings_bowling_stats', {}),
                team_order['first_bowling'], 'bowling', first_bowling_lineup),
                (f"{self.match_id}_{self.username}_{team_order['second_batting']}_batting.csv", 
                getattr(self.match, 'second_innings_batting_stats', {}),
                team_order['second_batting'], 'batting', second_batting_lineup),
                (f"{self.match_id}_{self.username}_{team_order['second_bowling']}_bowling.csv", 
                getattr(self.match, 'second_innings_bowling_stats', {}),
                team_order['second_bowling'], 'bowling', second_bowling_lineup)
            ]
            
            for filename, stats, team_name, file_type, lineup in csv_files:
                if file_type == 'batting':
                    self._create_batting_csv(filename, stats, team_name, lineup)
                else:
                    self._create_bowling_csv(filename, stats, team_name)
            
            self.logger.debug("All CSV files created successfully")
            
        except Exception as e:
            raise MatchArchiverError(f"Failed to create CSV files: {e}")

    def _create_batting_csv(self, filename: str, stats: Dict, team_name: str, full_lineup: List) -> None:
        """Create batting statistics CSV file with comprehensive data including all players"""
        csv_path = self.archive_path / filename
        
        try:
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                
                # Enhanced headers with team name
                headers = [
                    'Player Name', 'Team Name', 'Runs', 'Balls', '1s', '2s', '3s', 'Fours', 'Sixes', 
                    'Dots', 'Strike Rate', 'Status', 'Bowler Out', 'Fielder Out'
                ]
                writer.writerow(headers)
                
                # Write data for ALL players in the lineup
                for player in full_lineup:
                    player_name = player.get('name', 'Unknown')
                    
                    # Check if player actually batted (has balls faced > 0 OR has a wicket type)
                    if (player_name in stats and 
                        (stats[player_name].get('balls', 0) > 0 or stats[player_name].get('wicket_type'))):
                        
                        # Player has stats (actually batted)
                        player_stats = stats[player_name]
                        strike_rate = f"{(player_stats['runs'] * 100 / player_stats['balls']):.2f}" if player_stats['balls'] > 0 else "0.00"
                        status = player_stats.get('wicket_type', '') or "not out"
                        
                        writer.writerow([
                            player_name,
                            team_name,
                            player_stats.get('runs', 0),
                            player_stats.get('balls', 0),
                            player_stats.get('ones', 0),
                            player_stats.get('twos', 0),
                            player_stats.get('threes', 0),
                            player_stats.get('fours', 0),
                            player_stats.get('sixes', 0),
                            player_stats.get('dots', 0),
                            strike_rate,
                            status,
                            player_stats.get('bowler_out', ''),
                            player_stats.get('fielder_out', '')
                        ])
                    else:
                        # Player didn't bat - include only name and team, rest empty
                        writer.writerow([
                            player_name,
                            team_name,
                            '',  # Empty runs
                            '',  # Empty balls
                            '',  # Empty 1s
                            '',  # Empty 2s
                            '',  # Empty 3s
                            '',  # Empty 4s
                            '',  # Empty 6s
                            '',  # Empty dots
                            '',  # Empty strike rate
                            '',  # Empty status
                            '',  # Empty bowler out
                            ''   # Empty fielder out
                        ])
            
            self.created_files.append(csv_path)
            self.logger.debug(f"Batting CSV created: {filename}")
            
        except Exception as e:
            raise MatchArchiverError(f"Failed to create batting CSV {filename}: {e}")

    def _create_bowling_csv(self, filename: str, stats: Dict, team_name: str) -> None:
        """Create bowling statistics CSV file with comprehensive data including team name"""
        csv_path = self.archive_path / filename
        
        try:
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                
                # Enhanced headers with team name
                headers = [
                    'Bowler Name', 'Team Name', 'Overs', 'Maidens', 'Runs', 'Wickets', 
                    'Economy', 'Wides', 'No Balls', 'Byes', 'Leg Byes'
                ]
                writer.writerow(headers)
                
                # Write bowler data
                for bowler_name, bowler_stats in stats.items():
                    if bowler_stats.get('balls_bowled', 0) > 0:
                        total_balls = bowler_stats['overs'] * 6 + (bowler_stats['balls_bowled'] % 6)
                        overs_display = f"{bowler_stats['overs']}.{bowler_stats['balls_bowled'] % 6}" if bowler_stats['balls_bowled'] % 6 > 0 else str(bowler_stats['overs'])
                        economy = f"{(bowler_stats['runs'] * 6 / total_balls):.2f}" if total_balls > 0 else "0.00"
                        
                        writer.writerow([
                            bowler_name,
                            team_name,  # Added team name
                            overs_display,
                            bowler_stats.get('maidens', 0),
                            bowler_stats.get('runs', 0),
                            bowler_stats.get('wickets', 0),
                            economy,
                            bowler_stats.get('wides', 0),
                            bowler_stats.get('noballs', 0),
                            bowler_stats.get('byes', 0),
                            bowler_stats.get('legbyes', 0)
                        ])
            
            self.created_files.append(csv_path)
            self.logger.debug(f"Bowling CSV created: {filename}")
            
        except Exception as e:
            raise MatchArchiverError(f"Failed to create bowling CSV {filename}: {e}")

    def _create_html_file(self, html_content: str) -> None:
        """Create HTML file with enhanced archival features"""
        if not self._validate_html_content(html_content):
            raise MatchArchiverError("Invalid HTML content provided")
        
        html_path = self.archive_path / self.filenames['html']
        
        try:
            cleaned_html = self._clean_html_for_archive(html_content)
            
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(cleaned_html)
            
            self.created_files.append(html_path)
            self.logger.debug(f"HTML file created: {self.filenames['html']}")
            
        except Exception as e:
            raise MatchArchiverError(f"Failed to create HTML file: {e}")

    def _validate_html_content(self, html_content: str) -> bool:
        """Comprehensive HTML content validation"""
        if not html_content or not isinstance(html_content, str):
            self.logger.warning("HTML content is empty or not a string")
            return False
        
        if len(html_content) < self.MIN_HTML_SIZE:
            self.logger.warning(f"HTML content too small: {len(html_content)} < {self.MIN_HTML_SIZE}")
            return False
        
        if len(html_content) > self.MAX_FILE_SIZE:
            self.logger.warning(f"HTML content too large: {len(html_content)} > {self.MAX_FILE_SIZE}")
            return False
        
        # Check for essential HTML elements
        required_patterns = [
            r'<html[^>]*>',
            r'<head[^>]*>',
            r'<body[^>]*>',
            self.team_home.lower(),
            self.team_away.lower()
        ]
        
        missing_patterns = []
        for pattern in required_patterns:
            if not re.search(pattern, html_content, re.IGNORECASE):
                missing_patterns.append(pattern)
        
        if missing_patterns:
            self.logger.warning(f"HTML missing expected patterns: {missing_patterns}")
        
        return True

    def _clean_html_for_archive(self, html_content: str) -> str:
        """Clean and enhance HTML for archival storage"""
        
        # Add archive metadata header
        archive_metadata = f"""
<!--
═══════════════════════════════════════════════════════════════════════════════
CRICKET MATCH ARCHIVE - OFFICIAL HTML RECORD
═══════════════════════════════════════════════════════════════════════════════
Match: {self.team_home} vs {self.team_away}
Match ID: {self.match_id}
Date: {self.timestamp}
Created by: {self.username}
Stadium: {self.match_data.get('stadium', 'N/A')}
Pitch: {self.match_data.get('pitch', 'N/A')}
Rain Probability: {(self.match_data.get('rain_probability', 0) * 100):.1f}%

Archive Details:
- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- Archive Version: 1.0.0
- File Type: Complete Interactive Match Report
- Offline Compatible: Yes

Note: This is an archived version of the live match simulation.
All functionality preserved for offline viewing.
═══════════════════════════════════════════════════════════════════════════════
-->

"""
        
        # Insert metadata after DOCTYPE
        if html_content.startswith('<!DOCTYPE html>'):
            html_content = html_content.replace('<!DOCTYPE html>', f'<!DOCTYPE html>{archive_metadata}')
        else:
            html_content = archive_metadata + html_content
        
        # Add archive banner
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        archive_banner = f'''
<div id="archive-banner" style="
    background: linear-gradient(135deg, #ff6b6b, #ee5a24);
    color: white;
    padding: 0.8rem;
    text-align: center;
    font-weight: bold;
    font-family: 'Segoe UI', sans-serif;
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    z-index: 10000;
    box-shadow: 0 2px 10px rgba(0,0,0,0.3);
    border-bottom: 3px solid #c23616;
">
    📁 ARCHIVED MATCH REPORT
    <span style="margin: 0 1rem; opacity: 0.8;">•</span>
    {self.team_home} vs {self.team_away}
    <span style="margin: 0 1rem; opacity: 0.8;">•</span>
    Saved: {current_time}
    <span style="margin-left: 1rem; font-size: 0.9em; opacity: 0.9;">
        (Match ID: {self.match_id})
    </span>
</div>
<div style="height: 60px; margin-bottom: 1rem;"></div>
'''
        
        # Insert banner after body tag
        body_pattern = r'(<body[^>]*>)'
        if re.search(body_pattern, html_content, re.IGNORECASE):
            html_content = re.sub(body_pattern, r'\1' + archive_banner, html_content, flags=re.IGNORECASE)
        
        # Add archive-specific CSS for better offline experience
        archive_css = '''
<style id="archive-enhancements">
/* Archive-specific enhancements */
@media print {
    #archive-banner { display: none !important; }
    .toggle-theme { display: none !important; }
}

/* Ensure good contrast for archived version */
:root {
    --archive-text: #2c3e50;
    --archive-bg: #ecf0f1;
}

/* Add timestamp to printed versions */
@page {
    @bottom-right {
        content: "Archived: ''' + current_time + '''";
        font-size: 10px;
        color: #7f8c8d;
    }
}
</style>
'''
        
        # Insert CSS before closing head tag
        if '</head>' in html_content:
            html_content = html_content.replace('</head>', archive_css + '</head>')
        
        return html_content

    def _clean_html_for_text(self, html_string: str) -> str:
        """Clean HTML for text output with improved formatting"""
        if not html_string:
            return ""
        
        # First fix end-of-over formatting issues
        cleaned = self._fix_end_of_over_formatting(html_string)
        
        # Convert common HTML elements to text equivalents
        replacements = {
            '<br>': '\n',
            '<br/>': '\n',
            '<br />': '\n',
            '<strong>': '',
            '</strong>': '',
            '<b>': '',
            '</b>': '',
            '<em>': '',
            '</em>': '',
            '<i>': '',
            '</i>': '',
            '&nbsp;': ' ',
            '&amp;': '&',
            '&lt;': '<',
            '&gt;': '>',
            '&quot;': '"',
            '&#39;': "'"
        }
        
        for html_entity, replacement in replacements.items():
            cleaned = cleaned.replace(html_entity, replacement)
        
        # Remove remaining HTML tags
        cleaned = re.sub(r'<[^>]+>', '', cleaned)
        
        # Clean up excessive whitespace
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)  # Max 2 consecutive newlines
        cleaned = re.sub(r' {2,}', ' ', cleaned)      # Max 1 space between words
        
        return cleaned.strip()

    def _fix_end_of_over_formatting(self, text: str) -> str:
        """Fix end-of-over statistics formatting issues"""
        if "End of over" not in text:
            return text
        
        # Pattern fixes for end-of-over formatting
        patterns = [
            (r'(\*\*End of over \d+\*\* \([^)]+\))([A-Z][a-z])', r'\1\n\2'),
            (r'(\]\s*)([A-Z][a-z]+\s+[A-Z])', r'\1\n\2'),
            (r'(\])([A-Z][a-z]+)', r'\1\n\2'),
            (r'(\]\s*)([A-Z][a-z]+[^0-9]*\d+\.\d+-\d+-\d+-\d+)', r'\1\n\2'),
            (r'(\d\])([A-Z][a-z]+)', r'\1\n\2'),
            (r'(\)\s*\[[^\]]+\])([A-Z][a-z]+)', r'\1\n\2')
        ]
        
        for pattern, replacement in patterns:
            text = re.sub(pattern, replacement, text)
        
        return text

    def _create_zip_archive(self) -> str:
        """
        Create a ZIP archive containing all files in self.created_files.
        The resulting ZIP is written to <PROJECT_ROOT>/data/<zip_name>.
        Returns the absolute path to the ZIP file as a string.
        Raises MatchArchiverError on any failure.
        """
        # Determine destination folder and filename
        zip_name = self.filenames['zip']  # e.g. "playing_TeamA_vs_TeamB_user_20250531215311.zip"
        zip_dir = Path(PROJECT_ROOT) / "data"
        zip_dir.mkdir(parents=True, exist_ok=True)

        zip_path = zip_dir / zip_name
        try:
            self.logger.debug(f"🐛 DEBUG: Starting ZIP creation at {zip_path}")

            # Create the ZIP with moderate compression
            with zipfile.ZipFile(zip_path, mode='w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipf:
                for file_path in self.created_files:
                    if file_path.exists() and file_path.is_file():
                        arcname = file_path.name
                        zipf.write(file_path, arcname)
                        self.logger.debug(f"🐛 DEBUG: Added to ZIP: {arcname}")
                    else:
                        self.logger.warning(f"⚠️ WARNING: Skipping missing or invalid file: {file_path}")

                # After writing, check that at least one file of each expected type is present
                namelist = set(zipf.namelist())
                expected_exts = {'.json', '.txt', '.csv'}
                for ext in expected_exts:
                    if not any(name.endswith(ext) for name in namelist):
                        self.logger.warning(f"⚠️ WARNING: ZIP archive missing files with extension: {ext}")

            # Integrity check: test for any corrupted entries
            self.logger.debug("🐛 DEBUG: Verifying ZIP integrity via testzip()")
            with zipfile.ZipFile(zip_path, mode='r') as zipf:
                bad_file = zipf.testzip()
                if bad_file:
                    raise MatchArchiverError(f"Corrupted member in ZIP: {bad_file!r}")

            # Final validation: ensure it's recognized as a zipfile
            if not zipfile.is_zipfile(zip_path):
                raise MatchArchiverError("Resulting file is not a valid ZIP archive")

            size_bytes = zip_path.stat().st_size
            self.logger.info(f"✅ INFO: ZIP archive created successfully at {zip_path} ({size_bytes} bytes)")
            return str(zip_path)

        except Exception as e:
            msg = f"❌ ERROR: Failed to create ZIP archive at {zip_path}: {e}"
            self.logger.error(msg)
            raise MatchArchiverError(msg)

    def _validate_archive(self, zip_path: str) -> bool:
        """Comprehensive archive validation"""
        try:
            # Check ZIP file exists and is valid
            if not os.path.exists(zip_path):
                self.logger.error("ZIP file does not exist")
                return False
            
            if not zipfile.is_zipfile(zip_path):
                self.logger.error("File is not a valid ZIP archive")
                return False
            
            # Check ZIP contents
            with zipfile.ZipFile(zip_path, 'r') as zipf:
                files_in_zip = zipf.namelist()
                
                # Verify minimum required files
                required_patterns = [r'\.json$', r'\.txt$', r'\.csv$']
                for pattern in required_patterns:
                    if not any(re.search(pattern, f) for f in files_in_zip):
                        self.logger.error(f"ZIP missing files matching pattern: {pattern}")
                        return False
                
                # Test ZIP integrity
                zipf.testzip()
            
            # Check file size is reasonable
            zip_size = os.path.getsize(zip_path)
            if zip_size < 1024:  # Less than 1KB is suspicious
                self.logger.warning(f"ZIP file unusually small: {zip_size} bytes")
            elif zip_size > self.MAX_FILE_SIZE:
                self.logger.warning(f"ZIP file very large: {zip_size} bytes")
            
            self.logger.info(f"Archive validation passed: {zip_size:,} bytes")
            return True
            
        except Exception as e:
            self.logger.error(f"Archive validation failed: {e}")
            return False
        
    
    def _cleanup_temporary_files(self) -> None:
        """
        Comprehensive cleanup of all temporary files and directories.
        Uses multiple strategies to ensure complete cleanup even in edge cases.
        """
        cleanup_success = True
        
        # ========== PHASE 1: MAIN ARCHIVE DIRECTORY CLEANUP ==========
        try:
            self.logger.debug(f"🧹 Phase 1: Starting cleanup of main archive directory")
            self.logger.debug(f"Target directory: {self.archive_path}")
            
            if self.archive_path.exists():
                self.logger.debug(f"Directory exists with {len(list(self.archive_path.iterdir()))} items")
                
                # Strategy 1: Standard removal
                try:
                    shutil.rmtree(self.archive_path)
                    self.logger.info(f"✅ Successfully removed archive directory: {self.archive_path}")
                    
                except PermissionError as pe:
                    self.logger.warning(f"⚠️ Permission error during standard cleanup: {pe}")
                    cleanup_success = False
                    
                    # Strategy 2: Force permission change and retry
                    try:
                        self.logger.debug("🔧 Attempting permission fix and retry...")
                        self._force_remove_directory(self.archive_path)
                        self.logger.info(f"✅ Force removal successful: {self.archive_path}")
                        cleanup_success = True
                        
                    except Exception as force_error:
                        self.logger.error(f"❌ Force removal failed: {force_error}")
                        
                except Exception as std_error:
                    self.logger.warning(f"⚠️ Standard removal failed: {std_error}")
                    cleanup_success = False
                    
                    # Strategy 3: Individual file removal
                    try:
                        self.logger.debug("🔧 Attempting individual file removal...")
                        self._remove_directory_contents_individually(self.archive_path)
                        self.logger.info(f"✅ Individual removal successful: {self.archive_path}")
                        cleanup_success = True
                        
                    except Exception as individual_error:
                        self.logger.error(f"❌ Individual removal failed: {individual_error}")
            else:
                self.logger.debug("Directory does not exist, skipping archive cleanup")
                
        except Exception as phase1_error:
            self.logger.error(f"❌ Phase 1 cleanup failed completely: {phase1_error}")
            cleanup_success = False

        # ========== PHASE 2: TRACKED TEMP FILES CLEANUP ==========
        try:
            self.logger.debug(f"🧹 Phase 2: Cleaning tracked temp files ({len(self.temp_files)} files)")
            
            for temp_file in self.temp_files:
                try:
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                        self.logger.debug(f"✅ Removed tracked file: {temp_file}")
                    else:
                        self.logger.debug(f"Tracked file already gone: {temp_file}")
                        
                except Exception as file_error:
                    self.logger.warning(f"⚠️ Failed to remove tracked file {temp_file}: {file_error}")
                    cleanup_success = False
                    
        except Exception as phase2_error:
            self.logger.error(f"❌ Phase 2 cleanup failed: {phase2_error}")
            cleanup_success = False

        # ========== PHASE 3: SCORECARD IMAGES CLEANUP ==========
        try:
            self.logger.debug(f"🧹 Phase 3: Cleaning scorecard images for match {self.match_id}")
            temp_dir = Path("data") / "temp_scorecard_images"
            
            if temp_dir.exists():
                # Clean this match's specific images
                match_images = [
                    temp_dir / f"{self.match_id}_first_innings_scorecard.png",
                    temp_dir / f"{self.match_id}_second_innings_scorecard.png"
                ]
                
                images_removed = 0
                for img_path in match_images:
                    try:
                        if img_path.exists():
                            img_path.unlink()
                            images_removed += 1
                            self.logger.debug(f"✅ Removed scorecard image: {img_path.name}")
                        else:
                            self.logger.debug(f"Scorecard image not found: {img_path.name}")
                            
                    except Exception as img_error:
                        self.logger.warning(f"⚠️ Failed to remove image {img_path.name}: {img_error}")
                        cleanup_success = False
                
                self.logger.debug(f"Removed {images_removed} scorecard images for this match")
                
                # Clean up old images (older than 2 hours) to prevent accumulation
                self._cleanup_old_scorecard_images(temp_dir)
                
                # Try to remove temp directory if empty
                self._cleanup_empty_temp_directory(temp_dir)
                
            else:
                self.logger.debug("Scorecard temp directory does not exist")
                
        except Exception as phase3_error:
            self.logger.error(f"❌ Phase 3 cleanup failed: {phase3_error}")
            cleanup_success = False

        # ========== PHASE 4: FINAL VALIDATION & REPORTING ==========
        try:
            self.logger.debug("🧹 Phase 4: Final validation and reporting")
            
            # Check if main directory still exists
            if self.archive_path.exists():
                remaining_items = list(self.archive_path.iterdir())
                self.logger.warning(f"⚠️ Archive directory still exists with {len(remaining_items)} items: {[item.name for item in remaining_items]}")
                cleanup_success = False
            
            # Report overall status
            if cleanup_success:
                self.logger.info(f"🎉 Complete cleanup successful for match {self.match_id}")
            else:
                self.logger.warning(f"⚠️ Partial cleanup completed with some issues for match {self.match_id}")
                
        except Exception as phase4_error:
            self.logger.error(f"❌ Phase 4 validation failed: {phase4_error}")

    def _force_remove_directory(self, directory_path: Path) -> None:
        """
        Force remove directory by changing permissions and retrying.
        Handles Windows and Unix permission issues.
        """
        import stat
        
        def handle_remove_readonly(func, path, exc):
            """Error handler for permission issues"""
            if os.path.exists(path):
                # Change permissions and retry
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                func(path)
        
        try:
            # Try with error handler for read-only files
            shutil.rmtree(directory_path, onerror=handle_remove_readonly)
            
        except Exception as e:
            self.logger.debug(f"Force removal attempt failed: {e}")
            raise

    def _remove_directory_contents_individually(self, directory_path: Path) -> None:
        """
        Remove directory contents file by file, then remove empty directories.
        Last resort cleanup method.
        """
        if not directory_path.exists():
            return
        
        files_removed = 0
        dirs_removed = 0
        
        # First pass: Remove all files
        for item in directory_path.rglob('*'):
            if item.is_file():
                try:
                    item.unlink()
                    files_removed += 1
                    self.logger.debug(f"Individually removed file: {item.name}")
                except Exception as e:
                    self.logger.debug(f"Failed to remove file {item.name}: {e}")
                    # Try force removal for this file
                    try:
                        import stat
                        item.chmod(stat.S_IWRITE)
                        item.unlink()
                        files_removed += 1
                        self.logger.debug(f"Force removed file: {item.name}")
                    except Exception as force_e:
                        self.logger.warning(f"Could not force remove file {item.name}: {force_e}")
        
        # Second pass: Remove empty directories (bottom-up)
        for item in sorted(directory_path.rglob('*'), key=lambda p: len(str(p)), reverse=True):
            if item.is_dir() and item != directory_path:
                try:
                    item.rmdir()  # Only works if empty
                    dirs_removed += 1
                    self.logger.debug(f"Removed empty directory: {item.name}")
                except OSError:
                    pass  # Directory not empty, skip
        
        # Finally remove the main directory
        try:
            directory_path.rmdir()
            dirs_removed += 1
            self.logger.debug(f"Removed main directory: {directory_path.name}")
        except OSError as e:
            remaining = list(directory_path.iterdir()) if directory_path.exists() else []
            self.logger.warning(f"Could not remove main directory: {e}. Remaining items: {[item.name for item in remaining]}")
        
        self.logger.debug(f"Individual cleanup: {files_removed} files, {dirs_removed} directories removed")

    def _cleanup_old_scorecard_images(self, temp_dir: Path) -> None:
        """Clean up scorecard images older than 2 hours to prevent accumulation"""
        try:
            import time
            current_time = time.time()
            two_hours_ago = current_time - 7200  # 2 hours in seconds
            
            old_files_removed = 0
            for file_path in temp_dir.glob("*.png"):
                try:
                    if file_path.is_file():
                        file_age = file_path.stat().st_mtime
                        if file_age < two_hours_ago:
                            file_path.unlink()
                            old_files_removed += 1
                            self.logger.debug(f"Removed old scorecard image: {file_path.name}")
                except Exception as old_cleanup_error:
                    self.logger.debug(f"Error removing old file {file_path.name}: {old_cleanup_error}")
            
            if old_files_removed > 0:
                self.logger.debug(f"Cleaned up {old_files_removed} old scorecard images")
                
        except Exception as e:
            self.logger.debug(f"Error during old image cleanup: {e}")

    def _cleanup_empty_temp_directory(self, temp_dir: Path) -> None:
        """Remove temp directory if it's empty"""
        try:
            if temp_dir.exists():
                contents = list(temp_dir.iterdir())
                if not contents:
                    temp_dir.rmdir()
                    self.logger.debug(f"✅ Removed empty temp directory: {temp_dir}")
                else:
                    self.logger.debug(f"Temp directory not empty, contains: {[item.name for item in contents]}")
        except Exception as e:
            self.logger.debug(f"Could not remove temp directory: {e}")


    def _cleanup_on_error(self) -> None:
        """Clean up files created before error occurred"""
        try:
            self._cleanup_temporary_files()
            
            # Also remove any partial ZIP file
            zip_path = Path("data") / self.filenames['zip']
            if zip_path.exists():
                zip_path.unlink()
                self.logger.debug("Removed partial ZIP file after error")
                
        except Exception as e:
            self.logger.warning(f"Error cleanup failed (non-critical): {e}")

    def get_archive_info(self) -> Dict[str, Any]:
        """Get information about the created archive"""
        zip_path = Path("data") / self.filenames['zip']
        
        if not zip_path.exists():
            return {"error": "Archive not found"}
        
        try:
            with zipfile.ZipFile(zip_path, 'r') as zipf:
                files_info = []
                total_size = 0
                
                for file_info in zipf.filelist:
                    files_info.append({
                        "filename": file_info.filename,
                        "size": file_info.file_size,
                        "compressed_size": file_info.compress_size,
                        "compression_ratio": f"{(1 - file_info.compress_size / file_info.file_size) * 100:.1f}%" if file_info.file_size > 0 else "0%"
                    })
                    total_size += file_info.file_size
                
                return {
                    "zip_path": str(zip_path),
                    "zip_size": os.path.getsize(zip_path),
                    "total_uncompressed_size": total_size,
                    "file_count": len(files_info),
                    "files": files_info,
                    "created": datetime.fromtimestamp(os.path.getctime(zip_path)).isoformat(),
                    "match_id": self.match_id,
                    "teams": f"{self.team_home} vs {self.team_away}"
                }
                
        except Exception as e:
            return {"error": f"Failed to read archive info: {e}"}


def find_original_json_file(match_id: str, base_path: str = "data/matches") -> Optional[str]:
    """
    Find the original JSON file for a given match_id with enhanced error handling
    
    Args:
        match_id: The match identifier to search for
        base_path: Directory to search in (default: "data/matches")
        
    Returns:
        str: Path to the JSON file if found, None otherwise
    """
    logger = logging.getLogger(__name__)
    
    try:
        base_path = Path(base_path)
        
        if not base_path.exists():
            logger.warning(f"Base path does not exist: {base_path}")
            return None
        
        if not base_path.is_dir():
            logger.warning(f"Base path is not a directory: {base_path}")
            return None
        
        # Search for JSON files
        json_files = list(base_path.glob("*.json"))
        
        for json_file in json_files:
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                if data.get('match_id') == match_id:
                    logger.debug(f"Found match file: {json_file}")
                    return str(json_file)
                    
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Error reading {json_file}: {e}")
                continue
        
        logger.warning(f"No JSON file found for match_id: {match_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error searching for match file: {e}")
        return None


# Production-level utility functions
def validate_archive_environment() -> Dict[str, Any]:
    """Validate the environment is ready for archiving"""
    logger = logging.getLogger(__name__)
    issues = []
    
    # Check data directory
    data_dir = Path("data")
    if not data_dir.exists():
        issues.append("Data directory does not exist")
    elif not os.access(data_dir, os.W_OK):
        issues.append("Data directory is not writable")
    
    # Check required modules
    required_modules = ['json', 'csv', 'zipfile', 'tabulate']
    for module in required_modules:
        try:
            __import__(module)
        except ImportError:
            issues.append(f"Required module missing: {module}")
    
    # Check disk space (if more than 100MB available)
    try:
        statvfs = os.statvfs(data_dir)
        free_space = statvfs.f_frsize * statvfs.f_bavail
        if free_space < 100 * 1024 * 1024:  # Less than 100MB
            issues.append(f"Low disk space: {free_space / 1024 / 1024:.1f}MB available")
    except (AttributeError, OSError):
        # os.statvfs not available on Windows
        pass
    
    return {
        "ready": len(issues) == 0,
        "issues": issues,
        "data_directory": str(data_dir.absolute()),
        "timestamp": datetime.now().isoformat()
    }