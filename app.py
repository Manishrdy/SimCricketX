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
import logging
import yaml
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler
from utils.helpers import load_config
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, send_from_directory, send_file, flash
from engine.match import Match
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    login_required,
    logout_user,
    current_user
)
from auth.user_auth import (
    register_user,
    verify_user,
    delete_user,
    load_credentials
)
from engine.team import Team, save_team, PITCH_PREFERENCES
from engine.player import Player, PLAYER_ROLES, BATTING_HANDS, BOWLING_TYPES, BOWLING_HANDS
import random
import shutil
import time
import threading
import traceback
from auth.user_auth import load_credentials, save_credentials
from werkzeug.utils import secure_filename
from engine.stats_aggregator import StatsAggregator 
import glob
import pandas as pd 
from tabulate import tabulate
from flask import Response

# Add this import for system monitoring
try:
    import psutil
except ImportError:
    psutil = None

from database import db
from database.models import User as DBUser, Team as DBTeam, Player as DBPlayer, Tournament, TournamentTeam, TournamentFixture
from database.models import Match as DBMatch, MatchScorecard # Distinct from engine.match.Match
from engine.tournament_engine import TournamentEngine



MATCH_INSTANCES = {}
tournament_engine = TournamentEngine()

# How old is "too old"? 7 days -> 7*24*3600 seconds
PROD_MAX_AGE = 7 * 24 * 3600

# Make sure PROJECT_ROOT is defined near the top of app.py:
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
CREDENTIALS_FILE = 'auth/credentials.json'

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

