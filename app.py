# -*- coding: utf-8 -*-
"""SimCricketX Flask Application"""

# CRITICAL: Fix Windows console encoding BEFORE any other imports
import sys
import io
import os

# Force UTF-8 encoding for all I/O operations on Windows
if sys.platform == "win32":
    # Ensure stdout and stderr use UTF-8
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'buffer'):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    
    # Set environment variables for UTF-8
    os.environ['PYTHONIOENCODING'] = 'utf-8'

# Now import everything else
import json
import re
import logging
import yaml
import uuid
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from utils.helpers import load_config
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, send_from_directory, send_file, flash
from match_archiver import MatchArchiver, find_original_json_file
from engine.match import Match
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    login_required,
    logout_user,
    current_user
)
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from auth.user_auth import (
    register_user,
    verify_user,
    delete_user
)
from engine.team import Team, save_team, PITCH_PREFERENCES
from engine.player import Player, PLAYER_ROLES, BATTING_HANDS, BOWLING_TYPES, BOWLING_HANDS
import random
import shutil
import time
import threading
import traceback

from werkzeug.utils import secure_filename
from engine.stats_aggregator import StatsAggregator 
from engine.stats_service import StatsService
import glob
import pandas as pd 
from tabulate import tabulate
from flask import Response
from sqlalchemy.orm import joinedload, aliased

# Add this import for system monitoring
try:
    import psutil
except ImportError:
    psutil = None

from database import db
from database.models import User as DBUser, Team as DBTeam, Player as DBPlayer, Tournament, TournamentTeam, TournamentFixture
from database.models import Match as DBMatch, MatchScorecard, TournamentPlayerStatsCache, MatchPartnership # Distinct from engine.match.Match
from engine.tournament_engine import TournamentEngine
from sqlalchemy import func  # For aggregate functions




MATCH_INSTANCES = {}
tournament_engine = TournamentEngine()

# How old is "too old"? 7 days -> 7*24*3600 seconds
PROD_MAX_AGE = 7 * 24 * 3600

# Make sure PROJECT_ROOT is defined near the top of app.py:
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
VISIT_FILE = os.path.join(PROJECT_ROOT, "data", "visit_counter.txt")
MATCHES_FILE = os.path.join(PROJECT_ROOT, "data", "matches_simulated.txt")

def get_matches_simulated():
    try:
        with open(MATCHES_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return 0

def increment_matches_simulated():
    current = get_matches_simulated()
    try:
        with open(MATCHES_FILE, "w") as f:
            f.write(str(current + 1))
    except Exception as e:
        print(f"[ERROR] Failed to write matches_simulated: {e}")

def get_visit_counter():
    try:
        with open(VISIT_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return 0

def increment_visit_counter():
    count = get_visit_counter() + 1
    try:
        with open(VISIT_FILE, "w") as f:
            f.write(str(count))
    except Exception as e:
        print(f"[ERROR] Could not write visit count: {e}")


def clean_old_archives(max_age_seconds=PROD_MAX_AGE):
    """
    Walk through PROJECT_ROOT/data/, find any .zip files,
    and delete those whose modification time is older than max_age_seconds.
    """
    data_dir = os.path.join(PROJECT_ROOT, "data")
    now = time.time()

    if not os.path.isdir(data_dir):
        app.logger.warning(f"clean_old_archives: data directory does not exist: {data_dir}")
        return

    for filename in os.listdir(data_dir):
        if not filename.lower().endswith(".zip"):
            continue

        full_path = os.path.join(data_dir, filename)
        if not os.path.isfile(full_path):
            continue

        age = now - os.path.getmtime(full_path)
        if age > max_age_seconds:
            try:
                os.remove(full_path)
                app.logger.info(f"Deleted old archive: {filename} (age {age//3600}h)")
            except Exception as e:
                app.logger.error(f"Failed to delete {full_path}: {e}", exc_info=True)


def load_match_metadata(match_id):
    """
    Look in data/matches for a JSON whose "match_id" field equals match_id.
    Return the parsed dict if found, else None.
    """
    matches_dir = os.path.join(PROJECT_ROOT, "data", "matches")
    for fn in os.listdir(matches_dir):
        if not fn.lower().endswith(".json"):
            continue
        path = os.path.join(matches_dir, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("match_id") == match_id:
                return d
        except Exception:
            continue
    return None

def load_app_config():
    base_dir = os.path.abspath(os.path.dirname(__file__))
    config_path = os.path.join(base_dir, "config", "config.yaml")
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

def cleanup_old_match_instances(app):
    """Clean up old match instances from memory to prevent memory leaks"""
    try:
        current_time = time.time()
        cutoff_time = current_time - (7 * 24 * 3600)  # 24 hours ago
        
        instances_to_remove = []
        for match_id, instance in MATCH_INSTANCES.items():
            # Check if instance has a timestamp or creation time
            instance_time = getattr(instance, 'created_at', current_time)
            if instance_time < cutoff_time:
                instances_to_remove.append(match_id)
        
        for match_id in instances_to_remove:
            del MATCH_INSTANCES[match_id]
            app.logger.info(f"[Cleanup] Removed old match instance: {match_id}")
        
        if instances_to_remove:
            app.logger.info(f"[Cleanup] Cleaned up {len(instances_to_remove)} old match instances")
            
    except Exception as e:
        app.logger.error(f"[Cleanup] Error cleaning up match instances: {e}", exc_info=True)

def periodic_cleanup(app):
    """Run cleanup every 6 hours"""
    while True:
        try:
            time.sleep(6 * 3600)  # 6 hours
            cleanup_old_match_instances(app)
        except Exception as e:
            app.logger.error(f"[PeriodicCleanup] Error in cleanup thread: {e}")


def cleanup_temp_scorecard_images():
    """
    Clean up temporary scorecard images folder before starting a new match.
    Removes the entire temp_scorecard_images folder if it exists.
    """
    temp_images_dir = os.path.join(PROJECT_ROOT, "data", "temp_scorecard_images")
    
    try:
        if os.path.exists(temp_images_dir) and os.path.isdir(temp_images_dir):
            shutil.rmtree(temp_images_dir)
            app.logger.info(f"[Cleanup] Removed temp scorecard images directory: {temp_images_dir}")
        else:
            app.logger.debug(f"[Cleanup] Temp scorecard images directory does not exist: {temp_images_dir}")
    except Exception as e:
        app.logger.error(f"[Cleanup] Error removing temp scorecard images directory: {e}", exc_info=True)



def _safe_get_attr(obj, attr, default=None):
    """
    Safely get an attribute from an object, returning default if:
    - Attribute doesn't exist
    - Attribute value is None

    This is more robust than hasattr() which returns True for None values.
    """
    value = getattr(obj, attr, None)
    return value if value is not None else default


# ?????? App Factory ??????
def create_app():
    # --- Flask setup ---
    app = Flask(__name__)
    config = load_config()

    @app.before_request
    def configure_session_cookie():
        is_secure = request.is_secure or (request.headers.get('X-Forwarded-Proto') == 'https')
        app.config["SESSION_COOKIE_SECURE"] = is_secure
        app.logger.info(f"[Session] Setting SESSION_COOKIE_SECURE to {is_secure} (HTTPS: {request.scheme}, X-Forwarded-Proto: {request.headers.get('X-Forwarded-Proto')})")

    # --- Secret key setup ---
    secret = None
    try:
        secret = config.get("app", {}).get("secret_key", None)
        if not secret or not isinstance(secret, str):
            raise ValueError("Invalid secret_key in config")
    except Exception as e:
        print(f"[WARN] Could not read secret_key from config.yaml: {e}")

    if secret and secret.strip().lower() in {"change_me", "replace_me", "default", "your_secret_here"}:
        print("[WARN] secret_key is a placeholder; ignoring config value")
        secret = None

    if not secret:
        secret = os.getenv("FLASK_SECRET_KEY", None)
        if not secret:
            secret_file = os.path.join(PROJECT_ROOT, "data", "secret_key.txt")
            try:
                if os.path.exists(secret_file):
                    with open(secret_file, "r", encoding="utf-8") as f:
                        secret = f.read().strip()
                if not secret:
                    os.makedirs(os.path.dirname(secret_file), exist_ok=True)
                    secret = os.urandom(24).hex()
                    with open(secret_file, "w", encoding="utf-8") as f:
                        f.write(secret)
                    print(f"[WARN] Generated persistent SECRET_KEY at {secret_file}")
            except Exception as e:
                print(f"[WARN] Failed to load/write persistent SECRET_KEY: {e}")
                secret = os.urandom(24).hex()
                print("[WARN] Using random Flask SECRET_KEY--sessions won't persist across restarts")

    app.config["SECRET_KEY"] = secret
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)
    app.config["REMEMBER_COOKIE_REFRESH_EACH_REQUEST"] = True

    # --- CSRF Protection ---
    csrf = CSRFProtect(app)

    # --- Rate Limiting ---
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[],
        storage_uri="memory://",
    )

    # --- Database setup ---
    basedir = os.path.abspath(os.path.dirname(__file__))
    db_path = os.path.join(basedir, 'cricket_sim.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    db.init_app(app)

    # Lightweight, idempotent schema guard (covers sqlite drift in dev)
    try:
        from scripts.fix_db_schema import ensure_schema
        with app.app_context():
            ensure_schema(db.engine)
    except Exception as e:
        print(f"[WARN] Schema check skipped: {e}")

    # --- Logging setup (logs to file + terminal) ---
    base_dir = os.path.abspath(os.path.dirname(__file__))
    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "execution.log")

    # Clear existing handlers to avoid duplicates
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    # File handler for persistent logging
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)

    # Console handler for terminal visibility using stderr
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.DEBUG)

    # Formatter for both
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # Setup logging globally
    logging.basicConfig(level=logging.DEBUG, handlers=[file_handler, console_handler])

    # Attach logger to app
    app.logger = logging.getLogger("SimCricketX")
    app.logger.setLevel(logging.DEBUG)  # You can change to INFO for production

    # --- Flask-Login setup ---
    login_manager = LoginManager(app)
    login_manager.login_view = "login"

    # --- NEW: Statistics Feature Setup ---
    UPLOAD_FOLDER = 'uploads'
    ARCHIVES_FOLDER = os.path.join(PROJECT_ROOT, "data")
    ALLOWED_EXTENSIONS = {'csv'}
    app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
    app.config['ARCHIVES_FOLDER'] = ARCHIVES_FOLDER

    # Ensure upload and stats directories exist
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    if not os.path.exists('data/stats'):
        os.makedirs('data/stats')

    def allowed_file(filename):
        return '.' in filename and \
               filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
    # --- END NEW ---

    def _is_valid_match_id(match_id: str) -> bool:
        try:
            return str(uuid.UUID(match_id)).lower() == match_id.lower()
        except Exception:
            return False

    def _load_match_file_for_user(match_id):
        match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
        if not os.path.isdir(match_dir):
            return None, None, (jsonify({"error": "Match not found"}), 404)

        for fn in os.listdir(match_dir):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(match_dir, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("match_id") == match_id:
                    if data.get("created_by") != current_user.id:
                        return None, None, (jsonify({"error": "Unauthorized"}), 403)
                    return data, path, None
            except Exception as e:
                app.logger.error(f"[MatchAuth] error loading {fn}: {e}", exc_info=True)
                continue

        return None, None, (jsonify({"error": "Match not found"}), 404)

    @login_manager.user_loader
    def load_user(email):
        # Use DBUser (User model) directly
        return db.session.get(DBUser, email)

    # --- Request logging ---
    @app.before_request
    def log_request():
        app.logger.info(f"{request.remote_addr} {request.method} {request.path}")

    # --- Tournament Match Completion Handler ---
    def _handle_tournament_match_completion(match, match_id, outcome, logger):
        """
        Handle tournament match completion with proper transaction management.
        
        This function follows SOLID principles:
        - Single Responsibility: Handles only tournament match completion
        - Open/Closed: Extensible through tournament_engine methods
        - Dependency Inversion: Depends on abstractions (logger, db.session)
        
        Args:
            match: Match instance with completed game data
            match_id: Unique identifier for the match
            outcome: Match outcome dictionary from engine
            logger: Logger instance for tracking operations
            
        Raises:
            ValueError: If required tournament data is missing
            Exception: For any database or processing errors
        """
        try:
            # Step 1: Validate tournament context
            tournament_id = match.data.get("tournament_id")
            fixture_id = match.data.get("fixture_id")
            
            if not tournament_id:
                logger.error(f"[Tournament] Match {match_id} missing tournament_id")
                return
                
            if not fixture_id:
                logger.error(f"[Tournament] Match {match_id} missing fixture_id")
                return
            
            tournament_id = int(tournament_id)
            try:
                fixture_id = int(fixture_id)
            except Exception:
                raise ValueError(f"Invalid fixture_id '{fixture_id}' for match {match_id}")
            logger.info(f"[Tournament] Starting completion handler for match {match_id} in tournament {tournament_id}")
            
            # Step 2: Begin explicit transaction (not nested)
            # Using manual transaction control for better error handling
            try:
                # Step 2a: Handle resimulation - reverse previous standings if match exists
                existing_match = db.session.get(DBMatch, match_id)
                if existing_match:
                    logger.info(f"[Tournament] Existing match {match_id} found. Reversing previous standings.")
                    tournament_engine.reverse_standings(existing_match, commit=False)
                    # Clean up dependent rows to avoid FK nulling issues (sqlite NOT NULL)
                    try:
                        db.session.query(MatchPartnership).filter_by(match_id=match_id).delete(synchronize_session=False)
                        db.session.query(MatchScorecard).filter_by(match_id=match_id).delete(synchronize_session=False)
                    except Exception as cleanup_err:
                        logger.error(f"[Tournament] Failed to clean dependent rows for match {match_id}: {cleanup_err}", exc_info=True)
                        raise
                    db.session.delete(existing_match)
                    db.session.flush()
                    logger.info(f"[Tournament] Previous match data cleared for {match_id}")
                
                # Step 3: Validate fixture exists and is accessible
                fixture = db.session.get(TournamentFixture, fixture_id)
                if not fixture:
                    raise ValueError(f"Tournament fixture {fixture_id} not found; cannot proceed safely.")
                
                if not fixture.home_team_id or not fixture.away_team_id:
                    raise ValueError(f"Fixture {fixture_id} missing team assignments")
                
                logger.info(f"[Tournament] Validated fixture {fixture_id}: {fixture.home_team.name} vs {fixture.away_team.name}")
                
                # Step 4: Prepare match result data
                final_result = outcome.get("result", "Match Ended")
                match.data["result_description"] = final_result
                match.data["current_state"] = "completed"
                
                # Step 5: Create database match record
                db_match = DBMatch(
                    id=match_id,
                    user_id=current_user.id,
                    tournament_id=tournament_id,
                    home_team_id=fixture.home_team_id,
                    away_team_id=fixture.away_team_id,
                    match_json_path="autosaved",
                    result_description=final_result,
                    date=datetime.now()
                )
                
                logger.info(f"[Tournament] Created DBMatch record for {match_id}")
                
                # Step 6: Calculate and assign innings statistics
                team_home_code = match.data["team_home"].split("_")[0]
                first_bat_is_home = (
                    (match.toss_winner == team_home_code and match.toss_decision == "Bat") or
                    (match.toss_winner != team_home_code and match.toss_decision == "Bowl")
                )
                
                def calculate_innings_stats(batting_stats, bowling_stats):
                    """Calculate runs, wickets, and overs from stats dictionaries."""
                    runs = sum(p.get("runs", 0) for p in batting_stats.values())
                    wickets = sum(1 for p in batting_stats.values() if p.get("wicket_type"))
                    balls_total = sum(b.get("balls_bowled", 0) for b in bowling_stats.values())
                    overs = (balls_total // 6) + (balls_total % 6) / 10.0
                    return runs, wickets, overs
                
                # First innings stats
                s1_runs, s1_wickets, s1_overs = calculate_innings_stats(
                    match.first_innings_batting_stats,
                    match.first_innings_bowling_stats
                )
                
                # Second innings stats
                s2_runs, s2_wickets, s2_overs = calculate_innings_stats(
                    match.batsman_stats,
                    match.bowler_stats
                )
                
                # Assign scores based on batting order
                if first_bat_is_home:
                    db_match.home_team_score = s1_runs
                    db_match.home_team_wickets = s1_wickets
                    db_match.home_team_overs = s1_overs
                    db_match.away_team_score = s2_runs
                    db_match.away_team_wickets = s2_wickets
                    db_match.away_team_overs = s2_overs
                else:
                    db_match.away_team_score = s1_runs
                    db_match.away_team_wickets = s1_wickets
                    db_match.away_team_overs = s1_overs
                    db_match.home_team_score = s2_runs
                    db_match.home_team_wickets = s2_wickets
                    db_match.home_team_overs = s2_overs
                
                logger.info(
                    f"[Tournament] Match scores - Home: {db_match.home_team_score}/{db_match.home_team_wickets} "
                    f"({db_match.home_team_overs}), Away: {db_match.away_team_score}/{db_match.away_team_wickets} "
                    f"({db_match.away_team_overs})"
                )
                
                # Step 7: Resolve winner team ID
                def _extract_winner_from_text(result_text):
                    if not result_text:
                        return None
                    lower = result_text.lower()
                    if "match tied" in lower or "no result" in lower or "abandoned" in lower:
                        return None
                    match_obj = re.search(r"\b([A-Za-z0-9 _-]+?)\s+won\b", result_text, re.IGNORECASE)
                    if match_obj:
                        return match_obj.group(1).strip()
                    return None

                def _team_hit(candidate, team_name, team_code):
                    if not candidate:
                        return False
                    if team_name and team_name == candidate:
                        return True
                    if team_code and team_code == candidate:
                        return True
                    if team_name and re.search(rf"\\b{re.escape(team_name)}\\b", candidate):
                        return True
                    if team_code and re.search(rf"\\b{re.escape(team_code)}\\b", candidate):
                        return True
                    return False

                winner_name = getattr(match, 'winner', None) or outcome.get("winner")
                if not winner_name:
                    winner_name = _extract_winner_from_text(final_result)

                if winner_name:
                    normalized = _extract_winner_from_text(winner_name) or winner_name
                    winner_name_lower = normalized.lower().strip()
                    home_name = (fixture.home_team.name or '').lower().strip()
                    home_code = (fixture.home_team.short_code or '').lower().strip()
                    away_name = (fixture.away_team.name or '').lower().strip()
                    away_code = (fixture.away_team.short_code or '').lower().strip()

                    home_hit = _team_hit(winner_name_lower, home_name, home_code)
                    away_hit = _team_hit(winner_name_lower, away_name, away_code)

                    if home_hit and not away_hit:
                        db_match.winner_team_id = fixture.home_team_id
                        logger.info(f"[Tournament] Winner: {fixture.home_team.name} (Home)")
                    elif away_hit and not home_hit:
                        db_match.winner_team_id = fixture.away_team_id
                        logger.info(f"[Tournament] Winner: {fixture.away_team.name} (Away)")
                    else:
                        logger.warning(
                            f"[Tournament] Could not match winner '{winner_name}' to teams. "
                            f"Home: {home_name}/{home_code}, Away: {away_name}/{away_code}"
                        )
                else:
                    logger.info(f"[Tournament] Match ended without clear winner (tie/no result)")
                
                # Step 8: Add match to session
                db.session.add(db_match)
                db.session.flush()  # Ensure match is persisted before scorecard
                logger.info(f"[Tournament] DBMatch added to session for {match_id}")
                
                # Step 9: Save detailed scorecard data
                scorecard_data = outcome.get("scorecard_data")
                if scorecard_data:
                    logger.info(f"[Tournament] Saving detailed scorecard for match {match_id}")
                    try:
                        # Fix: MatchArchiver takes (match_data, match_instance)
                        # and created_by should be in match_data
                        archiver = MatchArchiver(match.data, match)
                        archiver._save_to_database()
                        logger.info(f"[Tournament] Scorecard saved successfully for {match_id}")
                    except Exception as scorecard_err:
                        logger.error(f"[Tournament] Scorecard save failed: {scorecard_err}", exc_info=True)
                        # Continue - scorecard is supplementary data
                
                # Step 10: Update fixture status and link to match
                fixture.match_id = match_id
                fixture.status = 'Completed'
                fixture.winner_team_id = db_match.winner_team_id
                logger.info(f"[Tournament] Fixture {fixture_id} marked as Completed")
                
                # Step 11: Update tournament standings (critical operation)
                logger.info(f"[Tournament] Updating standings for tournament {tournament_id}")
                standings_updated = tournament_engine.update_standings(db_match, commit=False)
                
                if standings_updated:
                    logger.info(f"[Tournament] Standings updated successfully for tournament {tournament_id}")
                else:
                    logger.warning(f"[Tournament] Standings update returned False for match {match_id}")
                
                # Step 12: Commit all changes atomically
                db.session.commit()
                logger.info(
                    f"[Tournament] ✓ Match {match_id} completed successfully. "
                    f"Tournament {tournament_id} standings updated."
                )
                
            except ValueError as ve:
                # Validation errors - log and rollback
                logger.error(f"[Tournament] Validation error for match {match_id}: {ve}", exc_info=True)
                db.session.rollback()
                raise
                
            except Exception as db_err:
                # Database or processing errors - rollback and log
                logger.error(
                    f"[Tournament] Database error during match {match_id} completion: {db_err}",
                    exc_info=True
                )
                db.session.rollback()
                raise
                
        except Exception as outer_err:
            # Catch-all for any unexpected errors
            logger.error(
                f"[Tournament] Critical error in match completion handler for {match_id}: {outer_err}",
                exc_info=True
            )
            # Ensure rollback even if inner try didn't catch it
            try:
                db.session.rollback()
            except:
                pass

    def _compute_tournament_stats(user_id, tournament_id, use_cache=False):
        """
        Aggregate batting/bowling/fielding stats from match_scorecards for a given tournament.
        """
        q = (
            db.session.query(MatchScorecard, DBMatch, DBPlayer, DBTeam)
            .join(DBMatch, MatchScorecard.match_id == DBMatch.id)
            .join(DBPlayer, MatchScorecard.player_id == DBPlayer.id)
            .join(DBTeam, DBPlayer.team_id == DBTeam.id)
            .filter(DBMatch.tournament_id == tournament_id)
            .filter(DBTeam.user_id == user_id)
        )

        records = q.all()
        app.logger.info(f"STATS DEBUG: User {user_id} Tournament {tournament_id} - Found {len(records)} records")
        if not records:
            try:
                total = MatchScorecard.query.join(DBMatch).filter(DBMatch.tournament_id == tournament_id).count()
                app.logger.info(f"STATS DEBUG: Total scorecards for tournament {tournament_id} (ignoring user filter): {total}")
            except Exception as e:
                app.logger.error(f"STATS DEBUG: Error checking total: {e}")
            return [], [], [], {}

        agg = {}
        match_sets = {}

        for card, match, player, team in records:
            pid = player.id
            if pid not in agg:
                agg[pid] = {
                    "player": player.name,
                    "team": team.name,
                    "bat_runs": 0,
                    "bat_balls": 0,
                    "bat_fours": 0,
                    "bat_sixes": 0,
                    "bat_innings": 0,
                    "bat_not_outs": 0,
                    "bowl_balls": 0,
                    "bowl_runs": 0,
                    "bowl_wkts": 0,
                    "bowl_maidens": 0,
                    "bowl_best": (0, 9999),  # (wkts, runs)
                    "bowl_innings": 0,
                    "catches": 0,
                    "run_outs": 0,
                }
                match_sets[pid] = set()

            match_sets[pid].add(card.match_id)

            if card.record_type == "batting":
                faced = (card.balls or 0) > 0 or (card.runs or 0) > 0 or bool(card.is_out)
                if faced:
                    agg[pid]["bat_innings"] += 1
                    if not card.is_out:
                        agg[pid]["bat_not_outs"] += 1
                agg[pid]["bat_runs"] += card.runs or 0
                agg[pid]["bat_balls"] += card.balls or 0
                agg[pid]["bat_fours"] += card.fours or 0
                agg[pid]["bat_sixes"] += card.sixes or 0

            if card.record_type == "bowling":
                balls = card.balls_bowled or 0
                if balls > 0 or (card.overs or 0) > 0:
                    agg[pid]["bowl_innings"] += 1
                agg[pid]["bowl_balls"] += balls
                agg[pid]["bowl_runs"] += card.runs_conceded or 0
                agg[pid]["bowl_wkts"] += card.wickets or 0
                agg[pid]["bowl_maidens"] += card.maidens or 0
                # best figure: higher wickets, then lower runs
                best_w, best_r = agg[pid]["bowl_best"]
                if (card.wickets or 0) > best_w or ((card.wickets or 0) == best_w and (card.runs_conceded or 0) < best_r):
                    agg[pid]["bowl_best"] = (card.wickets or 0, card.runs_conceded or 0)

            agg[pid]["catches"] += card.catches or 0
            agg[pid]["run_outs"] += card.run_outs or 0

        batting_stats = []
        bowling_stats = []
        fielding_stats = []

        for pid, d in agg.items():
            matches_played = len(match_sets[pid])

            # Batting
            if matches_played > 0:
                outs = max(d["bat_innings"] - d["bat_not_outs"], 0)
                bat_avg = d["bat_runs"] / outs if outs > 0 else d["bat_runs"]
                sr = (d["bat_runs"] * 100 / d["bat_balls"]) if d["bat_balls"] > 0 else 0
                batting_stats.append({
                    "Player": d["player"],
                    "Team": d["team"],
                    "Matches": matches_played,
                    "Innings": d["bat_innings"],
                    "Runs": d["bat_runs"],
                    "Balls": d["bat_balls"],
                    "Average": round(bat_avg, 2),
                    "Strike Rate": round(sr, 2),
                    "6s": d["bat_sixes"],
                    "4s": d["bat_fours"],
                    "Not Outs": d["bat_not_outs"],
                })

            # Bowling
            if matches_played > 0 and (d["bowl_balls"] > 0 or d["bowl_wkts"] > 0):
                overs_float = (d["bowl_balls"] // 6) + (d["bowl_balls"] % 6) / 10.0
                econ = d["bowl_runs"] / (d["bowl_balls"] / 6) if d["bowl_balls"] > 0 else 0
                bowl_avg = d["bowl_runs"] / d["bowl_wkts"] if d["bowl_wkts"] > 0 else 0
                best_w, best_r = d["bowl_best"]
                bowling_stats.append({
                    "Player": d["player"],
                    "Team": d["team"],
                    "Matches": matches_played,
                    "Innings": d["bowl_innings"],
                    "Wickets": d["bowl_wkts"],
                    "Overs": overs_float,
                    "Economy": round(econ, 2),
                    "Average": round(bowl_avg, 2) if bowl_avg else 0,
                    "Best": f"{best_w}/{best_r}" if best_w or best_r < 9999 else "-",
                })

            # Fielding
            if matches_played > 0:
                fielding_stats.append({
                    "Player": d["player"],
                    "Team": d["team"],
                    "Matches": matches_played,
                    "Innings": matches_played,  # fielded if in match
                    "Catches": d["catches"],
                    "Run Outs": d["run_outs"],
                    "Stumpings": 0,  # not tracked yet
                })

        # Leaderboards
        # Use StatsService for bowling figures
        stats_service = StatsService(app.logger)
        bowling_figures = stats_service.get_bowling_figures_leaderboard(
            user_id, 
            tournament_id, 
            limit=5
        )
        
        leaderboards = {
            "top_run_scorers": sorted(batting_stats, key=lambda x: x["Runs"], reverse=True)[:5],
            "top_wicket_takers": sorted(bowling_stats, key=lambda x: x["Wickets"], reverse=True)[:5],
            "most_catches": sorted(fielding_stats, key=lambda x: x["Catches"], reverse=True)[:5],
            "best_strikers": sorted([p for p in batting_stats if p["Balls"] > 20], key=lambda x: x["Strike Rate"], reverse=True)[:5],
            "best_economy": sorted([p for p in bowling_stats if p["Overs"] > 5], key=lambda x: x["Economy"])[:5],
            "best_bowling_figures": bowling_figures[:5],  # NEW: Best bowling figures
        }

        return batting_stats, bowling_stats, fielding_stats, leaderboards


    def _render_statistics_page(user_id):
        try:
            tournaments = Tournament.query.filter_by(user_id=user_id).all()
            selected_tid = request.args.get("tournament_id", type=int)

            if not tournaments:
                return render_template("statistics.html", has_stats=False, user=current_user)

            if not selected_tid:
                return render_template("statistics.html", has_stats=False, user=current_user, tournaments=tournaments, selected_tid=None)

            batting_stats, bowling_stats, fielding_stats, leaderboards = _compute_tournament_stats(user_id, selected_tid)

            if not batting_stats and not bowling_stats and not fielding_stats:
                return render_template("statistics.html", has_stats=False, user=current_user, tournaments=tournaments, selected_tid=selected_tid)

            batting_headers = ['Player', 'Team', 'Matches', 'Innings', 'Runs', 'Balls', 'Average', 'Strike Rate', '6s', '4s', 'Not Outs']
            bowling_headers = ['Player', 'Team', 'Matches', 'Innings', 'Wickets', 'Overs', 'Economy', 'Average', 'Best']
            fielding_headers = ['Player', 'Team', 'Matches', 'Innings', 'Catches', 'Run Outs', 'Stumpings']

            return render_template(
                "statistics.html",
                has_stats=True,
                user=current_user,
                tournaments=tournaments,
                selected_tid=selected_tid,
                batting_stats=batting_stats,
                bowling_stats=bowling_stats,
                fielding_stats=fielding_stats,
                batting_headers=batting_headers,
                bowling_headers=bowling_headers,
                fielding_headers=fielding_headers,
                leaderboards=leaderboards,
                batting_filename=f"tournament_{selected_tid}_batting",
                bowling_filename=f"tournament_{selected_tid}_bowling"
            )
        except Exception as e:
            app.logger.error(f"Error in _render_statistics_page for user {user_id}: {e}", exc_info=True)
            return render_template("statistics.html", has_stats=False, user=current_user)

    # ????? Routes ?????

    @app.route("/")
    @login_required
    def home():
        if not session.get("visit_counted"):
            increment_visit_counter()
            session["visit_counted"] = True
            
        return render_template("home.html", user=current_user, total_visits=get_visit_counter(), matches_simulated=get_matches_simulated())

    @app.route("/register", methods=["GET", "POST"])
    @limiter.limit("5 per minute", methods=["POST"])
    def register():
        """
        Simplified registration route
        """
        try:
            if request.method == "GET":
                return render_template("register.html")
            
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            
            if not email or "@" not in email or "." not in email:
                return render_template("register.html", error="Invalid email")
            
            if not password:
                return render_template("register.html", error="Password required")

            if len(password) < 8:
                return render_template("register.html", error="Password must be at least 8 characters")

            if not re.search(r'[A-Z]', password):
                return render_template("register.html", error="Password must contain at least one uppercase letter")

            if not re.search(r'[a-z]', password):
                return render_template("register.html", error="Password must contain at least one lowercase letter")

            if not re.search(r'[0-9]', password):
                return render_template("register.html", error="Password must contain at least one digit")

            if register_user(email, password):
                return redirect(url_for("login"))
            else:
                return render_template("register.html", error="Registration failed. Please try a different email.")
            
        except Exception as e:
            app.logger.error(f"Registration error: {e}")
            return render_template("register.html", error="System error")


    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=["POST"])
    def login():
        """
        Simplified login route
        """
        try:
            if request.method == "GET":
                if current_user.is_authenticated:
                    return redirect(url_for("home"))
                return render_template("login.html")
            
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            
            if not email or not password:
                return render_template("login.html", error="Email and password required")

            if verify_user(email, password):
                # Fetch user model from DB (aliased as DBUser or just User from models)
                # In app.py imports: from database.models import User as DBUser
                user = db.session.get(DBUser, email)
                if user:
                    login_user(user, remember=True, duration=app.config.get("REMEMBER_COOKIE_DURATION"))
                    session.permanent = True
                app.logger.info(f"Successful login for {email}")
                return redirect(url_for("home"))
            else:
                return render_template("login.html", error="Invalid email or password")
            
        except Exception as e:
            app.logger.error(f"Login error: {e}")
            return render_template("login.html", error="System error")

    @app.route("/delete_account", methods=["POST"])
    @login_required
    def delete_account():
        confirmation = request.form.get("confirm_delete", "")
        if confirmation != "DELETE":
            flash("Account deletion requires typing DELETE to confirm.", "danger")
            return redirect(url_for("home"))

        email = current_user.id
        app.logger.info(f"Account deletion requested for {email}")
        if delete_user(email):
            logout_user()
            return redirect(url_for("register"))
        else:
            flash("Failed to delete account. Please try again.", "danger")
            return redirect(url_for("home"))

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        session.pop("visit_counted", None)
        app.logger.info(f"Logout for {current_user.id}")
        logout_user()
        session.pop('_flashes', None) 
        return redirect(url_for("login"))
    
    def load_user_teams(user_email):
        """Return list of team dicts created by this user from DB."""
        teams = []
        try:
            db_teams = DBTeam.query.filter_by(user_id=user_email).all()
            for t in db_teams:
                # Construct dict with full player data for frontend JS
                team_dict = {
                    "id": t.id,
                    "team_name": t.name,
                    "short_code": t.short_code,
                    "home_ground": t.home_ground,
                    "pitch_preference": t.pitch_preference,
                    "team_color": t.team_color,
                    "created_by_email": t.user_id,
                    "players": []
                }
                for p in t.players:
                    team_dict["players"].append({
                        "name": p.name,
                        "role": p.role,
                        "batting_rating": p.batting_rating,
                        "bowling_rating": p.bowling_rating,
                        "fielding_rating": p.fielding_rating,
                        "batting_hand": p.batting_hand,
                        "bowling_type": p.bowling_type,
                        "bowling_hand": p.bowling_hand
                    })
                teams.append(team_dict)
        except Exception as e:
            app.logger.error(f"Error loading teams from DB: {e}", exc_info=True)
        return teams


    @app.route("/team/create", methods=["GET", "POST"])
    @login_required
    def create_team():
        if request.method == "POST":
            try:
                # 1. Basic team info
                name = request.form["team_name"].strip()
                short_code = request.form["short_code"].strip().upper()
                home_ground = request.form["home_ground"].strip()
                pitch = request.form["pitch_preference"]
                action = request.form.get("action", "publish")
                is_draft = (action == "save_draft")

                # Validate required fields (Basic info always required)
                if not (name and short_code and home_ground and pitch):
                    return render_template("team_create.html", error="All team fields are required.")

                # Check for duplicate short_code for this user
                existing = DBTeam.query.filter_by(user_id=current_user.id, short_code=short_code).first()
                if existing:
                    return render_template("team_create.html", error=f"You already have a team with short code '{short_code}'. Please use a different code.")

                # 2. Collect player fields from form data
                player_names = request.form.getlist("player_name")
                roles = request.form.getlist("player_role")
                bat_ratings = request.form.getlist("batting_rating")
                bowl_ratings = request.form.getlist("bowling_rating")
                field_ratings = request.form.getlist("fielding_rating")
                bat_hands = request.form.getlist("batting_hand")
                bowl_types = request.form.getlist("bowling_type")
                bowl_hands = request.form.getlist("bowling_hand")

                players = []
                for i in range(len(player_names)):
                    try:
                        # Safe retrieval with defaults to prevent IndexError if form data is sparse
                        b_type = bowl_types[i] if i < len(bowl_types) else ""
                        b_hand = bowl_hands[i] if i < len(bowl_hands) else ""
                        
                        bat_r = int(bat_ratings[i])
                        bowl_r = int(bowl_ratings[i])
                        field_r = int(field_ratings[i])

                        # Validate rating bounds
                        if not (0 <= bat_r <= 100 and 0 <= bowl_r <= 100 and 0 <= field_r <= 100):
                            return render_template("team_create.html", error=f"Player {i+1}: All ratings must be between 0 and 100.")

                        player = Player(
                            name=player_names[i],
                            role=roles[i],
                            batting_rating=bat_r,
                            bowling_rating=bowl_r,
                            fielding_rating=field_r,
                            batting_hand=bat_hands[i],
                            bowling_type=b_type if b_type else "",
                            bowling_hand=b_hand if b_hand else ""
                        )
                        players.append(player)
                    except Exception as e:
                        app.logger.error(f"Error in player creation: {e}", exc_info=True)
                        return render_template("team_create.html", error=f"Error in player {i+1}: {e}")

                # --- Validation Logic ---
                if not is_draft:
                    # Enforce strict rules for Published/Active teams
                    
                    # Validate player count (Updated limits: 12-25)
                    if len(players) < 12 or len(players) > 25:
                        return render_template("team_create.html", error="You must enter between 12 and 25 players.")
                    
                    # Validate at least 1 wicketkeeper
                    wk_count = sum(1 for p in players if p.role == "Wicketkeeper")
                    if wk_count < 1:
                        return render_template("team_create.html", error="You need at least one Wicketkeeper.")

                    # Validate minimum 6 bowlers/all-rounders
                    bowl_count = sum(1 for p in players if p.role in ["Bowler", "All-rounder"])
                    if bowl_count < 6:
                        return render_template("team_create.html", error="You need at least six Bowler/All-rounder roles.")
                    
                    # Validate leadership selection
                    captain_name = request.form.get("captain")
                    wk_name = request.form.get("wicketkeeper")
                    
                    if not captain_name or not wk_name:
                         # Fallback if not selected but required
                         return render_template("team_create.html", error="Captain and Wicketkeeper selection required.")
                else:
                    # Relaxed rules for Drafts
                    if len(players) < 1:
                         return render_template("team_create.html", error="Draft must have at least 1 player.")
                    
                    # Optional leadership for drafts
                    captain_name = request.form.get("captain")
                    wk_name = request.form.get("wicketkeeper")

                # Read team color
                color = request.form["team_color"]

                # 3. Create and save team to DB
                try:
                    new_team = DBTeam(
                        user_id=current_user.id,
                        name=name,
                        short_code=short_code,
                        home_ground=home_ground,
                        pitch_preference=pitch,
                        team_color=color,
                        is_draft=is_draft
                    )
                    db.session.add(new_team)
                    db.session.flush() # Get ID for foreign keys

                    # Add players
                    for p in players:
                        is_captain = (p.name == captain_name) if captain_name else False
                        is_wk = (p.name == wk_name) if wk_name else False
                        
                        db_player = DBPlayer(
                            team_id=new_team.id,
                            name=p.name,
                            role=p.role,
                            batting_rating=p.batting_rating,
                            bowling_rating=p.bowling_rating,
                            fielding_rating=p.fielding_rating,
                            batting_hand=p.batting_hand,
                            bowling_type=p.bowling_type,
                            bowling_hand=p.bowling_hand,
                            is_captain=is_captain,
                            is_wicketkeeper=is_wk
                        )
                        db.session.add(db_player)
                    
                    db.session.commit()
                    status_msg = "Draft" if is_draft else "Active"
                    app.logger.info(f"Team '{new_team.name}' (ID: {new_team.id}) created as {status_msg} by {current_user.id}")
                    if is_draft:
                         flash("Team saved as draft.", "success")
                         return redirect(url_for("manage_teams"))
                    else:
                         return redirect(url_for("home"))

                except Exception as db_err:
                    db.session.rollback()
                    app.logger.error(f"Database error saving team: {db_err}", exc_info=True)
                    return render_template("team_create.html", error="Database error saving team.")

            except Exception as e:
                app.logger.error(f"Unexpected error saving team: {e}", exc_info=True)
                return render_template("team_create.html", error="An unexpected error occurred. Please try again.")

        # GET: Show form
        return render_template("team_create.html")
    
    @app.route("/teams/manage")
    @login_required
    def manage_teams():
        teams = []
        try:
            # Query teams from DB for current user
            db_teams = DBTeam.query.filter_by(user_id=current_user.id).all()
            for t in db_teams:
                # Need player details for stats calculation in template (e.g. squad size)
                # Naive serialization for now, but lightweight enough for manage view
                players_list = []
                for p in t.players:
                    players_list.append({
                        "role": p.role,
                        "name": p.name,
                        "is_captain": p.is_captain,
                        "is_wicketkeeper": p.is_wicketkeeper
                    })
                
                # Find captain name
                captain_name = next((p.name for p in t.players if p.is_captain), "Unknown")

                teams.append({
                    "id": t.id,
                    "team_name": t.name, # Template uses team_name
                    "short_code": t.short_code,
                    "home_ground": t.home_ground,
                    "pitch_preference": t.pitch_preference,
                    "team_color": t.team_color,
                    "captain": captain_name,
                    "players": players_list,
                    "is_draft": getattr(t, 'is_draft', False)
                })
        except Exception as e:
            app.logger.error(f"Error loading teams from DB: {e}", exc_info=True)
            
        return render_template("manage_teams.html", teams=teams)


    @app.route("/team/delete", methods=["POST"])
    @login_required
    def delete_team():
        short_code = request.form.get("short_code")
        if not short_code:
            flash("No team specified for deletion.", "danger")
            return redirect(url_for("manage_teams"))

        try:
            # Find team by short_code and owner
            team = DBTeam.query.filter_by(short_code=short_code, user_id=current_user.id).first()

            if not team:
                app.logger.warning(f"Delete failed: Team '{short_code}' not found or unauthorized for {current_user.id}")
                flash("Team not found or you don't have permission to delete it.", "danger")
                return redirect(url_for("manage_teams"))

            team_name = team.name
            # Delete from DB (cascade handles players)
            db.session.delete(team)
            db.session.commit()

            app.logger.info(f"Team '{short_code}' (ID: {team.id}) deleted by {current_user.id}")
            flash(f"Team '{team_name}' has been deleted.", "success")

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error deleting team from DB: {e}", exc_info=True)
            flash("An error occurred while deleting the team. Please try again.", "danger")

        return redirect(url_for("manage_teams"))


    @app.route("/team/<short_code>/edit", methods=["GET", "POST"])
    @login_required
    def edit_team(short_code):
        user_id = current_user.id
        
        try:
            # 1. Find team in DB
            team = DBTeam.query.filter_by(short_code=short_code, user_id=user_id).first()
            
            if not team:
                app.logger.warning(f"Edit failed: Team '{short_code}' not found or unauthorized for {user_id}")
                return redirect(url_for("manage_teams"))

            # POST: Update team
            if request.method == "POST":
                try:
                    # Update Basic Info
                    team.name = request.form["team_name"].strip()
                    new_short_code = request.form["short_code"].strip().upper()
                    team.home_ground = request.form["home_ground"].strip()
                    team.pitch_preference = request.form["pitch_preference"]
                    team.team_color = request.form["team_color"]

                    # Check for duplicate short_code if changed
                    if new_short_code != team.short_code:
                        conflict = DBTeam.query.filter_by(user_id=user_id, short_code=new_short_code).first()
                        if conflict:
                            return render_template("team_create.html", team={"team_name": team.name, "short_code": new_short_code, "home_ground": team.home_ground, "pitch_preference": team.pitch_preference, "team_color": team.team_color}, edit=True, error=f"You already have a team with short code '{new_short_code}'.")

                    # Check Action (Draft handling)
                    action = request.form.get("action", "publish")
                    is_draft = (action == "save_draft")

                    # Gather players from form
                    names = request.form.getlist("player_name")
                    roles = request.form.getlist("player_role")
                    bats  = request.form.getlist("batting_rating")
                    bowls = request.form.getlist("bowling_rating")
                    fields= request.form.getlist("fielding_rating")
                    bhands= request.form.getlist("batting_hand")
                    btypes= request.form.getlist("bowling_type")
                    bhand2s = request.form.getlist("bowling_hand")

                    captain_name = request.form.get("captain")
                    wk_name = request.form.get("wicketkeeper")

                    team_form_data = {
                        "team_name": team.name,
                        "short_code": new_short_code,
                        "home_ground": team.home_ground,
                        "pitch_preference": team.pitch_preference,
                        "team_color": team.team_color,
                        "created_by_email": team.user_id,
                        "captain": captain_name or "",
                        "wicketkeeper": wk_name or "",
                        "players": []
                    }
                    for i in range(len(names)):
                        team_form_data["players"].append({
                            "name": names[i],
                            "role": roles[i],
                            "batting_rating": bats[i],
                            "bowling_rating": bowls[i],
                            "fielding_rating": fields[i],
                            "batting_hand": bhands[i],
                            "bowling_type": btypes[i] or "",
                            "bowling_hand": bhand2s[i] or ""
                        })

                    # --- Validate BEFORE modifying DB ---
                    if not is_draft:
                        if len(names) < 12 or len(names) > 25:
                            return render_template("team_create.html", team=team_form_data, edit=True, error="Active teams must have 12-25 players.")

                        wk_count = roles.count("Wicketkeeper")
                        bowl_count = sum(1 for r in roles if r in ["Bowler", "All-rounder"])

                        if wk_count < 1:
                            return render_template("team_create.html", team=team_form_data, edit=True, error="Active teams need at least one Wicketkeeper.")
                        if bowl_count < 6:
                            return render_template("team_create.html", team=team_form_data, edit=True, error="Active teams need at least six Bowler/All-rounder roles.")

                        if not captain_name or not wk_name:
                            return render_template("team_create.html", team=team_form_data, edit=True, error="Active teams require a Captain and Wicketkeeper.")
                    else:
                        if len(names) < 1:
                            return render_template("team_create.html", team=team_form_data, edit=True, error="Drafts must have at least 1 player.")

                    # Validate rating bounds before any DB mutation
                    for i in range(len(names)):
                        bat_r = int(bats[i])
                        bowl_r = int(bowls[i])
                        field_r = int(fields[i])
                        if not (0 <= bat_r <= 100 and 0 <= bowl_r <= 100 and 0 <= field_r <= 100):
                            return render_template("team_create.html", team=team_form_data, edit=True, error=f"Player {i+1}: All ratings must be between 0 and 100.")

                    # --- All validation passed, now mutate DB ---
                    team.is_draft = is_draft
                    team.short_code = new_short_code
                    DBPlayer.query.filter_by(team_id=team.id).delete()

                    for i in range(len(names)):
                        p_name = names[i]
                        db_player = DBPlayer(
                            team_id=team.id,
                            name=p_name,
                            role=roles[i],
                            batting_rating=int(bats[i]),
                            bowling_rating=int(bowls[i]),
                            fielding_rating=int(fields[i]),
                            batting_hand=bhands[i],
                            bowling_type=btypes[i] or "",
                            bowling_hand=bhand2s[i] or "",
                            is_captain=(p_name == captain_name) if captain_name else False,
                            is_wicketkeeper=(p_name == wk_name) if wk_name else False
                        )
                        db.session.add(db_player)

                    db.session.commit()
                    status_msg = "Draft" if is_draft else "Active"
                    app.logger.info(f"Team '{team.short_code}' (ID: {team.id}) updated as {status_msg} by {user_id}")
                    flash(f"Team updated as {status_msg}.", "success")
                    return redirect(url_for("manage_teams"))

                except Exception as e:
                    db.session.rollback()
                    app.logger.error(f"Error updating team: {e}", exc_info=True)
                    flash("An error occurred while updating the team. Please try again.", "danger")
                    return redirect(url_for("edit_team", short_code=short_code))

            # GET: Render form with team data
            # Convert DB object to dictionary expected by template
            team_data = {
                "team_name": team.name,  # Fix: was "name", frontend expects "team_name"
                "short_code": team.short_code,
                "home_ground": team.home_ground,
                "pitch_preference": team.pitch_preference,
                "team_color": team.team_color,
                "created_by_email": team.user_id,
                "captain": next((p.name for p in team.players if p.is_captain), ""),
                "wicketkeeper": next((p.name for p in team.players if p.is_wicketkeeper), ""),
                "players": []
            }
            
            for p in team.players:
                team_data["players"].append({
                    "name": p.name,
                    "role": p.role,
                    "batting_rating": p.batting_rating,
                    "bowling_rating": p.bowling_rating,
                    "fielding_rating": p.fielding_rating,
                    "batting_hand": p.batting_hand,
                    "bowling_type": p.bowling_type,
                    "bowling_hand": p.bowling_hand
                })
                
            # Determine captain/wk names
            team_data["captain"] = next((p.name for p in team.players if p.is_captain), "")
            team_data["wicketkeeper"] = next((p.name for p in team.players if p.is_wicketkeeper), "")

            return render_template("team_create.html", team=team_data, edit=True)

        except Exception as e:
            app.logger.error(f"Error in edit_team: {e}", exc_info=True)
            return redirect(url_for("manage_teams"))
    
    
    @app.route("/match/setup", methods=["GET", "POST"])
    @login_required
    def match_setup():
        teams = load_user_teams(current_user.id)
        
        # Check for tournament fixture execution
        fixture_id = request.args.get('fixture_id')
        preselect_home = None
        preselect_away = None
        tournament_id = None
        
        if fixture_id:
            fixture = db.session.get(TournamentFixture, fixture_id)
            if fixture and fixture.tournament.user_id == current_user.id:
                # Prevent starting locked or completed matches
                if fixture.status == 'Locked':
                    flash("Cannot start a locked match. Wait for previous rounds to complete.", "error")
                    return redirect(url_for("tournament_dashboard", tournament_id=fixture.tournament.id))
                
                if fixture.status == 'Completed':
                    flash("This match is already completed.", "info")
                    return redirect(url_for("tournament_dashboard", tournament_id=fixture.tournament.id))

                preselect_home = fixture.home_team_id
                preselect_away = fixture.away_team_id
                tournament_id = fixture.tournament_id

        if request.method == "POST":
            clean_old_archives(PROD_MAX_AGE)
            cleanup_temp_scorecard_images()

            data = request.get_json()

            # Step 1: Load teams from DB using IDs from frontend
            home_id = data.get("team_home")
            away_id = data.get("team_away")
            
            # Tournament context
            req_tournament_id = data.get("tournament_id")
            req_fixture_id = data.get("fixture_id")

            # Fix: Use db.session.get() instead of legacy Model.query.get()
            home_db = db.session.get(DBTeam, home_id)
            away_db = db.session.get(DBTeam, away_id)
            
            if not home_db or not away_db:
                return jsonify({"error": "Invalid team selection"}), 400

            if req_fixture_id:
                fixture = db.session.get(TournamentFixture, req_fixture_id)
                if not fixture or fixture.tournament.user_id != current_user.id:
                    return jsonify({"error": "Invalid tournament fixture"}), 403
                if req_tournament_id and fixture.tournament_id != int(req_tournament_id):
                    return jsonify({"error": "Fixture does not match tournament"}), 400
                if fixture.home_team_id != home_id or fixture.away_team_id != away_id:
                    return jsonify({"error": "Fixture teams do not match selection"}), 400
                req_tournament_id = fixture.tournament_id
            elif req_tournament_id:
                tournament = db.session.get(Tournament, int(req_tournament_id))
                if not tournament or tournament.user_id != current_user.id:
                    return jsonify({"error": "Invalid tournament"}), 403

            # Fix: Define codes for filename generation later
            home_code = home_db.short_code
            away_code = away_db.short_code

            # Step 2: Construct legacy string identifiers for Match Engine compatibility
            # Engine expects "ShortCode_UserEmail" format to parse ShortCode via split('_')[0]
            data["team_home"] = f"{home_code}_{home_db.user_id}"
            data["team_away"] = f"{away_code}_{away_db.user_id}"

            # Helper to convert DB team to Full Dict (mimicking JSON file structure)
            def team_to_full_dict(t):
                d = {
                    "team_name": t.name,
                    "short_code": t.short_code,
                    "players": []
                }
                for p in t.players:
                    d["players"].append({
                        "name": p.name,
                        "role": p.role,
                        "batting_rating": p.batting_rating,
                        "bowling_rating": p.bowling_rating,
                        "fielding_rating": p.fielding_rating,
                        "batting_hand": p.batting_hand,
                        "bowling_type": p.bowling_type,
                        "bowling_hand": p.bowling_hand,
                        "will_bowl": False # Default
                    })
                return d

            full_home = team_to_full_dict(home_db)
            full_away = team_to_full_dict(away_db)

            # Step 3: Generic function to enrich player lists (XI and substitutes)
            def enrich_player_list(players_to_enrich, full_team_data):
                enriched = []
                for player_info in players_to_enrich:
                    # Find the full player data from the team dict
                    full_player_data = next((p for p in full_team_data["players"] if p["name"] == player_info["name"]), None)
                    if full_player_data:
                        enriched_player = full_player_data.copy()
                        # If 'will_bowl' was sent from frontend (for playing_xi), add it.
                        print("Logger in app.py for player_info: {}".format(player_info))
                        if 'will_bowl' in player_info:
                            enriched_player["will_bowl"] = player_info.get("will_bowl", False)
                        enriched.append(enriched_player)
                return enriched

            # Enrich both playing XI and substitutes
            data["playing_xi"]["home"] = enrich_player_list(data["playing_xi"]["home"], full_home)
            data["playing_xi"]["away"] = enrich_player_list(data["playing_xi"]["away"], full_away)

            if "substitutes" in data:
                data["substitutes"]["home"] = enrich_player_list(data["substitutes"]["home"], full_home)
                data["substitutes"]["away"] = enrich_player_list(data["substitutes"]["away"], full_away)
            else:
                data["substitutes"] = {"home": [], "away": []}


            # Step 4: Generate metadata and save file
            match_id = str(uuid.uuid4())
            ts = datetime.now().strftime("%Y%m%d%H%M%S")
            user = current_user.id
            fname = f"playing_{home_code}_vs_{away_code}_{user}_{ts}.json"

            match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
            os.makedirs(match_dir, exist_ok=True)
            path = os.path.join(match_dir, fname)

            data.update({
                "match_id": match_id,
                "created_by": user,
                "tournament_id": req_tournament_id,
                "fixture_id": req_fixture_id,
                "timestamp": ts,
            })

            with open(path, "w") as f:
                json.dump(data, f, indent=2)

            app.logger.info(f"[MatchSetup] Saved {fname} for {user}")
            return jsonify(match_id=match_id), 200

        return render_template("match_setup.html", 
                               teams=teams, 
                               preselect_home=preselect_home, 
                               preselect_away=preselect_away, 
                               tournament_id=tournament_id,
                               fixture_id=fixture_id)

    @app.route("/match/<match_id>")
    @login_required
    def match_detail(match_id):
        match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
        match_data = None

        # Search for the JSON whose match_id field matches
        for fn in os.listdir(match_dir):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(match_dir, fn)
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                if data.get("match_id") == match_id and data.get("created_by") == current_user.id:
                    match_data = data
                    break
            except Exception as e:
                app.logger.error(f"[MatchDetail] error loading {fn}: {e}", exc_info=True)

        if not match_data:
            return redirect(url_for("home"))

        # increment_matches_simulated()  <-- REMOVED: Caused premature counting on page load/reload
        
        # Check if match is completed
        if match_data.get("current_state") == "completed":
             # Redirect to the dedicated scoreboard view
             return redirect(url_for("view_scoreboard", match_id=match_id))
            
        # Render the detail page, passing the loaded JSON
        return render_template("match_detail.html", match=match_data)
    
    @app.route("/match/<match_id>/scoreboard")
    @login_required
    def view_scoreboard(match_id):
        db_match = DBMatch.query.filter_by(id=match_id, user_id=current_user.id).first()
        if not db_match:
            flash("Match not found", "error")
            return redirect(url_for("home"))

        scorecards = (
            MatchScorecard.query.options(joinedload(MatchScorecard.player_ref))
            .filter_by(match_id=match_id)
            .all()
        )
        if not scorecards:
            flash("Detailed scorecard stats unavailable - showing summary only", "warning")
            # Don't redirect, just continue with empty scorecards


        teams = {
            team.id: team
            for team in DBTeam.query.filter(DBTeam.id.in_([db_match.home_team_id, db_match.away_team_id])).all()
        }

        def format_overs(card):
            if card.balls_bowled:
                return f"{card.balls_bowled // 6}.{card.balls_bowled % 6}"
            if card.overs:
                return f"{card.overs:.1f}"
            return "0.0"

        innings_data = {}
        for card in scorecards:
            entry = innings_data.setdefault(
                card.innings_number,
                {
                    "number": card.innings_number,
                    "batting": [],
                    "bowling": [],
                    "batting_team_id": None,
                    "bowling_team_id": None,
                },
            )
            player_name = card.player_ref.name if card.player_ref else "Unknown"

            if card.record_type == "batting":
                entry["batting_team_id"] = card.team_id
                entry["batting"].append(
                    {
                        "name": player_name,
                        "runs": card.runs,
                        "balls": card.balls,
                        "fours": card.fours,
                        "sixes": card.sixes,
                        "is_out": card.is_out,
                        "wicket_type": card.wicket_type,
                        "wicket_taker_name": card.wicket_taker_name,
                        "fielder_name": card.fielder_name,
                        "strike_rate": card.strike_rate if card.strike_rate else (card.runs * 100.0 / card.balls if card.balls > 0 else 0),
                        "position": card.position or 9999,
                    }
                )
            elif card.record_type == "bowling":
                entry["bowling_team_id"] = card.team_id
                entry["bowling"].append(
                    {
                        "name": player_name,
                        "overs": format_overs(card),
                        "runs_conceded": card.runs_conceded,
                        "wickets": card.wickets,
                        "maidens": card.maidens,
                        "wides": card.wides,
                        "noballs": card.noballs,
                        "economy": (card.runs_conceded / card.overs) if card.overs and card.overs > 0 else 0,
                        "position": card.position or 9999,
                    }
                )

        innings_list = []
        for innings_number in sorted(innings_data.keys()):
            entry = innings_data[innings_number]
            entry["batting"].sort(key=lambda item: item["position"])
            entry["bowling"].sort(key=lambda item: item["position"])
            # A12: Compute extras breakdown from bowling stats
            total_wides = sum(item.get("wides", 0) for item in entry["bowling"])
            total_noballs = sum(item.get("noballs", 0) for item in entry["bowling"])
            batting_runs = sum(item["runs"] for item in entry["batting"])
            total_extras = total_wides + total_noballs  # byes/legbyes not tracked in DB
            entry["extras"] = {
                "wides": total_wides,
                "noballs": total_noballs,
                "total": total_extras
            }
            entry["score"] = batting_runs + total_extras
            entry["wickets"] = sum(1 for item in entry["batting"] if item["is_out"])
            entry["batting_team_name"] = teams.get(entry["batting_team_id"]).name if entry["batting_team_id"] in teams else "Unknown"
            entry["bowling_team_name"] = teams.get(entry["bowling_team_id"]).name if entry["bowling_team_id"] in teams else "Unknown"
            innings_list.append(entry)

        match_summary = {
            "result_description": db_match.result_description or "Match Completed",
            "team_home": teams.get(db_match.home_team_id).name if db_match.home_team_id in teams else "Home",
            "team_away": teams.get(db_match.away_team_id).name if db_match.away_team_id in teams else "Away",
            "venue": db_match.venue or "Stadium",
            "tournament_id": db_match.tournament_id,
        }

        return render_template(
            "scorecard_view.html",
            match=match_summary,
            innings_list=innings_list,
        )
    


    @app.route("/match/<match_id>/set-toss", methods=["POST"])
    @login_required
    def set_toss(match_id):
        match_data, match_path, err = _load_match_file_for_user(match_id)
        if err:
            return err

        data = request.get_json() or {}
        toss_winner = data.get("winner")
        decision = data.get("decision")
        if not toss_winner or not decision:
            return jsonify({"error": "winner and decision are required"}), 400

        match_data["toss_winner"] = toss_winner
        match_data["toss_decision"] = decision

        with open(match_path, "w") as f:
            json.dump(match_data, f, indent=2)
        app.logger.info(f"[MatchToss] {toss_winner} chose to {decision} (Match: {match_id})")
        return jsonify({"status": "success"}), 200
    
    @app.route("/match/<match_id>/spin-toss", methods=["POST"])
    @login_required
    def spin_toss(match_id):
        match_data, match_path, err = _load_match_file_for_user(match_id)
        if err:
            return err

        if not match_data:
            return jsonify({"error": "Match not found"}), 404

        team_home = match_data["team_home"].split('_')[0]
        team_away = match_data["team_away"].split('_')[0]
        toss_choice = match_data["toss"]
        toss_result = random.choice(["Heads", "Tails"])
        home_captain = match_data["playing_xi"]["home"][0]["name"]
        away_captain = match_data["playing_xi"]["away"][0]["name"]

        toss_winner = team_away if toss_choice == toss_result else team_home
        toss_decision = random.choice(["Bat", "Bowl"])

        match_data["toss_winner"] = toss_winner
        match_data["toss_decision"] = toss_decision

        with open(match_path, "w") as f:
            json.dump(match_data, f, indent=2)
        
        # ????? NEW: update the in-memory Match, if created
        if match_id in MATCH_INSTANCES:
            inst = MATCH_INSTANCES[match_id]
            inst.toss_winner   = toss_winner
            inst.toss_decision = toss_decision
            inst.batting_team  = inst.home_xi if toss_decision=="Bat" else inst.away_xi
            inst.bowling_team  = inst.away_xi if inst.batting_team==inst.home_xi else inst.home_xi

        commentary = f"{home_captain} spins the coin and {away_captain} calls for {toss_choice}.<br>" \
                    f"{toss_winner} won the toss and choose to {toss_decision} first."
        commentary = commentary +"\n"

        # Determine batting and bowling teams based on toss result
        if toss_winner == team_home:
            if toss_decision == "Bat":
                batting_team = match_data["playing_xi"]["home"]
                bowling_team = match_data["playing_xi"]["away"]
            else:  # Bowl
                batting_team = match_data["playing_xi"]["away"]
                bowling_team = match_data["playing_xi"]["home"]
        else:  # toss_winner == team_away
            if toss_decision == "Bat":
                batting_team = match_data["playing_xi"]["away"]
                bowling_team = match_data["playing_xi"]["home"]
            else:  # Bowl
                batting_team = match_data["playing_xi"]["home"]
                bowling_team = match_data["playing_xi"]["away"]

        # Build complete toss commentary
        full_commentary = f"{home_captain} spins the coin and {away_captain} calls for {toss_choice}.<br>" \
                        f"{toss_winner} won the toss and choose to {toss_decision} first."
        
        return jsonify({
            "toss_commentary": full_commentary,
            "toss_winner":     toss_winner,
            "toss_decision":   toss_decision
        })

    @app.route("/match/<match_id>/impact-player-swap", methods=["POST"])
    @login_required
    def impact_player_swap(match_id):
        """
        Handle impact player substitution with optional swaps for each team.
        
        PRODUCTION-LEVEL ENDPOINT with comprehensive error handling, validation,
        logging, and state management for mid-match player substitutions.
        """
        app.logger.info(f"[ImpactSwap] Starting impact player swap for match {match_id}")
        
        try:
            swap_data = request.get_json()
            if not swap_data:
                return jsonify({"error": "Request body is required"}), 400
                
            home_swap = swap_data.get("home_swap")
            away_swap = swap_data.get("away_swap")
            
            # Load match data from filesystem
            match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
            match_path, match_data = None, None
            
            for filename in os.listdir(match_dir):
                if not filename.endswith(".json"): continue
                file_path = os.path.join(match_dir, filename)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("match_id") == match_id:
                        match_data, match_path = data, file_path
                        break
                except Exception as e:
                    app.logger.warning(f"[ImpactSwap] Error reading {filename}: {e}")
                    continue
            
            if not match_data:
                return jsonify({"error": "Match not found"}), 404

            if match_data.get("created_by") != current_user.id:
                return jsonify({"error": "Unauthorized access"}), 403

            impact_swaps = {}
            
            # Perform home team swap
            if home_swap:
                home_out_idx = home_swap["out_player_index"]
                home_in_idx = home_swap["in_player_index"]
                home_out_player = match_data["playing_xi"]["home"][home_out_idx]
                home_in_player = match_data["substitutes"]["home"][home_in_idx]
                match_data["playing_xi"]["home"][home_out_idx] = home_in_player
                match_data["substitutes"]["home"][home_in_idx] = home_out_player
                impact_swaps["home"] = {"out": home_out_player["name"], "in": home_in_player["name"]}
            
            # Perform away team swap
            if away_swap:
                away_out_idx = away_swap["out_player_index"]
                away_in_idx = away_swap["in_player_index"]
                away_out_player = match_data["playing_xi"]["away"][away_out_idx]
                away_in_player = match_data["substitutes"]["away"][away_in_idx]
                match_data["playing_xi"]["away"][away_out_idx] = away_in_player
                match_data["substitutes"]["away"][away_in_idx] = away_out_player
                impact_swaps["away"] = {"out": away_out_player["name"], "in": away_in_player["name"]}

            # Mark that swaps have occurred
            match_data["impact_players_swapped"] = True
            
            # =================================================================
            # ? START: CRITICAL FIX - UPDATE IN-MEMORY INSTANCE
            # =================================================================
            if match_id in MATCH_INSTANCES:
                app.logger.info(f"[ImpactSwap] Found active match instance for {match_id}. Updating state.")
                match_instance = MATCH_INSTANCES[match_id]
                
                # Directly update the instance's player lists
                match_instance.home_xi = match_data["playing_xi"]["home"]
                match_instance.away_xi = match_data["playing_xi"]["away"]
                
                # Also update the raw data stored in the instance
                match_instance.data = match_data
                
                app.logger.info(f"[ImpactSwap] Instance updated. Home XI now has {len(match_instance.home_xi)} players.")
            else:
                app.logger.warning(f"[ImpactSwap] No active match instance found for {match_id}. File will be updated, but live game may not reflect changes until reload.")
            # =================================================================
            # ? END: CRITICAL FIX
            # =================================================================
            
            # Save the updated data back to the JSON file
            with open(match_path, "w", encoding="utf-8") as f:
                json.dump(match_data, f, indent=2)
                
            app.logger.info(f"[ImpactSwap] Successfully completed swaps for match {match_id}: {impact_swaps}")
            
            return jsonify({
                "success": True,
                "match_id": match_id,
                "updated_match_data": match_data,
                "swaps_made": impact_swaps
            }), 200

        except Exception as e:
            app.logger.error(f"[ImpactSwap] Unexpected error for match {match_id}: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500


    @app.route("/match/<match_id>/update-final-lineups", methods=["POST"])
    @login_required
    def update_final_lineups(match_id):
        """
        Update match instance with final reordered lineups and resync stats dictionaries.
        """
        try:
            if match_id not in MATCH_INSTANCES:
                _match_data, _match_path, err = _load_match_file_for_user(match_id)
                if err:
                    return err
                app.logger.info(f"[FinalLineups] Match instance {match_id} not yet in memory. No action needed.")
                return jsonify({"success": True, "message": "Lineups will be loaded from updated file."}), 200

            match_instance = MATCH_INSTANCES[match_id]
            if match_instance.data.get("created_by") != current_user.id:
                return jsonify({"error": "Unauthorized"}), 403
            lineup_data = request.get_json() or {}
            home_final_xi = lineup_data.get("home_final_xi")
            away_final_xi = lineup_data.get("away_final_xi")

            # Update the master XI lists in the instance
            if home_final_xi:
                match_instance.home_xi = home_final_xi
                match_instance.data["playing_xi"]["home"] = home_final_xi
                app.logger.info(f"[FinalLineups] Updated HOME XI for match {match_id}")

            if away_final_xi:
                match_instance.away_xi = away_final_xi
                match_instance.data["playing_xi"]["away"] = away_final_xi
                app.logger.info(f"[FinalLineups] Updated AWAY XI for match {match_id}")

            # --- START FIX ---
            # Determine the current batting and bowling teams based on the updated XIs
            team_home_code = match_instance.match_data["team_home"].split("_")[0]
            first_batting_team_was_home = (match_instance.toss_winner == team_home_code and match_instance.toss_decision == "Bat") or \
                                        (match_instance.toss_winner != team_home_code and match_instance.toss_decision == "Bowl")

            current_batting_team_list = None
            if match_instance.innings == 1:
                if first_batting_team_was_home:
                    match_instance.batting_team = match_instance.home_xi
                    match_instance.bowling_team = match_instance.away_xi
                else:
                    match_instance.batting_team = match_instance.away_xi
                    match_instance.bowling_team = match_instance.home_xi
            else:  # Innings 2
                if first_batting_team_was_home:
                    match_instance.batting_team = match_instance.away_xi
                    match_instance.bowling_team = match_instance.home_xi
                else:
                    match_instance.batting_team = match_instance.home_xi
                    match_instance.bowling_team = match_instance.away_xi
            
            # Preserve old stats before rebuilding the dictionary
            old_batsman_stats = getattr(match_instance, 'batsman_stats', {}).copy()
            new_batsman_stats = {}

            # Rebuild the batsman_stats dictionary using the new batting_team
            for player in match_instance.batting_team:
                player_name = player["name"]
                if player_name in old_batsman_stats:
                    # If player already has stats, keep them
                    new_batsman_stats[player_name] = old_batsman_stats[player_name]
                else:
                    # If it's a new player (e.g., impact sub), initialize their stats
                    new_batsman_stats[player_name] = {
                        "runs": 0, "balls": 0, "fours": 0, "sixes": 0,
                        "ones": 0, "twos": 0, "threes": 0, "dots": 0,
                        "wicket_type": "", "bowler_out": "", "fielder_out": ""
                    }
            
            # Overwrite the instance's stats with the newly synced dictionary
            match_instance.batsman_stats = new_batsman_stats
            app.logger.info(f"[FinalLineups] Batsman stats dictionary resynced. Contains {len(new_batsman_stats)} players.")
            
            # Update the current striker and non-striker objects
            if match_instance.wickets < 10 and len(match_instance.batting_team) > 1:
                # Ensure batter_idx is valid for the current team size
                if match_instance.batter_idx[0] < len(match_instance.batting_team) and \
                match_instance.batter_idx[1] < len(match_instance.batting_team):
                    match_instance.current_striker = match_instance.batting_team[match_instance.batter_idx[0]]
                    match_instance.current_non_striker = match_instance.batting_team[match_instance.batter_idx[1]]
            # --- END FIX ---

            app.logger.info(f"[FinalLineups] Confirmed batting order for Innings {match_instance.innings}:")
            for i, player in enumerate(match_instance.batting_team):
                app.logger.info(f"   {i+1}. {player['name']}")

            return jsonify({"success": True}), 200

        except Exception as e:
            app.logger.error(f"Error updating final lineups: {e}", exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500
        
    @app.route("/match/<match_id>/next-ball", methods=["POST"])
    @login_required
    def next_ball(match_id):
        if match_id not in MATCH_INSTANCES:
            match_data, _match_path, err = _load_match_file_for_user(match_id)
            if err:
                return err
            
            # Reset impact player flags for fresh simulation
            if "impact_players_swapped" in match_data:
                del match_data["impact_players_swapped"]
            if "impact_swaps" in match_data:
                del match_data["impact_swaps"]

            MATCH_INSTANCES[match_id] = Match(match_data)

        match = MATCH_INSTANCES[match_id]
        if match.data.get("created_by") != current_user.id:
            return jsonify({"error": "Unauthorized"}), 403
        outcome = match.next_ball()

        # Explicitly send final score and wickets clearly
        if outcome.get("match_over"):
            # Only increment if this is the first time we're seeing the match end
            # (Checking if it wasn't already marked completed prevents double counting on repeated API calls)
            if match.data.get("current_state") != "completed":
                 increment_matches_simulated()
                 
            # If this is a tournament match, autosave to DB and update standings
            if match.data.get("tournament_id"):
                _handle_tournament_match_completion(match, match_id, outcome, app.logger)

            return jsonify({
                "innings_end":     match.innings == 2, # Flag generic innings end
                "innings_number":  match.innings,
                "match_over":      True,
                "commentary":      outcome.get("commentary", "<b>Match Over!</b>"),
                "scorecard_data":  outcome.get("scorecard_data"),
                "score":           outcome.get("final_score", match.score),
                "wickets":         outcome.get("wickets",  match.wickets),
                "result":          outcome.get("result",  "Match ended")
            })

        return jsonify(outcome)
    

    @app.route("/match/<match_id>/start-super-over", methods=["POST"])
    @login_required
    def start_super_over(match_id):
        if match_id not in MATCH_INSTANCES:
            return jsonify({"error": "Match not found"}), 404
        
        match = MATCH_INSTANCES[match_id]
        if match.data.get("created_by") != current_user.id:
            return jsonify({"error": "Unauthorized"}), 403
        data = request.get_json()
        first_batting_team = data.get("first_batting_team")
        
        result = match.start_super_over(first_batting_team)
        
        return jsonify(result)

    @app.route("/match/<match_id>/next-super-over-ball", methods=["POST"])
    @login_required
    def next_super_over_ball(match_id):
        if match_id not in MATCH_INSTANCES:
            return jsonify({"error": "Match not found"}), 404
        
        match = MATCH_INSTANCES[match_id]
        if match.data.get("created_by") != current_user.id:
            return jsonify({"error": "Unauthorized"}), 403
        try:
            result = match.next_super_over_ball()
            return jsonify(result)
        except Exception as e:
            app.logger.error(f"Error in super over: {e}", exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500
    
    # Add this endpoint to your app.py

    @app.route("/match/<match_id>/save-commentary", methods=["POST"])
    @login_required
    def save_commentary(match_id):
        """Receive and store the complete frontend commentary for archiving"""
        try:
            print(f"DEBUG: Received commentary request for match {match_id}")
            
            data = request.get_json()
            commentary_html = data.get('commentary_html', '')
            
            print(f"DEBUG: Commentary HTML length: {len(commentary_html)}")
            print(f"DEBUG: Contains 'End of over': {'End of over' in commentary_html}")
            print(f"DEBUG: First 300 chars: {commentary_html[:300]}")
            
            if not commentary_html:
                return jsonify({"error": "No commentary provided"}), 400
            
            # Store commentary for the match instance
            if match_id in MATCH_INSTANCES:
                match_instance = MATCH_INSTANCES[match_id]
                if match_instance.data.get("created_by") != current_user.id:
                    return jsonify({"error": "Unauthorized"}), 403
                
                # Convert HTML to clean text list for archiving
                frontend_commentary = html_to_commentary_list(commentary_html)
                print(f"DEBUG: Converted to {len(frontend_commentary)} commentary items")
                
                # Replace the backend commentary with frontend commentary
                match_instance.frontend_commentary_captured = frontend_commentary
                
                # DON'T trigger archive creation here - it already happened
                # Just store the commentary for next time
                print(f"DEBUG: Stored frontend commentary for future use")
                
                app.logger.info(f"[Commentary] Captured {len(frontend_commentary)} items for match {match_id}")
                return jsonify({"message": "Commentary captured successfully"}), 200
            else:
                print(f"DEBUG: Match instance {match_id} not found in MATCH_INSTANCES")
                return jsonify({"error": "Match instance not found"}), 404
                
        except Exception as e:
            print(f"DEBUG: Error in save_commentary: {e}")
            app.logger.error(f"Error saving commentary: {e}", exc_info=True)
            return jsonify({"error": "Failed to save commentary"}), 500

    def html_to_commentary_list(html_content):
        """Convert HTML commentary to clean text list"""
        from bs4 import BeautifulSoup
        import re
        
        # Parse HTML
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Extract all paragraph texts
        paragraphs = soup.find_all('p')
        commentary_items = []
        
        for p in paragraphs:
            text = p.get_text().strip()
            if text and text != "Match starts soon...":  # Skip placeholder text
                commentary_items.append(str(p))  # Keep HTML structure for archiver
        
        return commentary_items


    @app.route("/match/<match_id>/download-archive", methods=["POST"])
    @login_required
    @limiter.limit("10 per minute")
    def download_archive(match_id):
        """
        PRODUCTION VERSION with HTML Integration
        1) Receive HTML content from frontend
        2) Load match metadata and instance
        3) Use MatchArchiver to create complete archive with CSV, JSON, TXT, AND HTML
        4) Return ZIP file to user (also stored under <PROJECT_ROOT>/data/)
        """
        try:
            app.logger.info(f"[DownloadArchive] Starting archive creation for match '{match_id}'")

            # ??? A) Extract HTML content from request ???????????????????????????
            payload = request.get_json() or {}
            html_content = payload.get("html_content")
            if not html_content:
                app.logger.error("[DownloadArchive] No HTML content provided in request payload")
                return jsonify({"error": "HTML content is required"}), 400

            app.logger.debug(f"[DownloadArchive] Received HTML content length: {len(html_content):,} characters")
            if len(html_content) < 1000:
                app.logger.warning("[DownloadArchive] HTML content seems unusually short (< 1,000 chars)")

            # ??? B) Load match metadata ?????????????????????????????????????????
            match_meta = load_match_metadata(match_id)
            if not match_meta:
                app.logger.error(f"[DownloadArchive] Match metadata not found for match_id='{match_id}'")
                return jsonify({"error": "Match not found"}), 404

            # Verify ownership
            created_by = match_meta.get("created_by")
            if created_by != current_user.id:
                app.logger.warning(f"[DownloadArchive] Unauthorized access: user='{current_user.id}' attempted to archive match='{match_id}'")
                return jsonify({"error": "Unauthorized"}), 403

            # ??? C) Retrieve or rehydrate match instance ?????????????????????????
            match_instance = MATCH_INSTANCES.get(match_id)
            if not match_instance:
                app.logger.info(f"[DownloadArchive] Match instance not in memory; recreating minimal Match for '{match_id}'")
                from engine.match import Match
                match_instance = Match(match_meta)

            # ??? D) Locate original JSON file on disk ???????????????????????????
            from match_archiver import find_original_json_file
            original_json_path = find_original_json_file(match_id)
            if not original_json_path:
                app.logger.error(f"[DownloadArchive] Original JSON file not found for match_id='{match_id}'")
                return jsonify({"error": "Original match file not found"}), 404

            app.logger.debug(f"[DownloadArchive] Found original JSON at '{original_json_path}'")

            # ??? E) Extract commentary log ???????????????????????????????????????
            if getattr(match_instance, "frontend_commentary_captured", None):
                commentary_log = match_instance.frontend_commentary_captured
                app.logger.info(f"[DownloadArchive] Using frontend commentary (items={len(commentary_log)})")
            elif getattr(match_instance, "commentary", None):
                commentary_log = match_instance.commentary
                app.logger.info(f"[DownloadArchive] Using backend commentary (items={len(commentary_log)})")
            else:
                commentary_log = ["Match completed - commentary preserved in HTML"]
                app.logger.warning("[DownloadArchive] No commentary found; using fallback single-line log")

            # ??? F) Instantiate MatchArchiver and create ZIP ????????????????????
            from match_archiver import MatchArchiver
            archiver = MatchArchiver(match_meta, match_instance)
            zip_name = f"{archiver.folder_name}.zip"
            app.logger.info(f"[DownloadArchive] Creating archive '{zip_name}' via MatchArchiver")

            try:
                # create_archive() will write ZIP to <PROJECT_ROOT>/data/<zip_name>
                success = archiver.create_archive(
                    original_json_path=original_json_path,
                    commentary_log=commentary_log,
                    html_content=html_content
                )
                if not success:
                    app.logger.error(f"[DownloadArchive] MatchArchiver reported failure for '{match_id}'")
                    return jsonify({"error": "Failed to create archive"}), 500
            except ValueError as ve:
                app.logger.error(f"[DownloadArchive] Validation error during archiving: {ve}", exc_info=True)
                return jsonify({"error": "Invalid archive data provided"}), 400
            except Exception as arch_err:
                app.logger.error(f"[DownloadArchive] Failed to create archive for match '{match_id}': {arch_err}", exc_info=True)
                return jsonify({"error": "Failed to create archive"}), 500

            # ??? G) Compute and confirm ZIP path on disk ?????????????????????????
            zip_path = os.path.join(PROJECT_ROOT, "data", zip_name)
            if not os.path.isfile(zip_path):
                app.logger.error(f"[DownloadArchive] ZIP file missing after creation: '{zip_path}'")
                return jsonify({"error": "Archive ZIP file not found"}), 500

            zip_size = os.path.getsize(zip_path)
            app.logger.info(f"[DownloadArchive] ZIP successfully created: '{zip_name}' ({zip_size:,} bytes)")

            # ??? H) Stream the ZIP file back to the browser ?????????????????????
            try:
                app.logger.debug(f"[DownloadArchive] Sending ZIP to client: '{zip_path}'")
                return send_file(
                    zip_path,
                    mimetype="application/zip",
                    as_attachment=True,
                    download_name=zip_name
                )
            except Exception as send_err:
                app.logger.error(f"[DownloadArchive] Error sending ZIP file for match '{match_id}': {send_err}", exc_info=True)
                return jsonify({"error": "Failed to send archive file"}), 500

        except Exception as e:
            app.logger.error(f"[DownloadArchive] Unexpected error: {e}", exc_info=True)
            return jsonify({"error": "An unexpected error occurred while creating the archive"}), 500



    @app.route("/my-matches")
    @login_required
    def my_matches():
        """
        Display all ZIP archives in data/files/ matching current_user.id,
        regardless of subfolders. Only show files up to 7 days old.
        """
        username    = current_user.id
        files_dir   = os.path.join(PROJECT_ROOT, "data")
        valid_files = []

        app.logger.info(f"User '{username}' requested /my-matches")

        try:
            if not os.path.isdir(files_dir):
                app.logger.warning(f"'{files_dir}' does not exist for user '{username}'")
            else:
                now = time.time()
                max_age = 7 * 24 * 3600  # 7 days in seconds

                for fn in os.listdir(files_dir):
                    # Only consider ".zip" and filenames containing "_<username>_"
                    if not fn.lower().endswith(".zip"):
                        continue
                    if f"_{username}_" not in fn:
                        continue

                    full_path = os.path.join(files_dir, fn)
                    if not os.path.isfile(full_path):
                        app.logger.debug(f"Skipping '{fn}' (not a regular file)")
                        continue

                    age = now - os.path.getmtime(full_path)
                    if age > max_age:
                        app.logger.info(f"Skipping old archive '{fn}' (age {age//3600}h > 7d)")
                        continue

                    # Build URLs for download & delete
                    download_url = f"/archives/{username}/{fn}"
                    delete_url   = f"/archives/{username}/{fn}"

                    valid_files.append({
                        "filename":     fn,
                        "download_url": download_url,
                        "delete_url":   delete_url
                    })

                app.logger.info(f"User '{username}' has {len(valid_files)} valid archives")

        except Exception as e:
            app.logger.error(f"Error listing archives in '{files_dir}' for '{username}': {e}", exc_info=True)

        return render_template("my_matches.html", files=valid_files)


    @app.route("/archives/<username>/<filename>", methods=["GET"])
    @login_required
    def serve_archive(username, filename):
        """
        Serve a ZIP file stored under PROJECT_ROOT/data/<filename>
        Only the user whose email == username can download it.
        """
        # 1) Authorization check: current_user.id holds the email
        if current_user.id != username:
            app.logger.warning(f"Unauthorized download attempt by '{current_user.id}' for '{username}/{filename}'")
            return jsonify({"error": "Unauthorized"}), 403
        if f"_{current_user.id}_" not in filename:
            app.logger.warning(f"Unauthorized download attempt by '{current_user.id}' for filename '{filename}'")
            return jsonify({"error": "Unauthorized"}), 403

        # 2) Prevent directory-traversal (reject anything with a slash)
        if "/" in filename or "\\" in filename:
            app.logger.warning(f"Invalid filename in download: {filename}")
            return jsonify({"error": "Invalid filename"}), 400

        # 3) Build the absolute path to the ZIP under data/
        zip_path = os.path.join(PROJECT_ROOT, "data", filename)
        if not os.path.isfile(zip_path):
            app.logger.warning(f"Attempt to download non-existent file: {zip_path}")
            return jsonify({"error": "File not found"}), 404

        # 4) Stream the file back
        try:
            app.logger.info(f"Sending archive '{filename}' to user '{username}'")
            return send_file(
                zip_path,
                mimetype="application/zip",
                as_attachment=True,
                download_name=filename
            )
        except Exception as e:
            app.logger.error(f"Error sending archive {zip_path}: {e}", exc_info=True)
            return jsonify({"error": "Failed to send file"}), 500


    @app.route('/archives/<path:archive_name>', methods=['DELETE'])
    @login_required
    def delete_archive(archive_name):
        """
        DELETE endpoint to remove an archive file.
        Expects archive_name to be either 'filename' or 'username/filename'.
        Verified against current_user.id for security.
        """
        # 1. Extract filename and verify ownership
        if '/' in archive_name:
            username_part, filename = archive_name.split('/', 1)
            # Ensure the user is deleting their own file
            if username_part != current_user.id:
                app.logger.warning(f"Unauthorized delete attempt by {current_user.id} for {archive_name}")
                return jsonify({'error': 'Unauthorized'}), 403
        else:
            filename = archive_name
            # If only filename is provided, we must verify it contains the username
            if f"_{current_user.id}_" not in filename:
                app.logger.warning(f"Unauthorized delete attempt by {current_user.id} for {filename}")
                return jsonify({'error': 'Unauthorized'}), 403

        # 2. Normalize filename
        filename = os.path.basename(filename)
        
        # 3. Build the absolute path under ARCHIVES_FOLDER
        archive_folder = app.config.get('ARCHIVES_FOLDER')
        if not archive_folder:
            app.logger.error("ARCHIVES_FOLDER is not configured")
            return jsonify({'error': 'Server misconfiguration'}), 500

        file_path = os.path.join(archive_folder, filename)

        # 4. Check existence
        if not os.path.isfile(file_path):
            app.logger.info(f"Delete requested for non-existent file: {file_path}")
            return jsonify({'error': 'File not found'}), 404

        # 5. Attempt removal
        try:
            os.remove(file_path)
            app.logger.info(f"Deleted archive: {file_path}")
            
            # Also cleanup any related CSV files if they exist in statistics?
            # (Optional, but good for storage)
            
            return jsonify({'message': 'Archive deleted successfully'}), 200

        except PermissionError:
            app.logger.exception(f"Permission denied deleting {file_path}")
            return jsonify({'error': 'Permission denied'}), 403

        except Exception:
            app.logger.exception(f"Unexpected error deleting {file_path}")
            return jsonify({'error': 'Internal server error'}), 500
        

    @app.route('/match/<match_id>/save-scorecard-images', methods=['POST'])
    @login_required
    @limiter.limit("10 per minute")
    def save_scorecard_images(match_id):
        MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB
        ALLOWED_CONTENT_TYPES = {'image/png', 'image/jpeg', 'image/webp'}

        try:
            from pathlib import Path
            if not _is_valid_match_id(match_id):
                return jsonify({"error": "Invalid match id"}), 400

            _match_data, _match_path, err = _load_match_file_for_user(match_id)
            if err:
                return err

            # Use absolute path with user isolation
            temp_dir = Path(PROJECT_ROOT) / "data" / "temp_scorecard_images" / secure_filename(current_user.id)
            temp_dir.mkdir(parents=True, exist_ok=True)

            saved_files = []

            for field_name, label in [('first_innings_image', 'first'), ('second_innings_image', 'second')]:
                if field_name in request.files:
                    img = request.files[field_name]
                    if img.filename:
                        # Validate content type
                        if img.content_type not in ALLOWED_CONTENT_TYPES:
                            return jsonify({"error": f"Invalid file type for {label} innings image. Allowed: PNG, JPEG, WebP"}), 400

                        # Validate file size
                        img.seek(0, 2)  # Seek to end
                        size = img.tell()
                        img.seek(0)     # Reset to start
                        if size > MAX_IMAGE_SIZE:
                            return jsonify({"error": f"The {label} innings image exceeds the 5 MB size limit"}), 400

                        safe_match_id = secure_filename(match_id)
                        if safe_match_id != match_id:
                            return jsonify({"error": "Invalid match id"}), 400

                        ext = 'png' if img.content_type == 'image/png' else ('jpg' if img.content_type == 'image/jpeg' else 'webp')
                        img_path = temp_dir / f"{safe_match_id}_{label}_innings_scorecard.{ext}"
                        img.save(img_path)
                        saved_files.append(str(img_path))

            return jsonify({
                "success": True,
                "saved_files": saved_files
            })

        except Exception as e:
            app.logger.error(f"Error saving scorecard images: {e}", exc_info=True)
            return jsonify({"error": "An error occurred while saving images"}), 500

    # --- TOURNAMENT ROUTES ---

    @app.route("/tournaments")
    @login_required
    def tournaments():
        # List User's Tournaments
        user_tournaments = Tournament.query.filter_by(user_id=current_user.id).order_by(Tournament.created_at.desc()).all()
        return render_template("tournaments/dashboard_list.html", tournaments=user_tournaments)

    @app.route("/tournaments/create", methods=["GET", "POST"])
    @login_required
    def create_tournament_route():
        if request.method == "POST":
            name = request.form.get("name")
            team_ids = request.form.getlist("team_ids")
            mode = request.form.get("mode", "round_robin")

            if not name or len(team_ids) < 2:
                flash("Please provide a tournament name and select at least 2 teams.", "error")
                return redirect(url_for("create_tournament_route"))

            try:
                # Convert string IDs to int
                team_ids = [int(tid) for tid in team_ids]

                owned_team_ids = {
                    team.id
                    for team in DBTeam.query.filter_by(user_id=current_user.id)
                    .filter(DBTeam.id.in_(team_ids))
                    .all()
                }
                if len(owned_team_ids) != len(team_ids):
                    flash("One or more selected teams are not owned by you.", "error")
                    return redirect(url_for("create_tournament_route"))

                # Handle custom series configuration
                series_config = None
                if mode == "custom_series":
                    if len(team_ids) != 2:
                        flash("Custom series requires exactly 2 teams.", "error")
                        return redirect(url_for("create_tournament_route"))

                    num_matches = int(request.form.get("series_matches", 3))
                    series_config = {
                        "series_name": name,
                        "matches": []
                    }
                    # Alternate home/away for each match
                    for i in range(num_matches):
                        series_config["matches"].append({
                            "match_num": i + 1,
                            "home": i % 2,  # Alternate home team
                            "venue_name": f"Match {i + 1}"
                        })

                # Validate mode requirements
                min_teams = tournament_engine.MIN_TEAMS.get(mode, 2)
                if len(team_ids) < min_teams:
                    flash(f"{mode.replace('_', ' ').title()} requires at least {min_teams} teams.", "error")
                    return redirect(url_for("create_tournament_route"))

                t = tournament_engine.create_tournament(
                    name=name,
                    user_id=current_user.id,
                    team_ids=team_ids,
                    mode=mode,
                    series_config=series_config
                )
                flash(f"Tournament '{name}' created successfully!", "success")
                return redirect(url_for("tournament_dashboard", tournament_id=t.id))

            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("create_tournament_route"))
            except Exception as e:
                app.logger.error(f"Error creating tournament: {e}", exc_info=True)
                flash("An error occurred while creating the tournament.", "error")
                return redirect(url_for("create_tournament_route"))

        # GET - Show form with available modes
        teams = DBTeam.query.filter_by(user_id=current_user.id).filter(
            ~DBTeam.name.in_(["BYE", "TBD"]),
            ~DBTeam.short_code.in_(["BYE", "TBD"])
        ).all()
        num_teams = len(teams)

        # Get available modes based on team count
        available_modes = tournament_engine.get_available_modes(num_teams) if num_teams >= 2 else []

        return render_template("tournaments/create.html", teams=teams, available_modes=available_modes)

    @app.route("/tournaments/<int:tournament_id>")
    @login_required
    def tournament_dashboard(tournament_id):
        t = db.session.get(Tournament, tournament_id)
        if not t or t.user_id != current_user.id:
            return "Tournament not found", 404
            
        # Get Standings (use engine tie-breakers)
        standings = tournament_engine.get_standings(tournament_id)
        
        return render_template("tournaments/dashboard.html", tournament=t, standings=standings)

    @app.route("/tournaments/<int:tournament_id>/delete", methods=["POST"])
    @login_required
    def delete_tournament(tournament_id):
        t = db.session.get(Tournament, tournament_id)
        if t and t.user_id == current_user.id:
            db.session.delete(t)
            db.session.commit()
        return redirect(url_for("tournaments"))


    @app.route("/fixture/<fixture_id>/resimulate", methods=["POST"])
    @login_required
    def resimulate_fixture(fixture_id):
        """
        Resets a fixture to 'Scheduled' state and deletes match data for re-simulation.
        """
        try:
            # 1. Fetch Fixture
            fixture = db.session.get(TournamentFixture, fixture_id)
            if not fixture:
                flash("Fixture not found.", "danger")
                return redirect(url_for('tournaments'))

            # Verify authorization (fixture -> tournament -> user)
            if fixture.tournament.user_id != current_user.id:
                flash("Unauthorized to modify this fixture.", "danger")
                return redirect(url_for('tournament_dashboard', tournament_id=fixture.tournament_id))

            match_id = fixture.match_id
            if not match_id:
                flash("No match data found to reset.", "warning")
                return redirect(url_for('tournament_dashboard', tournament_id=fixture.tournament_id))

            # 2. Reverse DB Stats (if match exists)
            db_match = db.session.get(DBMatch, match_id)
            if db_match:
                app.logger.info(f"Reversing stats for match {match_id}")
                reversed_ok = tournament_engine.reverse_standings(db_match, commit=False)
                if not reversed_ok:
                    # Ensure fixture state is reset even if reverse could not locate it
                    fixture.status = 'Scheduled'
                    fixture.winner_team_id = None
                    fixture.match_id = None
                    fixture.standings_applied = False
                # Clean up dependent rows to avoid FK nulling errors
                db.session.query(MatchPartnership).filter_by(match_id=match_id).delete(synchronize_session=False)
                db.session.query(MatchScorecard).filter_by(match_id=match_id).delete(synchronize_session=False)
                db.session.delete(db_match)
            else:
                 # Fallback: if DBMatch missing but fixture has ID, manually reset fixture
                 fixture.status = 'Scheduled' 
                 fixture.winner_team_id = None
                 fixture.match_id = None
                 fixture.standings_applied = False
            
            # 3. Delete JSON File (FileSystem)
            match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
            for fn in os.listdir(match_dir):
                if fn.endswith(".json"):
                    path = os.path.join(match_dir, fn)
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        if data.get("match_id") == match_id:
                            f.close()
                            os.remove(path)
                            app.logger.info(f"Deleted match JSON: {fn}")
                            break
                    except Exception:
                        continue

            # 4. Clear In-Memory Instance
            if match_id in MATCH_INSTANCES:
                del MATCH_INSTANCES[match_id]

            db.session.commit()
            flash("Match reset successfully. You can now re-simulate.", "success")
            
            # 5. Redirect to Match Setup
            return redirect(url_for('match_setup', fixture_id=fixture.id, tournament_id=fixture.tournament_id))

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Resimulation error: {e}", exc_info=True)
            flash("Failed to reset match.", "danger")
            return redirect(url_for('tournament_dashboard', tournament_id=fixture.tournament_id if fixture else 0))

    # ===== Statistics Hub Routes =====
    
    @app.route("/statistics")
    @login_required
    def statistics():
        """Display statistics dashboard with overall or tournament-specific stats"""
        try:
            # Initialize stats service
            stats_service = StatsService(logger=app.logger)
            
            # Get view type from query params
            view_type = request.args.get('view', 'overall')  # 'overall' or 'tournament'
            tournament_id = request.args.get('tournament_id', type=int)
            
            # Get user's tournaments for selector
            tournaments = Tournament.query.filter_by(user_id=current_user.id).all()
            
            # Initialize empty stats
            stats_data = None
            has_stats = False
            
            # Fetch stats based on view type
            if view_type == 'overall':
                app.logger.info(f"Fetching overall stats for user {current_user.id}")
                stats_data = stats_service.get_overall_stats(current_user.id)
            elif view_type == 'tournament' and tournament_id:
                app.logger.info(f"Fetching tournament stats for user {current_user.id}, tournament {tournament_id}")
                stats_data = stats_service.get_tournament_stats(current_user.id, tournament_id)
            
            # Check if we have data
            if stats_data and (stats_data['batting'] or stats_data['bowling'] or stats_data['fielding']):
                has_stats = True
            
            # Prepare headers for tables
            batting_headers = ['Player', 'Team', 'Matches', 'Innings', 'Runs', 'Balls', 'Not Outs', 
                             'Strike Rate', 'Average', '0s', '1s', '2s', '3s', '4s', '6s', 
                             '30s', '50s', '100s']
            
            bowling_headers = ['Team', 'Player', 'Matches', 'Innings', 'Overs', 'Runs', 'Wickets', 
                             'Best', 'Average', 'Economy', 'Dots', 'Bowled', 'LBW', 'Byes', 'Leg Byes', 
                             'Wides', 'No Balls']
            
            fielding_headers = ['Player', 'Team', 'Matches', 'Catches', 'Run Outs']
            
            # Add best bowling figures leaderboard (top 5 sorted by wickets)
            if stats_data is not None:
                figures_tournament = tournament_id if view_type == 'tournament' else None
                best_figures = stats_service.get_bowling_figures_leaderboard(
                    current_user.id,
                    figures_tournament,
                    limit=5
                )
                stats_data.setdefault('leaderboards', {})
                stats_data['leaderboards']['best_bowling_figures'] = best_figures

            # Insights (Impact, Form, Conditions)
            insights = stats_service.get_insights(
                current_user.id,
                tournament_id if view_type == 'tournament' else None
            ) if stats_data else {}

            return render_template(
                'statistics.html',
                view_type=view_type,
                tournament_id=tournament_id,
                tournaments=tournaments,
                has_stats=has_stats,
                batting_stats=stats_data['batting'] if stats_data else [],
                bowling_stats=stats_data['bowling'] if stats_data else [],
                fielding_stats=stats_data['fielding'] if stats_data else [],
                leaderboards=stats_data['leaderboards'] if stats_data else {},
                insights=insights,
                batting_headers=batting_headers,
                bowling_headers=bowling_headers,
                fielding_headers=fielding_headers,
                user=current_user
            )
            
        except Exception as e:
            app.logger.error(f"Error in statistics route: {e}", exc_info=True)
            flash("Error loading statistics", "danger")
            return render_template('statistics.html', has_stats=False, user=current_user)
    
    @app.route("/statistics/export/<stat_type>/<format_type>")
    @login_required
    def export_statistics(stat_type, format_type):
        """Export statistics to CSV or TXT format"""
        try:
            # Initialize stats service
            stats_service = StatsService(logger=app.logger)
            
            # Get view type and tournament from query params
            view_type = request.args.get('view', 'overall')
            tournament_id = request.args.get('tournament_id', type=int)
            
            # Fetch stats
            if view_type == 'overall':
                stats_data = stats_service.get_overall_stats(current_user.id)
            elif tournament_id:
                stats_data = stats_service.get_tournament_stats(current_user.id, tournament_id)
            else:
                flash("Please select a tournament", "warning")
                return redirect(url_for('statistics'))
            
            # Get the appropriate stats based on type
            if stat_type == 'batting':
                data = stats_data['batting']
            elif stat_type == 'bowling':
                data = stats_data['bowling']
            elif stat_type == 'fielding':
                data = stats_data['fielding']
            else:
                flash("Invalid stat type", "danger")
                return redirect(url_for('statistics'))
            
            if not data:
                flash(f"No {stat_type} data available", "warning")
                return redirect(url_for('statistics'))
            
            # Generate filename
            view_label = f"tournament_{tournament_id}" if view_type == 'tournament' else "overall"
            filename = f"{view_label}_{stat_type}_stats.{format_type}"
            
            # Export based on format
            if format_type == 'csv':
                content = stats_service.export_to_csv(data, stat_type)
                mimetype = 'text/csv'
            elif format_type == 'txt':
                content = stats_service.export_to_txt(data, stat_type)
                mimetype = 'text/plain'
            else:
                flash("Invalid format type", "danger")
                return redirect(url_for('statistics'))
            
            # Create response
            return Response(
                content,
                mimetype=mimetype,
                headers={"Content-Disposition": f"attachment;filename={filename}"}
            )
            
        except Exception as e:
            app.logger.error(f"Error exporting statistics: {e}", exc_info=True)
            flash("Error exporting statistics", "danger")
            return redirect(url_for('statistics'))

    # ============================================================================
    # PLAYER COMPARISON PAGE
    # ============================================================================
    
    @app.route('/compare-players')
    @login_required
    def compare_players_page():
        """Render player comparison page"""
        try:
            # Get all teams and tournaments for the user
            teams = Team.query.filter_by(user_id=current_user.id).all()
            tournaments = Tournament.query.filter_by(user_id=current_user.id).all()
            
            return render_template('compare_players.html', 
                                 teams=teams, 
                                 tournaments=tournaments)
        except Exception as e:
            app.logger.error(f"Error loading comparison page: {e}", exc_info=True)
            flash("Error loading comparison page", "danger")
            return redirect(url_for('index'))

    # ============================================================================
    # NEW FEATURE ROUTES: Stats Enhancements
    # ============================================================================
    
    @app.route('/api/bowling-figures')
    @login_required
    @limiter.limit("30 per minute")
    def api_bowling_figures():
        """
        API endpoint for best bowling figures leaderboard.
        Supports filtering by tournament and limiting results.
        """
        try:
            tournament_id = request.args.get('tournament_id', type=int)
            limit = request.args.get('limit', 10, type=int)
            
            # Validate limit
            if limit < 1 or limit > 100:
                return jsonify({'error': 'Limit must be between 1 and 100'}), 400
            
            stats_service = StatsService(app.logger)
            figures = stats_service.get_bowling_figures_leaderboard(
                current_user.id,
                tournament_id,
                limit
            )
            
            return jsonify({
                'success': True,
                'data': figures,
                'count': len(figures)
            })
            
        except Exception as e:
            app.logger.error(f"Error fetching bowling figures: {e}", exc_info=True)
            return jsonify({'error': 'An internal error occurred'}), 500
    
    @app.route('/api/compare-players')
    @login_required
    @limiter.limit("30 per minute")
    def api_compare_players():
        """
        API endpoint for player comparison.
        Accepts multiple player IDs and optional tournament filter.
        If player_ids is empty or not provided, returns all available players with inferred roles.
        """
        try:
            # Get player IDs from query params
            player_ids_str = request.args.get('player_ids', '')
            player_ids = [int(x.strip()) for x in player_ids_str.split(',') if x.strip().isdigit()]
            tournament_id = request.args.get('tournament_id', type=int)
            
            # If no player IDs provided, return all available players with inferred roles
            if not player_ids:
                stats_service = StatsService(app.logger)

                # Single query: get all players with batting/bowling counts
                players_with_stats = db.session.query(
                    DBPlayer.id,
                    DBPlayer.name,
                    DBTeam.name.label('team_name'),
                    func.sum(
                        db.case((MatchScorecard.record_type == 'batting', 1), else_=0)
                    ).label('batting_count'),
                    func.sum(
                        db.case((MatchScorecard.record_type == 'bowling', 1), else_=0)
                    ).label('bowling_count')
                ).join(
                    MatchScorecard, DBPlayer.id == MatchScorecard.player_id
                ).join(
                    DBMatch, MatchScorecard.match_id == DBMatch.id
                ).join(
                    DBTeam, DBPlayer.team_id == DBTeam.id
                ).filter(
                    DBMatch.user_id == current_user.id
                ).group_by(
                    DBPlayer.id, DBPlayer.name, DBTeam.name
                ).all()

                available_players = []
                for player_id, player_name, team_name, batting_count, bowling_count in players_with_stats:
                    has_batting = (batting_count or 0) > 0
                    has_bowling = (bowling_count or 0) > 0

                    if has_batting and has_bowling:
                        role = 'All-Rounder'
                    elif has_bowling:
                        role = 'Bowler'
                    elif has_batting:
                        role = 'Batsman'
                    else:
                        role = 'Unknown'

                    available_players.append({
                        'id': player_id,
                        'name': player_name,
                        'team': team_name,
                        'role': role
                    })
                
                return jsonify({
                    'success': True,
                    'available_players': available_players,
                    'count': len(available_players)
                })
            
            # If player IDs provided, perform comparison
            if len(player_ids) < 2:
                return jsonify({'error': 'Select at least 2 players to compare'}), 400
            
            if len(player_ids) > 6:
                return jsonify({'error': 'Maximum 6 players can be compared at once'}), 400
            
            stats_service = StatsService(app.logger)
            comparison = stats_service.compare_players(
                current_user.id,
                player_ids,
                tournament_id
            )
            
            if 'error' in comparison:
                return jsonify(comparison), 400

            players = comparison.get('players', [])
            normalized = []
            for p in players:
                normalized.append({
                    'id': p.get('player_id'),
                    'name': p.get('player_name') or p.get('name'),
                    'team': p.get('team_name') or p.get('team'),
                    'matches': p.get('matches', 0),
                    'batting': p.get('batting', {}),
                    'bowling': p.get('bowling', {}),
                    'fielding': p.get('fielding', {})
                })
            
            return jsonify({
                'success': True,
                'data': normalized
            })
            
        except Exception as e:
            app.logger.error(f"Error comparing players: {e}", exc_info=True)
            return jsonify({'error': 'An internal error occurred'}), 500
    
    @app.route('/api/player/<int:player_id>/partnerships')
    @login_required
    @limiter.limit("30 per minute")
    def api_player_partnerships(player_id):
        """
        API endpoint for player partnership statistics.
        Returns comprehensive partnership data for a specific player.
        """
        try:
            tournament_id = request.args.get('tournament_id', type=int)
            
            stats_service = StatsService(app.logger)
            partnership_stats = stats_service.get_player_partnership_stats(
                player_id,
                current_user.id,
                tournament_id
            )
            
            if 'error' in partnership_stats:
                return jsonify(partnership_stats), 400
            
            return jsonify({
                'success': True,
                'data': partnership_stats
            })
            
        except Exception as e:
            app.logger.error(f"Error fetching partnership stats: {e}", exc_info=True)
            return jsonify({'error': 'An internal error occurred'}), 500
    
    @app.route('/api/tournament/<int:tournament_id>/partnerships')
    @login_required
    @limiter.limit("30 per minute")
    def api_tournament_partnerships(tournament_id):
        """
        API endpoint for tournament partnership leaderboard.
        Returns top partnerships in a tournament.
        """
        try:
            limit = request.args.get('limit', 10, type=int)
            
            if limit < 1 or limit > 50:
                return jsonify({'error': 'Limit must be between 1 and 50'}), 400
            
            stats_service = StatsService(app.logger)
            partnerships = stats_service.get_tournament_partnership_leaderboard(
                current_user.id,
                tournament_id,
                limit
            )
            
            return jsonify({
                'success': True,
                'data': partnerships,
                'count': len(partnerships)
            })
            
        except Exception as e:
            app.logger.error(f"Error fetching tournament partnerships: {e}", exc_info=True)
            return jsonify({'error': 'An internal error occurred'}), 500

    @app.route('/api/partnerships')
    @login_required
    @limiter.limit("30 per minute")
    def api_overall_partnerships():
        """
        API endpoint for overall partnership leaderboard.
        Returns top partnerships across all tournaments.
        """
        try:
            limit = request.args.get('limit', 10, type=int)
            
            if limit < 1 or limit > 50:
                return jsonify({'error': 'Limit must be between 1 and 50'}), 400
            
            # Use aliased joins to get both batsman names in a single query
            Batsman1 = aliased(DBPlayer, name='batsman1')
            Batsman2 = aliased(DBPlayer, name='batsman2')

            partnerships = db.session.query(
                MatchPartnership,
                Batsman1.name.label('batsman1_name'),
                Batsman2.name.label('batsman2_name'),
                Tournament.name.label('tournament_name')
            ).join(
                DBMatch, MatchPartnership.match_id == DBMatch.id
            ).join(
                Batsman1, MatchPartnership.batsman1_id == Batsman1.id
            ).join(
                Batsman2, MatchPartnership.batsman2_id == Batsman2.id
            ).join(
                Tournament, DBMatch.tournament_id == Tournament.id
            ).filter(
                DBMatch.user_id == current_user.id
            ).order_by(
                MatchPartnership.runs.desc()
            ).limit(limit).all()

            app.logger.info(f"[Partnerships] Found {len(partnerships)} overall partnership rows (limit={limit}) for user {current_user.id}")

            result = []
            for p, b1_name, b2_name, tourn_name in partnerships:
                result.append({
                    'batsman1': b1_name,
                    'batsman2': b2_name,
                    'runs': p.runs,
                    'balls': p.balls,
                    'wicket': p.wicket_number,
                    'batsman1_contribution': p.batsman1_contribution,
                    'batsman2_contribution': p.batsman2_contribution,
                    'tournament': tourn_name,
                    'match_id': p.match_id
                })
            
            return jsonify({
                'success': True,
                'data': result,
                'count': len(result)
            })
            
        except Exception as e:
            app.logger.error(f"Error fetching overall partnerships: {e}", exc_info=True)
            return jsonify({'error': 'An internal error occurred'}), 500



    return app

# ?????? Run Server ??????
if __name__ == "__main__":
    import socket
    import webbrowser
    import os
    import traceback
    import threading

    try:
        app = create_app()

        # Choose host based on environment
        hostname = socket.gethostname()
        ip_address = socket.gethostbyname(hostname)
        ENV = os.getenv("ENV", "dev").lower()

        is_local = ip_address.startswith("127.") or ENV == "dev"

        HOST = "127.0.0.1" if is_local else "0.0.0.0"
        PORT = 7860
        url = f"http://{HOST}:{PORT}"

        # Console info
        print("[OK] SimCricketX is up and running!")
        print(f"[WEB] Access the app at: {url}")
        print("[INFO] Press Ctrl+C to stop the server.\n")

        # Cleanup tasks
        cleanup_temp_scorecard_images()
        threading.Thread(target=periodic_cleanup, args=(app,), daemon=True).start()

        # Open browser for local use only
        if is_local:
            webbrowser.open_new_tab(url)

        # Run Flask app
        # app.run(
        #     host=HOST,
        #     port=PORT,
        #     debug=is_local,
        #     use_reloader=False  # Important: avoid reloader in prod threads
        # )
        app.run(host="0.0.0.0", port=7860, debug=False, use_reloader=False)

    except Exception as e:
        print("[ERROR] Failed to start SimCricketX:")
        traceback.print_exc()