class User(UserMixin):
    def __init__(self, email):
        self.id = email


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

    if not secret:
        secret = os.getenv("FLASK_SECRET_KEY", None)
        if not secret:
            secret = os.urandom(24).hex()
            print("[WARN] Using random Flask SECRET_KEY--sessions won't persist across restarts")

    app.config["SECRET_KEY"] = secret
    app.config["SECRET_KEY"] = secret
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # --- Database setup ---
    basedir = os.path.abspath(os.path.dirname(__file__))
    db_path = os.path.join(basedir, 'cricket_sim.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    db.init_app(app)

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

    @login_manager.user_loader
    def load_user(email):
        creds = load_credentials()
        if email in creds:
            return User(email)
        return None

    # --- Request logging ---
    @app.before_request
    def log_request():
        app.logger.info(f"{request.remote_addr} {request.method} {request.path}")

    def _render_statistics_page(user_id):
        try:
            # Query all players for this user
            players = DBPlayer.query.join(DBTeam).filter(DBTeam.user_id == user_id).all()
            
            if not players:
                return render_template("statistics.html", has_stats=False, user=current_user)

            batting_stats = []
            bowling_stats = []
            
            for p in players:
                # Batting Calculation
                innings = p.matches_played # Naive approximation, or track real innings
                dismissals = p.matches_played - p.not_outs 
                # Better dismissal count: we don't have it explicitly stored, 
                # but we updated not_outs only if they played.
                # Let's assume matches_played tracks innings batted? 
                # MatchArchiver increments matches_played for everyone in the scorecard.
                # If they didn't bat, balls=0. 
                # Let's refine MatchArchiver later, but for now use safe math.
                
                avg = p.total_runs / dismissals if dismissals > 0 else p.total_runs
                sr = (p.total_runs / p.total_balls_faced * 100) if p.total_balls_faced > 0 else 0.0
                
                if p.total_balls_faced > 0: # Only include in batting stats if they faced a ball
                    batting_stats.append({
                        "Player": p.name,
                        "Team": p.team.name,
                        "Runs": p.total_runs,
                        "Balls": p.total_balls_faced,
                        "6s": p.total_sixes,
                        "4s": p.total_fours,
                        "Average": round(avg, 2),
                        "Strike Rate": round(sr, 2),
                        "Catches": 0 # Not tracking catches in DB yet? Added schema but assumed 0
                    })

                # Bowling Calculation
                if p.total_balls_bowled > 0:
                    overs = p.total_balls_bowled / 6
                    econ = p.total_runs_conceded / overs if overs > 0 else 0.0
                    
                    bowling_stats.append({
                        "Player": p.name,
                        "Team": p.team.name,
                        "Wickets": p.total_wickets,
                        "Overs": round(overs, 1),
                        "Runs": p.total_runs_conceded,
                        "Economy": round(econ, 2),
                        "Best": f"{p.best_bowling_wickets}/{p.best_bowling_runs}" if p.best_bowling_wickets > 0 else "-"
                    })

            # Leaderboards (Derive from the calculated lists)
            leaderboards = {}
            leaderboards['top_run_scorers'] = sorted(batting_stats, key=lambda x: x['Runs'], reverse=True)[:5]
            leaderboards['top_wicket_takers'] = sorted(bowling_stats, key=lambda x: x['Wickets'], reverse=True)[:5]
            leaderboards['most_sixes'] = sorted(batting_stats, key=lambda x: x['6s'], reverse=True)[:5]
            leaderboards['best_strikers'] = sorted([p for p in batting_stats if p['Balls'] > 20], key=lambda x: x['Strike Rate'], reverse=True)[:5]
            leaderboards['best_economy'] = sorted([p for p in bowling_stats if p['Overs'] > 5], key=lambda x: x['Economy'])[:5]
            leaderboards['best_average'] = sorted([p for p in batting_stats if p['Runs'] > 50], key=lambda x: x['Average'], reverse=True)[:5]
            
            # TODO: Add 'catches' and 'best_figures' proper sorting if needed
            leaderboards['most_catches'] = [] 
            
            # Headers expected by template
            batting_headers = ['Player', 'Team', 'Runs', 'Balls', 'Average', 'Strike Rate', '6s', '4s']
            bowling_headers = ['Player', 'Team', 'Wickets', 'Overs', 'Runs', 'Economy', 'Best']

            return render_template("statistics.html",
                                has_stats=True, user=current_user,
                                batting_stats=batting_stats,
                                bowling_stats=bowling_stats,
                                batting_headers=batting_headers,
                                bowling_headers=bowling_headers,
                                leaderboards=leaderboards,
                                batting_filename="DB_Live_Stats",
                                bowling_filename="DB_Live_Stats")
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
            
            if register_user(email, password):
                return redirect(url_for("login"))
            else:
                return render_template("register.html", error="User already exists")
            
        except Exception as e:
            app.logger.error(f"Registration error: {e}")
            return render_template("register.html", error="System error")


    @app.route("/login", methods=["GET", "POST"])
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
                user = User(email)
                login_user(user)
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
        email = current_user.id
        app.logger.info(f"Account deletion requested for {email}")
        if delete_user(email):
            logout_user()
            return redirect(url_for("register"))
        else:
            return redirect(url_for("home")) # Failed to delete

    @app.route("/logout")
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

                # Validate required fields
                if not (name and short_code and home_ground and pitch):
                    return render_template("team_create.html", error="All team fields are required.")

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
                        player = Player(
                            name=player_names[i],
                            role=roles[i],
                            batting_rating=int(bat_ratings[i]),
                            bowling_rating=int(bowl_ratings[i]),
                            fielding_rating=int(field_ratings[i]),
                            batting_hand=bat_hands[i],
                            bowling_type=bowl_types[i] if bowl_types[i] else "",
                            bowling_hand=bowl_hands[i] if bowl_hands[i] else ""
                        )
                        players.append(player)
                    except Exception as e:
                        app.logger.error(f"Error in player creation: {e}", exc_info=True)
                        return render_template("team_create.html", error=f"Error in player {i+1}: {e}")

                # Validate player count
                if len(players) < 15 or len(players) > 18:
                    return render_template("team_create.html", error="You must enter between 15 and 18 players.")
                
                # Validate at least 1 wicketkeeper
                wk_count = sum(1 for p in players if p.role == "Wicketkeeper")
                if wk_count < 1:
                    return render_template("team_create.html", error="You need at least one Wicketkeeper.")

                # Validate minimum 6 bowlers/all-rounders
                bowl_count = sum(1 for p in players if p.role in ["Bowler", "All-rounder"])
                if bowl_count < 6:
                    return render_template("team_create.html", error="You need at least six Bowler/All-rounder roles.")
                
                # Read team color
                color = request.form["team_color"]

                # For now: auto-pick captain and wicketkeeper as first ones matching
                captain = next((p.name for p in players if p.role in ["Batsman", "All-rounder", "Wicketkeeper"]), players[0].name)
                wicketkeeper = next((p.name for p in players if p.role == "Wicketkeeper"), None)
                if not wicketkeeper:
                    return render_template("team_create.html", error="At least one player must be a Wicketkeeper.")

                # 3. Create and save team to DB
                try:
                    new_team = DBTeam(
                        user_id=current_user.id,
                        name=name,
                        short_code=short_code,
                        home_ground=home_ground,
                        pitch_preference=pitch,
                        team_color=color
                    )
                    db.session.add(new_team)
                    db.session.flush() # Get ID for foreign keys

                    # Add players
                    for p in players:
                        is_captain = (p.name == captain)
                        is_wk = (p.name == wicketkeeper)
                        
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
                    app.logger.info(f"Team '{new_team.name}' (ID: {new_team.id}) created by {current_user.id}")
                    return redirect(url_for("home"))

                except Exception as db_err:
                    db.session.rollback()
                    app.logger.error(f"Database error saving team: {db_err}", exc_info=True)
                    return render_template("team_create.html", error="Database error saving team.")

            except Exception as e:
                app.logger.error(f"Unexpected error saving team '{name}': {e}", exc_info=True)
                return render_template("team_create.html", error=f"Unexpected error saving team: {e}")

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
                    "players": players_list
                })
        except Exception as e:
            app.logger.error(f"Error loading teams from DB: {e}", exc_info=True)
            
        return render_template("manage_teams.html", teams=teams)


    @app.route("/team/delete", methods=["POST"])
    @login_required
    def delete_team():
        short_code = request.form.get("short_code")
        if not short_code:
            return redirect(url_for("manage_teams"))

        try:
            # Find team by short_code and owner
            team = DBTeam.query.filter_by(short_code=short_code, user_id=current_user.id).first()
            
            if not team:
                app.logger.warning(f"Delete failed: Team '{short_code}' not found or unauthorized for {current_user.id}")
                return redirect(url_for("manage_teams"))
            
            # Delete from DB (cascade handles players)
            db.session.delete(team)
            db.session.commit()
            
            app.logger.info(f"Team '{short_code}' (ID: {team.id}) deleted by {current_user.id}")
            
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error deleting team from DB: {e}", exc_info=True)

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
                    
                    # Update Short Code
                    team.short_code = new_short_code

                    # Update Players: Delete all and re-add (Simplest way to handle reordering/edits)
                    # Note: In a real prod app with history, we might soft-delete or diff, 
                    # but for this sim, hard replace is fine as stats link by Player Name/Team internally for now.
                    # Wait, if we link stats by PlayerID, deleting players breaks foreign keys!
                    # For now, let's assuming stats are aggregated by Name/Team string in the current stats engine.
                    # If we use ForeignKeys in MatchScorecard, we CANNOT delete players.
                    # We must diff them.
                    
                    # Correction: For this phase, I will stick to "Delete All & Re-Insert" 
                    # UNLESS foreign keys prevent it. Since we just migrated, we have no matches linking to these NEW player IDs yet.
                    # But future matches will. 
                    # Simpler approach for now: Delete existing players for this team.
                    
                    DBPlayer.query.filter_by(team_id=team.id).delete()
                    
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
                            is_captain=(p_name == captain_name),
                            is_wicketkeeper=(p_name == wk_name)
                        )
                        db.session.add(db_player)

                    db.session.commit()
                    app.logger.info(f"Team '{team.short_code}' (ID: {team.id}) updated by {user_id}")
                    return redirect(url_for("manage_teams"))

                except Exception as e:
                    db.session.rollback()
                    app.logger.error(f"Error updating team: {e}", exc_info=True)
                    # Fallthrough to re-render form with error? For now redirect.
                    return redirect(url_for("manage_teams"))

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
            return redirect(url_for("manage_teams"))

        # GET: render the same form, passing raw JSON and an edit flag
        return render_template("team_create.html", team=raw, edit=True)
    
    
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
                # Prevent starting locked matches
                if fixture.status == 'Locked':
                    flash("Cannot start a locked match. Wait for previous rounds to complete.", "error")
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
            match_id = uuid.uuid4().hex[:8]
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
                "rain_probability": data.get("rain_probability", 0.0)
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

        increment_matches_simulated()
        # Render the detail page, passing the loaded JSON
        return render_template("match_detail.html", match=match_data)
    
    @app.route("/teams/<short_code>/delete", methods=["DELETE"])
    @login_required
    def delete_team_rest(short_code):
        teams_dir = os.path.join(PROJECT_ROOT, "data", "teams")
        filename = f"{short_code}_{current_user.id}.json"
        team_path = os.path.join(teams_dir, filename)

        if not os.path.exists(team_path):
            return jsonify({"error": "Team not found"}), 404

        try:
            os.remove(team_path)
            app.logger.info(f"Team '{short_code}' deleted by {current_user.id}")
            return jsonify({"success": True})
        except Exception as e:
            app.logger.error(f"Error deleting team file: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500


    @app.route("/match/<match_id>/set-toss", methods=["POST"])
    @login_required
    def set_toss(match_id):
        match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
        data = request.get_json()
        toss_winner = data.get("winner")
        decision = data.get("decision")

        # Locate match file by match_id
        for fn in os.listdir(match_dir):
            path = os.path.join(match_dir, fn)
            with open(path, "r") as f:
                match_data = json.load(f)
            if match_data.get("match_id") == match_id:
                match_data["toss_winner"] = toss_winner
                match_data["toss_decision"] = decision
                # Save updated file
                with open(path, "w") as f:
                    json.dump(match_data, f, indent=2)
                app.logger.info(f"[MatchToss] {toss_winner} chose to {decision} (Match: {match_id})")
                return jsonify({"status":"success"}), 200

        return jsonify({"error":"Match not found"}), 404
    
    @app.route("/match/<match_id>/spin-toss")
    @login_required
    def spin_toss(match_id):
        match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
        match_data = None
        match_path = None

        for fn in os.listdir(match_dir):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(match_dir, fn)
            with open(path) as f:
                try:
                    data = json.load(f)
                    if data.get("match_id") == match_id:
                        match_data = data
                        match_path = path
                        break
                except Exception as e:
                    app.logger.error(f"Error reading match file {fn}: {e}")

        if not match_data:
            return jsonify({"error": "Match not found"}), 404

        toss_choice = match_data["toss"]
        toss_result = random.choice(["Heads", "Tails"])

        team_home = match_data["team_home"].split('_')[0]
        team_away = match_data["team_away"].split('_')[0]
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

        # Build complete toss commentary with correct batsmen
        full_commentary = f"{home_captain} spins the coin and {away_captain} calls for {toss_choice}.<br>" \
                        f"{toss_winner} won the toss and choose to {toss_decision} first.<br>" \
                        f"<br>? <strong>Striker:</strong> {batting_team[0]['name']}<br>" \
                        f"? <strong>Non-striker:</strong> {batting_team[1]['name']}"
        
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
                app.logger.info(f"[FinalLineups] Match instance {match_id} not yet in memory. No action needed.")
                return jsonify({"success": True, "message": "Lineups will be loaded from updated file."}), 200

            match_instance = MATCH_INSTANCES[match_id]
            lineup_data = request.get_json()
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
            return jsonify({"error": str(e)}), 500
        
    @app.route("/match/<match_id>/next-ball")
    @login_required
    def next_ball(match_id):
        if match_id not in MATCH_INSTANCES:
            match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
            match_data = None
            for fn in os.listdir(match_dir):
                path = os.path.join(match_dir, fn)
                try:
                    # Add encoding="utf-8" to correctly read files with special characters
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("match_id") == match_id:
                        match_data = data
                        break
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    app.logger.error(f"Error reading or decoding {fn}: {e}")
                    continue # Skip corrupted or invalid files
            if not match_data:
                return jsonify({"error": "Match not found"}), 404
            
            # Reset impact player flags for fresh simulation
            if "impact_players_swapped" in match_data:
                del match_data["impact_players_swapped"]
            if "impact_swaps" in match_data:
                del match_data["impact_swaps"]

            MATCH_INSTANCES[match_id] = Match(match_data)

        match = MATCH_INSTANCES[match_id]
        outcome = match.next_ball()

        # Explicitly send final score and wickets clearly
        if outcome.get("match_over"):
            # If this is a tournament match, autosave to DB and update standings
            if match.data.get("tournament_id"):
                try:
                    app.logger.info(f"[Tournament] Auto-saving match {match_id} for Tournament {match.data.get('tournament_id')}")
                    
                    final_res = outcome.get("result", "Match Ended")
                    match.data["result_description"] = final_res
                    match.data["current_state"] = "completed"
                    
                    # Create DB Match Record
                    db_match = DBMatch(
                        id=match_id,
                        user_id=current_user.id,
                        tournament_id=int(match.data["tournament_id"]),
                        match_json_path="autosaved",
                        result_description=final_res,
                        date=datetime.now()
                    )
                    
                    # Link fixture and update its status
                    fix_id = match.data.get("fixture_id")
                    if fix_id:
                        fixture = db.session.get(TournamentFixture, fix_id)
                        if fixture:
                            db_match.home_team_id = fixture.home_team_id
                            db_match.away_team_id = fixture.away_team_id
                            fixture.match_id = match_id
                            fixture.status = 'Completed'  # Mark fixture as completed
                            app.logger.info(f"[Tournament] Fixture {fix_id} marked as Completed")
                    else:
                        # Fallback if no fixture ID (shouldn't happen in tournament mode)
                        pass

                    # Set Scores from match Instance (with None-safe extraction)
                    db_match.home_team_score = _safe_get_attr(match, 'home_score', 0)
                    db_match.home_team_wickets = _safe_get_attr(match, 'home_wickets', 0)
                    db_match.home_team_overs = _safe_get_attr(match, 'home_overs', 0.0)

                    db_match.away_team_score = _safe_get_attr(match, 'away_score', 0)
                    db_match.away_team_wickets = _safe_get_attr(match, 'away_wickets', 0)
                    db_match.away_team_overs = _safe_get_attr(match, 'away_overs', 0.0)

                    # Determine Winner ID (case-insensitive matching)
                    winner_name = getattr(match, 'winner', None)
                    if winner_name and fixture:
                        winner_name_lower = winner_name.lower().strip()
                        home_name = (fixture.home_team.name or '').lower().strip()
                        home_code = (fixture.home_team.short_code or '').lower().strip()
                        away_name = (fixture.away_team.name or '').lower().strip()
                        away_code = (fixture.away_team.short_code or '').lower().strip()

                        if winner_name_lower in (home_name, home_code):
                            db_match.winner_team_id = fixture.home_team_id
                        elif winner_name_lower in (away_name, away_code):
                            db_match.winner_team_id = fixture.away_team_id
                        else:
                            app.logger.warning(f"[Tournament] Could not match winner '{winner_name}' to teams")

                    db.session.add(db_match)
                    db.session.commit()
                    
                    # Update Standings
                    tournament_engine.update_standings(db_match)
                    app.logger.info(f"[Tournament] Standings updated for Tournament {db_match.tournament_id}")

                except Exception as e:
                    app.logger.error(f"[Tournament] Failed to auto-update tournament: {e}", exc_info=True)

            return jsonify({
                "innings_end":     True,
                "innings_number":  2,
                "match_over":      True,
                "commentary":      outcome.get("commentary", "<b>Match Over!</b>"),
                "scorecard_data":  outcome.get("scorecard_data"),
                "score":           outcome.get("final_score", match.score),
                "wickets":         outcome.get("wickets",  match.wickets),
                "result":          outcome.get("result",  "Match ended")
            })

        # Check for Match End to Auto-Update Tournament
        if outcome.get("match_over"):
            # If this is a tournament match, autosave to DB and update standings
            if match.data.get("tournament_id"):
                try:
                    app.logger.info(f"[Tournament] Auto-saving match {match_id} for Tournament {match.data.get('tournament_id')}")
                    # Create DB Match Record
                    from engine.stats_aggregator import StatsAggregator
                    agg = StatsAggregator(db.session)
                    
                    # We need the full match data, which match instance has updated
                    # But we also need the 'result_description' if possible. 
                    # Outcome usually has 'result' string (e.g. "Sim won by 10 runs")
                    
                    # We can use the existing save_match_to_db logic if we extract it, 
                    # but simple direct call here is safer for now.
                    # We will use StatsAggregator to save it properly.
                    
                    # Wait, StatsAggregator parses JSON files. We have the data in memory 'match.data'
                    # Or we can dump it to file first (which is done by simulator implicitly? No match.data is updated in memory)
                    # Simulator updates match.data? Let's check. 
                    # Usually simulator updates state but maybe not the raw 'match.data' dict perfectly for serialization?
                    # The 'match.data' is usually kept in sync. 
                    
                    # Let's save the file first to be sure
                    match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
                    # Find filename... usually 'match_id.json' or similar if constructed.
                    # Actually we loop listdir to find it usually.
                    # Assuming we can find it or overwrite it.
                    
                    # Better: Pass the match_instance.data to StatsAggregator if we can refactor it? 
                    # StatsAggregator.process_match_data(match_data)
                    
                    # For now, let's manually create the DBMatch object to avoid complexity
                    
                    # 1. Update In-Memory Data with Result
                    final_res = outcome.get("result", "Match Ended")
                    match.data["result_description"] = final_res
                    match.data["current_state"] = "completed"
                    
                    # 2. Persist to DB
                    db_match = DBMatch(
                        id=match_id,
                        user_id=current_user.id,
                        home_team_id=db.session.get(DBTeam, int(match.data['team_home'].split('_')[0])).id if '_' not in str(match.data.get('tournament_id', '')) else None, # Complex ID handling... 
                        # Actually match.data['team_home'] is "CODE_UserID". We need the DB ID.
                        # Wait, we know the fixture logic.
                        # But wait, we stored IDs in 'tournament_id' ? No 'tournament_id' is integer.
                        tournament_id=int(match.data["tournament_id"]),
                        match_json_path="autosaved",
                        result_description=final_res,
                        date=datetime.now()
                    )
                    
                    # Resolve Team IDs from ShortCodes if needed, OR use the fixture info if we have fixture_id
                    fix_id = match.data.get("fixture_id")
                    if fix_id:
                        fixture = db.session.get(TournamentFixture, fix_id)
                        if fixture:
                            db_match.home_team_id = fixture.home_team_id
                            db_match.away_team_id = fixture.away_team_id
                            # Link fixture and mark as completed
                            fixture.match_id = match_id
                            fixture.status = 'Completed'  # Mark fixture as completed
                            app.logger.info(f"[Tournament] Fixture {fix_id} marked as Completed")
                    
                    # Set Scores
                    if match.innings == 2 and match.is_complete:
                        # Assuming Home batted first for simplicity to extract logic, but need to be robust
                        # match.data['innings'] has the list.
                        # Using match instance properties
                        pass # StatsAggregator handles this better.
                    
                    # Extract scores from match instance (with None-safe extraction)
                    db_match.home_team_score = _safe_get_attr(match, 'home_score', 0)
                    db_match.home_team_wickets = _safe_get_attr(match, 'home_wickets', 0)
                    db_match.home_team_overs = _safe_get_attr(match, 'home_overs', 0.0)

                    db_match.away_team_score = _safe_get_attr(match, 'away_score', 0)
                    db_match.away_team_wickets = _safe_get_attr(match, 'away_wickets', 0)
                    db_match.away_team_overs = _safe_get_attr(match, 'away_overs', 0.0)

                    # Determine Winner ID (case-insensitive matching)
                    winner_name = getattr(match, 'winner', None)
                    if winner_name and fixture:
                        winner_name_lower = winner_name.lower().strip()
                        home_name = (fixture.home_team.name or '').lower().strip()
                        home_code = (fixture.home_team.short_code or '').lower().strip()
                        away_name = (fixture.away_team.name or '').lower().strip()
                        away_code = (fixture.away_team.short_code or '').lower().strip()

                        if winner_name_lower in (home_name, home_code):
                            db_match.winner_team_id = fixture.home_team_id
                        elif winner_name_lower in (away_name, away_code):
                            db_match.winner_team_id = fixture.away_team_id
                        else:
                            app.logger.warning(f"[Tournament] Could not match winner '{winner_name}' to teams")
                            
                    db.session.add(db_match)
                    db.session.commit()
                    
                    # UPDATE STANDINGS
                    tournament_engine.update_standings(db_match)
                    app.logger.info(f"[Tournament] Standings updated for Tournament {db_match.tournament_id}")
                    
                except Exception as e:
                    app.logger.error(f"[Tournament] Failed to auto-update tournament: {e}", exc_info=True)


            return jsonify({
                "innings_end":     True,                              # <- flag it as an innings end
                "innings_number":  2,                                 # <- second innings
                "match_over":      True,
                "commentary":      outcome.get("commentary", "<b>Match Over!</b>"),
                "scorecard_data":  outcome.get("scorecard_data"),     # <- your detailed card
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
        
        data = request.get_json()
        first_batting_team = data.get("first_batting_team")
        
        match = MATCH_INSTANCES[match_id]
        result = match.start_super_over(first_batting_team)
        
        return jsonify(result)

    @app.route("/match/<match_id>/next-super-over-ball")
    @login_required
    def next_super_over_ball(match_id):
        if match_id not in MATCH_INSTANCES:
            return jsonify({"error": "Match not found"}), 404
        
        match = MATCH_INSTANCES[match_id]
        try:
            result = match.next_super_over_ball()
            return jsonify(result)
        except Exception as e:
            print(f"Error in super over: {e}")  # Debug print
            return jsonify({"error": str(e)}), 500
    
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
                return jsonify({"error": f"Validation error: {ve}"}), 400
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
        

    @app.route('/download-credentials')
    def download_credentials():
        try:
            return send_file('auth/credentials.json', as_attachment=True)
        except Exception as e:
            return str(e), 500

    @app.route('/credentials', methods=['GET', 'DELETE'])
    def handle_credentials():
        if not os.path.exists(CREDENTIALS_FILE):
            return jsonify({"error": "Credentials file not found"}), 404

        # Load the credentials
        try:
            with open(CREDENTIALS_FILE, 'r') as f:
                credentials = json.load(f)
        except Exception as e:
            return jsonify({"error": f"Failed to load file: {str(e)}"}), 500

        if request.method == 'GET':
            # Return entire credentials file
            return jsonify(credentials), 200

        elif request.method == 'DELETE':
            email = request.args.get('email') or request.json.get('email')
            if not email:
                return jsonify({"error": "Email is required for deletion"}), 400

            if email not in credentials:
                return jsonify({"message": f"No user found with email {email}"}), 404

            deleted = credentials.pop(email)

            try:
                with open(CREDENTIALS_FILE, 'w') as f:
                    json.dump(credentials, f, indent=2)
            except Exception as e:
                return jsonify({"error": f"Failed to write file: {str(e)}"}), 500

            return jsonify({
                "message": f"User {email} deleted successfully",
                "deleted_record": deleted
            }), 200

    @app.route('/match/<match_id>/save-scorecard-images', methods=['POST'])
    def save_scorecard_images(match_id):
        try:
            import os
            from pathlib import Path
            
            # Create temp directory for images
            temp_dir = Path("data") / "temp_scorecard_images"
            temp_dir.mkdir(parents=True, exist_ok=True)
            
            saved_files = []
            
            # Save first innings image if provided
            if 'first_innings_image' in request.files:
                first_img = request.files['first_innings_image']
                if first_img.filename:
                    first_path = temp_dir / f"{match_id}_first_innings_scorecard.png"
                    first_img.save(first_path)
                    saved_files.append(str(first_path))
            
            # Save second innings image if provided  
            if 'second_innings_image' in request.files:
                second_img = request.files['second_innings_image']
                if second_img.filename:
                    second_path = temp_dir / f"{match_id}_second_innings_scorecard.png"
                    second_img.save(second_path)
                    saved_files.append(str(second_path))
            
            return jsonify({
                "success": True,
                "saved_files": saved_files
            })
            
        except Exception as e:
            print(f"Error saving scorecard images: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/statistics")
    @login_required
    def statistics():
        return _render_statistics_page(current_user.id)

    @app.route('/upload_stats', methods=['POST'])
    @login_required
    def upload_stats():
        if 'stats_files' not in request.files:
            return redirect(url_for('statistics'))

        files = request.files.getlist('stats_files')
        if not files or files[0].filename == '':
            return redirect(url_for('statistics'))

        uploaded_filepaths = []
        seen_filenames = set()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        prefix = current_user.id.replace("@", "_").replace(".", "_")

        for file in files:
            if file and allowed_file(file.filename):
                original = secure_filename(file.filename)
                if original in seen_filenames:
                    app.logger.warning(f"[Upload] Duplicate skipped: {original}")
                    continue
                seen_filenames.add(original)

                # Detect file tag
                if 'bat' in original.lower():
                    tag = "batting"
                elif 'bowl' in original.lower():
                    tag = "bowling"
                else:
                    tag = "misc"

                basename = os.path.splitext(original)[0]  # e.g., MI_batting
                filename = f"{prefix}_{basename}_{timestamp}.csv"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                uploaded_filepaths.append(filepath)
                app.logger.debug(f"[DEBUG] Saved file: {filepath}")
            else:
                return redirect(url_for('statistics'))

        try:
            aggregator = StatsAggregator(uploaded_filepaths, current_user.id)
            aggregator.process_and_save()
        except Exception as e:
            app.logger.error(f"Error processing stats for user {current_user.id}: {e}", exc_info=True)
        finally:
            for filepath in uploaded_filepaths:
                if os.path.exists(filepath):
                    os.remove(filepath)

        return _render_statistics_page(current_user.id)

    
    @app.route("/download_stats_csv/<leaderboard>")
    @login_required
    def download_stats_csv(leaderboard):
        try:
            user_id = current_user.get_id()
            stats_dir = "data/stats"
            batting_file = max(glob.glob(os.path.join(stats_dir, f"{user_id}_batting_*.csv")), key=os.path.getctime)
            bowling_file = max(glob.glob(os.path.join(stats_dir, f"{user_id}_bowling_*.csv")), key=os.path.getctime)

            if leaderboard in ["top_run_scorers", "most_sixes", "best_strikers", "most_catches"]:
                df = pd.read_csv(batting_file)
            else:
                df = pd.read_csv(bowling_file)

            if leaderboard == "top_run_scorers":
                data = df.sort_values(by="Runs", ascending=False)[["Player", "Team", "Runs"]]
            elif leaderboard == "most_sixes":
                data = df.sort_values(by="6s", ascending=False)[["Player", "Team", "6s"]]
            elif leaderboard == "most_catches":
                data = df.sort_values(by="Catches", ascending=False)[["Player", "Team", "Catches"]]
            elif leaderboard == "best_strikers":
                filtered = df[df["Balls"] >= 50]
                data = filtered.sort_values(by="Strike Rate", ascending=False)[["Player", "Team", "Strike Rate"]]
            elif leaderboard == "top_wicket_takers":
                data = df.sort_values(by="Wickets", ascending=False)[["Player", "Team", "Wickets"]]
            elif leaderboard == "best_economy":
                df["total_balls"] = df["Overs"].apply(lambda x: int(str(x).split('.')[0]) * 6 + int(str(x).split('.')[1]) if '.' in str(x) else int(x) * 6)
                df["Economy"] = df.apply(lambda row: round(row["Runs"] / (row["total_balls"] / 6), 2) if row["total_balls"] > 0 else 0.00, axis=1)
                filtered = df[df["total_balls"] >= 60]
                data = filtered.sort_values(by="Economy")[["Player", "Team", "Economy"]]
            elif leaderboard == "batting_full":
                df = pd.read_csv(batting_file)
                data = df
            elif leaderboard == "bowling_full":
                df = pd.read_csv(bowling_file)
                data = df
            elif leaderboard == "best_average":
                df = pd.read_csv(batting_file)
                df["Average"] = pd.to_numeric(df["Average"], errors="coerce")
                df = df[df["Average"].notnull()]
                df = df.sort_values(by="Average", ascending=False)[["Player", "Team", "Average"]]
                data = df
            elif leaderboard == "best_figures":
                df = pd.read_csv(bowling_file)
                df["Best"] = df["Best"].astype(str)
                data = df.sort_values(by="Wickets", ascending=False)[["Player", "Team", "Best", "Wickets"]]

            return jsonify(data.to_dict(orient="records"))

        except Exception as e:
            app.logger.error(f"Error fetching stats: {e}")
            return jsonify({"error": "Failed to fetch stats"}), 500

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
        teams = DBTeam.query.filter_by(user_id=current_user.id).all()
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
            
        # Get Standings (Manual Sort for now)
        standings = sorted(t.participating_teams, key=lambda x: (-x.points, -x.net_run_rate))
        
        return render_template("tournaments/dashboard.html", tournament=t, standings=standings)

    @app.route("/tournaments/<int:tournament_id>/delete", methods=["POST"])
    @login_required
    def delete_tournament(tournament_id):
        t = db.session.get(Tournament, tournament_id)
        if t and t.user_id == current_user.id:
            db.session.delete(t)
            db.session.commit()
        return redirect(url_for("tournaments"))

    @app.route("/tournaments/fixtures/<int:fixture_id>/resimulate", methods=["POST"])
    @login_required
    def resimulate_fixture(fixture_id):
        """
        Reset a tournament fixture to allow re-simulation.
        Deletes the associated match data and resets standings.

        All operations are performed in a single transaction to ensure
        data consistency - if any operation fails, everything is rolled back.
        """
        fixture = db.session.get(TournamentFixture, fixture_id)
        if not fixture:
            return "Fixture not found", 404

        # Authorization check
        if fixture.tournament.user_id != current_user.id:
            app.logger.warning(f"Unauthorized resimulate attempt: user {current_user.id} on fixture {fixture_id}")
            return "Unauthorized", 403

        # Check if fixture is actually completed
        if fixture.status != 'Completed':
            return redirect(url_for("tournament_dashboard", tournament_id=fixture.tournament_id))

        tournament_id = fixture.tournament_id

        try:
            # If there's an associated match, reverse standings and delete it
            if fixture.match_id:
                match = db.session.get(Match, fixture.match_id)
                if match:
                    # Reverse the standings update (commit=False for transaction safety)
                    tournament_engine.reverse_standings(match, commit=False)

                    # Delete match scorecards first (foreign key constraint)
                    MatchScorecard.query.filter_by(match_id=fixture.match_id).delete()

                    # Delete the match record
                    db.session.delete(match)

            # Reset fixture status
            fixture.status = 'Scheduled'
            fixture.match_id = None

            # Single commit for all operations - atomic transaction
            db.session.commit()
            app.logger.info(f"Fixture {fixture_id} reset for re-simulation successfully")

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error resimulating fixture {fixture_id}: {e}", exc_info=True)
            return f"Error: {str(e)}", 500

        return redirect(url_for("tournament_dashboard", tournament_id=tournament_id))

    
    @app.route("/download_stats_tab/<leaderboard>")
    @login_required
    def download_stats_tab(leaderboard):
        try:
            stats_dir = "data/stats"
            user_id = current_user.get_id()
            batting_file = max(glob.glob(os.path.join(stats_dir, f"{user_id}_batting_*.csv")), key=os.path.getctime)
            bowling_file = max(glob.glob(os.path.join(stats_dir, f"{user_id}_bowling_*.csv")), key=os.path.getctime)

            # Determine source file
            if leaderboard in ["top_run_scorers", "most_sixes", "best_strikers", "most_catches", "best_average", "batting_full"]:
                df = pd.read_csv(batting_file)
            else:
                df = pd.read_csv(bowling_file)

            # Handle leaderboard logic
            if leaderboard == "top_run_scorers":
                df = df.sort_values(by="Runs", ascending=False)[["Player", "Team", "Runs"]]

            elif leaderboard == "most_sixes":
                df = df.sort_values(by="6s", ascending=False)[["Player", "Team", "6s"]]

            elif leaderboard == "best_strikers":
                df = df[df["Balls"] >= 50].sort_values(by="Strike Rate", ascending=False)[["Player", "Team", "Strike Rate"]]

            elif leaderboard == "most_catches":
                if "Catches" in df.columns:
                    df = df.sort_values(by="Catches", ascending=False)[["Player", "Team", "Catches"]]
                else:
                    df = pd.DataFrame(columns=["Player", "Team", "Catches"])

            elif leaderboard == "best_average":
                df["Average"] = pd.to_numeric(df["Average"], errors="coerce")
                df = df.sort_values(by="Average", ascending=False)[["Player", "Team", "Average"]]


            elif leaderboard == "top_wicket_takers":
                df = df.sort_values(by="Wickets", ascending=False)[["Player", "Team", "Wickets"]]

            elif leaderboard == "best_economy":
                df["total_balls"] = df["Overs"].apply(lambda x: int(str(x).split('.')[0]) * 6 + int(str(x).split('.')[1]) if '.' in str(x) else int(x) * 6)
                df["Economy"] = df.apply(lambda row: round(row["Runs"] / (row["total_balls"] / 6), 2) if row["total_balls"] > 0 else 0.00, axis=1)
                df = df[df["total_balls"] >= 60].sort_values(by="Economy")[["Player", "Team", "Economy"]]

            elif leaderboard == "best_figures":
                df["Best"] = df["Best"].astype(str)
                df = df.sort_values(by="Wickets", ascending=False)[["Player", "Team", "Best"]]

            elif leaderboard in ["batting_full", "bowling_full"]:
                pass

            else:
                return "Leaderboard not found", 404

            text = tabulate(df, headers="keys", tablefmt="pretty")
            return Response(text, mimetype='text/plain')

        except Exception as e:
            app.logger.error(f"Error generating tabular text: {e}", exc_info=True)
            return f"Error generating tabular text: {str(e)}", 500


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

