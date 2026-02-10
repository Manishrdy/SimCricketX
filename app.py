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
import threading  # Bug Fix B2: Add thread safety for MATCH_INSTANCES
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from logging.handlers import RotatingFileHandler
from utils.helpers import load_config
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, send_from_directory, send_file, flash, current_app, has_app_context
from match_archiver import MatchArchiver, find_original_json_file, reverse_player_aggregates
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
    delete_user,
    update_user_email,
    update_user_password,
    log_admin_action
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
from database.models import Match as DBMatch, MatchScorecard, TournamentPlayerStatsCache, MatchPartnership, AdminAuditLog  # Distinct from engine.match.Match
from database.models import FailedLoginAttempt, BlockedIP, ActiveSession
from engine.tournament_engine import TournamentEngine
from sqlalchemy import func, text  # For aggregate functions




MATCH_INSTANCES = {}
MATCH_INSTANCES_LOCK = threading.Lock()  # Bug Fix B2: Thread safety for concurrent access
tournament_engine = TournamentEngine()

# Module-level logger for functions outside create_app() scope
logger = logging.getLogger("SimCricketX")

# Maintenance mode: when True, only admins can access the app
MAINTENANCE_MODE = False
MAINTENANCE_MODE_LOCK = threading.Lock()

# D3: Per-match file locks to prevent JSON read/write races
_match_file_locks = {}
_match_file_locks_meta = threading.Lock()

def _get_match_file_lock(match_id):
    """Return a per-match threading lock (created on first access)."""
    with _match_file_locks_meta:
        if match_id not in _match_file_locks:
            _match_file_locks[match_id] = threading.Lock()
        return _match_file_locks[match_id]

# C3: Simple in-memory rate limiter (no external dependencies)
_rate_limit_store = {}  # {user_id: deque of timestamps}
_rate_limit_lock = threading.Lock()
_backup_scheduler_started = False
_backup_scheduler_lock = threading.Lock()
RUNTIME_FINGERPRINT = "SCX-ADMIN-ROUTE-FIX-20260208-2358"

def rate_limit(max_requests=30, window_seconds=10):
    """Decorator to rate-limit endpoints per user. Admins get 3x the limit (not unlimited)."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            from flask_login import current_user
            user_id = getattr(current_user, 'id', 'anonymous')
            # Admins get a higher limit, not a bypass
            effective_max = max_requests * 3 if getattr(current_user, 'is_admin', False) else max_requests
            now = datetime.now().timestamp()
            with _rate_limit_lock:
                if user_id not in _rate_limit_store:
                    _rate_limit_store[user_id] = deque()
                timestamps = _rate_limit_store[user_id]
                # Remove timestamps outside the window
                cutoff = now - window_seconds
                while timestamps and timestamps[0] < cutoff:
                    timestamps.popleft()
                if len(timestamps) >= effective_max:
                    return jsonify({"error": "Rate limit exceeded. Please slow down."}), 429
                timestamps.append(now)
            return f(*args, **kwargs)
        return wrapped
    return decorator

# How old is "too old"? 7 days -> 7*24*3600 seconds
PROD_MAX_AGE = 7 * 24 * 3600

# Make sure PROJECT_ROOT is defined near the top of app.py:
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))

def get_matches_simulated():
    from database.models import SiteCounter
    try:
        row = db.session.get(SiteCounter, 'matches_simulated')
        return row.value if row else 0
    except Exception:
        return 0

def increment_matches_simulated():
    from database.models import SiteCounter
    try:
        row = db.session.get(SiteCounter, 'matches_simulated')
        if row:
            row.value += 1
        else:
            db.session.add(SiteCounter(key='matches_simulated', value=1))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[ERROR] Failed to increment matches_simulated: {e}")

def get_visit_counter():
    from database.models import SiteCounter
    try:
        row = db.session.get(SiteCounter, 'total_visits')
        return row.value if row else 0
    except Exception:
        return 0

def increment_visit_counter():
    from database.models import SiteCounter
    try:
        row = db.session.get(SiteCounter, 'total_visits')
        if row:
            row.value += 1
        else:
            db.session.add(SiteCounter(key='total_visits', value=1))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[ERROR] Could not increment visit count: {e}")


def clean_old_archives(max_age_seconds=PROD_MAX_AGE):
    """
    Walk through PROJECT_ROOT/data/, find any .zip files,
    and delete those whose modification time is older than max_age_seconds.
    """
    data_dir = os.path.join(PROJECT_ROOT, "data")
    now = time.time()

    if not os.path.isdir(data_dir):
        logger.warning(f"clean_old_archives: data directory does not exist: {data_dir}")
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
                logger.info(f"Deleted old archive: {filename} (age {age//3600}h)")
            except Exception as e:
                logger.error(f"Failed to delete {full_path}: {e}", exc_info=True)


def load_match_metadata(match_id):
    """
    Look in data/matches for a JSON whose "match_id" field equals match_id.
    Return the parsed dict if found, else None.
    """
    matches_dir = os.path.join(PROJECT_ROOT, "data", "matches")

    # D1: O(1) direct lookup first
    direct_path = os.path.join(matches_dir, f"match_{match_id}.json")
    if os.path.isfile(direct_path):
        try:
            with open(direct_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Fallback: O(N) scan for legacy files
    if not os.path.isdir(matches_dir):
        return None
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
    """Clean up old match instances from memory and orphaned JSON files"""
    try:
        current_time = time.time()
        cutoff_time = current_time - (7 * 24 * 3600)  # 7 days ago

        # Phase 1: Clean up old in-memory match instances
        with MATCH_INSTANCES_LOCK:
            instances_to_remove = []
            for match_id, instance in MATCH_INSTANCES.items():
                instance_time = getattr(instance, 'created_at', current_time)
                if instance_time < cutoff_time:
                    instances_to_remove.append(match_id)

            for match_id in instances_to_remove:
                del MATCH_INSTANCES[match_id]
                app.logger.info(f"[Cleanup] Removed old match instance: {match_id}")

        if instances_to_remove:
            app.logger.info(f"[Cleanup] Cleaned up {len(instances_to_remove)} old match instances")

        # Phase 2: Clean up orphaned JSON files older than 24 hours
        # These are temp files from matches that were never archived or failed to clean up
        match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
        json_cutoff = current_time - (24 * 3600)  # 24 hours
        if os.path.isdir(match_dir):
            removed_files = 0
            for fn in os.listdir(match_dir):
                if not fn.endswith(".json") or fn.startswith("_temp_"):
                    continue
                path = os.path.join(match_dir, fn)
                try:
                    if os.path.getmtime(path) < json_cutoff:
                        os.remove(path)
                        removed_files += 1
                        app.logger.info(f"[Cleanup] Removed orphaned JSON: {fn}")
                except Exception as e:
                    app.logger.warning(f"[Cleanup] Failed to remove {fn}: {e}")
            if removed_files:
                app.logger.info(f"[Cleanup] Cleaned up {removed_files} orphaned JSON files")

    except Exception as e:
        # app.logger.error(f"[Cleanup] Error cleaning up match instances: {e}", exc_info=True)
        pass

def periodic_cleanup(app):
    """Run cleanup every 6 hours"""
    while True:
        try:
            time.sleep(6 * 3600)  # 6 hours
            cleanup_old_match_instances(app)
        except Exception as e:
            app.logger.error(f"[PeriodicCleanup] Error in cleanup thread: {e}")


def cleanup_temp_scorecard_images(logger=None):
    """
    Clean up temporary scorecard images folder before starting a new match.
    Removes the entire temp_scorecard_images folder if it exists.
    """
    temp_images_dir = os.path.join(PROJECT_ROOT, "data", "temp_scorecard_images")

    log = logger
    if log is None:
        log = current_app.logger if has_app_context() else logging.getLogger("SimCricketX")

    try:
        if os.path.exists(temp_images_dir) and os.path.isdir(temp_images_dir):
            shutil.rmtree(temp_images_dir)
            log.info(f"[Cleanup] Removed temp scorecard images directory: {temp_images_dir}")
        else:
            log.debug(f"[Cleanup] Temp scorecard images directory does not exist: {temp_images_dir}")
    except Exception as e:
        log.error(f"[Cleanup] Error removing temp scorecard images directory: {e}", exc_info=True)



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
    global _backup_scheduler_started, MAINTENANCE_MODE
    # --- Flask setup ---
    app = Flask(__name__)
    config = load_config()

    # Load maintenance mode from config (persists across restarts)
    MAINTENANCE_MODE = bool(config.get("app", {}).get("maintenance_mode", False))

    @app.context_processor
    def inject_route_helpers():
        def has_endpoint(endpoint_name):
            return endpoint_name in app.view_functions
        return {"has_endpoint": has_endpoint, "maintenance_mode": MAINTENANCE_MODE}

    @app.before_request
    def check_maintenance_mode():
        """Block non-admin users when maintenance mode is active."""
        if not MAINTENANCE_MODE:
            return None
        # Always allow static files
        if request.path.startswith('/static'):
            return None
        # Allow login/logout so admin can authenticate
        if request.endpoint in ('login', 'logout', 'static'):
            return None
        # Allow admin users through
        if current_user.is_authenticated and getattr(current_user, 'is_admin', False):
            return None
        # Everyone else sees the maintenance page
        return render_template('maintenance.html'), 503

    @app.before_request
    def check_ip_blocklist():
        """Block requests from banned IPs."""
        if request.path.startswith('/static'):
            return None
        try:
            blocked = BlockedIP.query.filter_by(ip_address=request.remote_addr).first()
            if blocked:
                return jsonify({"error": "Access denied"}), 403
        except Exception:
            pass
        return None

    @app.before_request
    def check_force_password_reset():
        """Redirect users who must change their password."""
        if not current_user.is_authenticated:
            return None
        if request.path.startswith('/static'):
            return None
        if request.endpoint in ('force_change_password', 'logout', 'static'):
            return None
        if getattr(current_user, 'force_password_reset', False):
            session['force_password_reset'] = True
            return redirect(url_for('force_change_password'))
        return None

    @app.before_request
    def update_session_activity():
        """Update last_active timestamp for session tracking."""
        token = session.get('session_token')
        if token and current_user.is_authenticated:
            try:
                active = ActiveSession.query.filter_by(session_token=token).first()
                if active:
                    active.last_active = datetime.utcnow()
                    db.session.commit()
                else:
                    # Session record missing (e.g. DB was wiped) — skip
                    pass
            except Exception:
                db.session.rollback()
        return None

    @app.before_request
    def configure_session_cookie():
        is_secure = request.is_secure or (request.headers.get('X-Forwarded-Proto') == 'https')
        app.config["SESSION_COOKIE_SECURE"] = is_secure
        app.logger.info(
            f"[Session:{RUNTIME_FINGERPRINT}] Setting SESSION_COOKIE_SECURE to {is_secure} "
            f"(HTTPS: {request.scheme}, X-Forwarded-Proto: {request.headers.get('X-Forwarded-Proto')})"
        )

    @app.after_request
    def add_runtime_fingerprint_header(response):
        response.headers["X-SimCricketX-Build"] = RUNTIME_FINGERPRINT
        return response

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
            ensure_schema(db.engine, db)
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

    # Attach logger to app with its own handlers
    app.logger = logging.getLogger("SimCricketX")
    app.logger.setLevel(logging.DEBUG)  # You can change to INFO for production
    app.logger.addHandler(file_handler)
    app.logger.addHandler(console_handler)
    app.logger.propagate = False  # Prevent duplicate logs via root logger

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

        # D1: O(1) direct lookup by match_id filename first
        direct_path = os.path.join(match_dir, f"match_{match_id}.json")
        if os.path.isfile(direct_path):
            try:
                with open(direct_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("created_by") != current_user.id:
                    return None, None, (jsonify({"error": "Unauthorized"}), 403)
                return data, direct_path, None
            except Exception as e:
                app.logger.error(f"[MatchAuth] error loading match_{match_id}.json: {e}", exc_info=True)

        # Fallback: O(N) scan for legacy files (old naming convention)
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

    # --- Admin Access Control Decorator ---
    def admin_required(f):
        """Decorator to require admin access for a route"""
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('login'))
            if not getattr(current_user, 'is_admin', False):
                app.logger.warning(f"[Admin] Unauthorized access attempt by {current_user.id}")
                return jsonify({"error": "Forbidden: Admin access required"}), 403
            return f(*args, **kwargs)
        return decorated_function

    # --- Backup token brute-force protection ---
    _backup_token_attempts = {}  # {user_id: deque of timestamps}
    _backup_token_lock = threading.Lock()

    def _check_backup_rate_limit(user_id, max_attempts=3, window_seconds=60):
        """Returns True if the user is rate-limited on backup attempts."""
        now = datetime.now().timestamp()
        with _backup_token_lock:
            if user_id not in _backup_token_attempts:
                _backup_token_attempts[user_id] = deque()
            attempts = _backup_token_attempts[user_id]
            cutoff = now - window_seconds
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            if len(attempts) >= max_attempts:
                return True
            attempts.append(now)
            return False

    # --- Scheduled backup management ---
    BACKUP_DIR = os.path.join(PROJECT_ROOT, "data", "backups")
    os.makedirs(BACKUP_DIR, exist_ok=True)

    def _run_scheduled_backup():
        """Create a scheduled backup copy of the database."""
        try:
            src = os.path.join(basedir, 'cricket_sim.db')
            if not os.path.exists(src):
                return
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dst = os.path.join(BACKUP_DIR, f"scheduled_backup_{ts}.db")
            shutil.copy2(src, dst)
            app.logger.info(f"[Backup] Scheduled backup created: {dst}")
            # Clean up backups older than 7 days
            _cleanup_old_backups()
        except Exception as e:
            app.logger.error(f"[Backup] Scheduled backup failed: {e}")

    def _cleanup_old_backups(max_age_days=7):
        """Delete backup files older than max_age_days."""
        cutoff = time.time() - (max_age_days * 86400)
        try:
            for fn in os.listdir(BACKUP_DIR):
                if not fn.endswith('.db'):
                    continue
                path = os.path.join(BACKUP_DIR, fn)
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    app.logger.info(f"[Backup] Deleted old backup: {fn}")
        except Exception as e:
            app.logger.error(f"[Backup] Cleanup failed: {e}")

    def _backup_scheduler():
        """Background thread: run backup every 24 hours."""
        while True:
            try:
                time.sleep(86400)  # 24 hours
                with app.app_context():
                    _run_scheduled_backup()
            except Exception as e:
                app.logger.error(f"[Backup] Scheduler error: {e}")

    # Start backup scheduler + initial backup only once per process.
    with _backup_scheduler_lock:
        if not _backup_scheduler_started:
            backup_thread = threading.Thread(target=_backup_scheduler, daemon=True)
            backup_thread.start()

            try:
                _run_scheduled_backup()
            except Exception:
                pass

            _backup_scheduler_started = True
        else:
            app.logger.info("[Backup] Scheduler already initialized; skipping duplicate startup backup")

    # --- Admin Routes ---

    @app.route('/admin/backup-database', methods=['POST'])
    @login_required
    @admin_required
    def backup_database():
        """Download database backup (admin only, requires token). Uses POST to prevent CSRF."""
        try:
            # Brute-force protection: 3 attempts per minute
            if _check_backup_rate_limit(current_user.id):
                app.logger.warning(f"[Admin] Backup rate limit hit by {current_user.id}")
                return jsonify({"error": "Too many attempts. Please wait 60 seconds."}), 429

            # Get token from POST body
            token = request.form.get('token', '').strip()

            # Load backup token from config (env var takes priority)
            expected_token = os.environ.get('BACKUP_TOKEN', '')
            if not expected_token:
                backup_config = config.get('backup', {})
                expected_token = str(backup_config.get('token', ''))

            if not expected_token or expected_token in ['CHANGE_ME', 'your_backup_token_here', '']:
                app.logger.error("[Admin] Backup token not configured")
                return jsonify({"error": "Backup not configured. Set BACKUP_TOKEN env var or config.yaml"}), 503

            # Verify token
            if token != expected_token:
                app.logger.warning(f"[Admin] Invalid backup token attempt by {current_user.id}")
                return jsonify({"error": "Invalid backup token"}), 403

            # Create a temporary copy to avoid exposing DB path
            src_path = os.path.join(basedir, 'cricket_sim.db')
            if not os.path.exists(src_path):
                return jsonify({"error": "Database file not found"}), 404

            import tempfile
            backup_name = f'cricket_sim_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
            tmp_dir = tempfile.mkdtemp()
            tmp_path = os.path.join(tmp_dir, backup_name)
            shutil.copy2(src_path, tmp_path)

            app.logger.info(f"[Admin] Database backup downloaded by {current_user.id}")
            log_admin_action(current_user.id, 'backup_database', None, 'Database backup downloaded', request.remote_addr)

            return send_file(
                tmp_path,
                as_attachment=True,
                download_name=backup_name,
                mimetype='application/x-sqlite3'
            )

        except Exception as e:
            app.logger.error(f"[Admin] Database backup failed: {e}", exc_info=True)
            return jsonify({"error": "Backup failed"}), 500

    @app.route('/admin/dashboard')
    @login_required
    @admin_required
    def admin_dashboard():
        """Admin dashboard home"""
        try:
            stats = {}
            stats['total_users'] = db.session.query(DBUser).count()
            stats['total_teams'] = db.session.query(DBTeam).count()
            stats['total_matches'] = db.session.query(DBMatch).count()
            stats['total_tournaments'] = db.session.query(Tournament).count()

            # Database size
            db_path = os.path.join(basedir, 'cricket_sim.db')
            if os.path.exists(db_path):
                stats['db_size_mb'] = round(os.path.getsize(db_path) / (1024 * 1024), 2)
            else:
                stats['db_size_mb'] = 0

            # Active users (logged in last 7 days)
            seven_days_ago = datetime.utcnow() - timedelta(days=7)
            stats['active_users_7d'] = db.session.query(DBUser).filter(DBUser.last_login >= seven_days_ago).count()

            # Active match instances
            with MATCH_INSTANCES_LOCK:
                stats['active_matches'] = len(MATCH_INSTANCES)

            # Recent activity from audit log
            recent_audit = db.session.query(AdminAuditLog).order_by(AdminAuditLog.timestamp.desc()).limit(10).all()
            audit_entries = []
            for entry in recent_audit:
                time_diff = datetime.utcnow() - entry.timestamp if entry.timestamp else None
                if time_diff:
                    if time_diff.days > 0:
                        time_str = f"{time_diff.days}d ago"
                    elif time_diff.seconds > 3600:
                        time_str = f"{time_diff.seconds // 3600}h ago"
                    else:
                        time_str = f"{max(1, time_diff.seconds // 60)}m ago"
                else:
                    time_str = "just now"
                audit_entries.append({
                    'admin': entry.admin_email,
                    'action': entry.action.replace('_', ' ').title(),
                    'target': entry.target or '',
                    'time': time_str
                })

            # Recent user logins
            recent_users = db.session.query(DBUser).order_by(DBUser.last_login.desc()).limit(10).all()
            recent_activity = []
            for user in recent_users:
                if user.last_login:
                    time_diff = datetime.utcnow() - user.last_login
                    if time_diff.days > 0:
                        time_str = f"{time_diff.days}d ago"
                    elif time_diff.seconds > 3600:
                        time_str = f"{time_diff.seconds // 3600}h ago"
                    else:
                        time_str = f"{max(1, time_diff.seconds // 60)}m ago"
                    recent_activity.append({
                        'email': user.id,
                        'action': 'logged in',
                        'time': time_str
                    })

            return render_template('admin/dashboard.html',
                                   stats=stats,
                                   recent_activity=recent_activity,
                                   audit_entries=audit_entries)
        except Exception as e:
            app.logger.error(f"[Admin] Dashboard error: {e}", exc_info=True)
            return "Error loading dashboard", 500

    @app.route('/admin/users')
    @login_required
    @admin_required
    def admin_users():
        """List all users"""
        try:
            users = db.session.query(DBUser).all()
            user_data = []
            for user in users:
                user_data.append({
                    'email': user.id,
                    'display_name': user.display_name,
                    'is_admin': user.is_admin,
                    'teams_count': db.session.query(DBTeam).filter_by(user_id=user.id).count(),
                    'matches_count': db.session.query(DBMatch).filter_by(user_id=user.id).count(),
                    'last_login': user.last_login
                })

            return render_template('admin/users_list.html', users=user_data)
        except Exception as e:
            app.logger.error(f"[Admin] Users list error: {e}", exc_info=True)
            return "Error loading users", 500

    @app.route('/admin/users/<user_email>')
    @login_required
    @admin_required
    def admin_user_detail(user_email):
        """View user details"""
        try:
            user = db.session.get(DBUser, user_email)
            if not user:
                return "User not found", 404

            teams = db.session.query(DBTeam).filter_by(user_id=user_email).all()
            matches = db.session.query(DBMatch).filter_by(user_id=user_email).all()
            sessions = db.session.query(ActiveSession).filter_by(user_id=user_email).order_by(ActiveSession.login_at.desc()).all()

            return render_template('admin/user_detail.html', user=user, teams=teams, matches=matches, sessions=sessions)
        except Exception as e:
            app.logger.error(f"[Admin] User detail error: {e}", exc_info=True)
            return "Error loading user", 500

    @app.route('/admin/users/<user_email>/change-email', methods=['POST'])
    @login_required
    @admin_required
    def admin_change_email(user_email):
        """Change user email"""
        try:
            new_email = request.form.get('new_email', '').strip()
            if not new_email:
                return jsonify({"error": "New email is required"}), 400

            success, message = update_user_email(user_email, new_email, current_user.id)
            if success:
                return jsonify({"message": message}), 200
            else:
                return jsonify({"error": message}), 400
        except Exception as e:
            app.logger.error(f"[Admin] Change email error: {e}", exc_info=True)
            return jsonify({"error": "Failed to change email"}), 500

    @app.route('/admin/users/<user_email>/reset-password', methods=['POST'])
    @login_required
    @admin_required
    def admin_reset_password(user_email):
        """Reset user password"""
        try:
            new_password = request.form.get('new_password', '')
            if not new_password:
                return jsonify({"error": "New password is required"}), 400

            success, message = update_user_password(user_email, new_password, current_user.id)
            if success:
                return jsonify({"message": message}), 200
            else:
                return jsonify({"error": message}), 400
        except Exception as e:
            app.logger.error(f"[Admin] Reset password error: {e}", exc_info=True)
            return jsonify({"error": "Failed to reset password"}), 500

    @app.route('/admin/users/<user_email>/delete', methods=['POST'])
    @login_required
    @admin_required
    def admin_delete_user(user_email):
        """Delete user (cannot delete admin or self)"""
        try:
            if user_email == current_user.id:
                return jsonify({"error": "Cannot delete your own account"}), 400

            target = db.session.get(DBUser, user_email)
            if target and target.is_admin:
                return jsonify({"error": "Cannot delete an admin account"}), 400

            success = delete_user(user_email, current_user.id)
            if success:
                return jsonify({"message": f"User {user_email} deleted successfully"}), 200
            else:
                return jsonify({"error": "Failed to delete user"}), 400
        except Exception as e:
            app.logger.error(f"[Admin] Delete user error: {e}", exc_info=True)
            return jsonify({"error": "Failed to delete user"}), 500

    @app.route('/admin/database/stats')
    @login_required
    @admin_required
    def admin_database_stats():
        """Database statistics"""
        try:
            stats = {}

            db_path = os.path.join(basedir, 'cricket_sim.db')
            if os.path.exists(db_path):
                stats['db_size_mb'] = round(os.path.getsize(db_path) / (1024 * 1024), 2)
            else:
                stats['db_size_mb'] = 0

            from sqlalchemy import inspect as sa_inspect
            inspector = sa_inspect(db.engine)
            stats['total_tables'] = len(inspector.get_table_names())

            stats['total_users'] = db.session.query(DBUser).count()
            stats['total_teams'] = db.session.query(DBTeam).count()
            stats['total_matches'] = db.session.query(DBMatch).count()
            stats['total_tournaments'] = db.session.query(Tournament).count()

            return render_template('admin/database_stats.html', stats=stats)
        except Exception as e:
            app.logger.error(f"[Admin] Database stats error: {e}", exc_info=True)
            return "Error loading database stats", 500

    @app.route('/admin/database/optimize', methods=['POST'])
    @login_required
    @admin_required
    def admin_optimize_database():
        """Optimize database (VACUUM)"""
        try:
            db.session.execute(text('VACUUM'))
            db.session.commit()
            app.logger.info(f"[Admin] Database optimized by {current_user.id}")
            log_admin_action(current_user.id, 'optimize_db', None, 'Database VACUUM executed', request.remote_addr)
            return jsonify({"message": "Database optimized successfully"}), 200
        except Exception as e:
            app.logger.error(f"[Admin] Database optimize error: {e}", exc_info=True)
            return jsonify({"error": "Failed to optimize database"}), 500

    # --- User Activity Dashboard ---
    @app.route('/admin/activity')
    @login_required
    @admin_required
    def admin_activity():
        """User activity dashboard with signup/login trends"""
        try:
            from sqlalchemy import func as sa_func

            # Signups per day (last 30 days)
            thirty_days_ago = datetime.utcnow() - timedelta(days=30)
            signups_raw = db.session.query(
                sa_func.date(DBUser.created_at).label('day'),
                sa_func.count(DBUser.id).label('count')
            ).filter(DBUser.created_at >= thirty_days_ago).group_by(sa_func.date(DBUser.created_at)).all()

            signups_data = {str(row.day): row.count for row in signups_raw}

            # Logins per day (last 30 days)
            logins_raw = db.session.query(
                sa_func.date(DBUser.last_login).label('day'),
                sa_func.count(DBUser.id).label('count')
            ).filter(DBUser.last_login >= thirty_days_ago).group_by(sa_func.date(DBUser.last_login)).all()

            logins_data = {str(row.day): row.count for row in logins_raw}

            # Matches per day (last 30 days)
            matches_raw = db.session.query(
                sa_func.date(DBMatch.date).label('day'),
                sa_func.count(DBMatch.id).label('count')
            ).filter(DBMatch.date >= thirty_days_ago).group_by(sa_func.date(DBMatch.date)).all()

            matches_data = {str(row.day): row.count for row in matches_raw}

            # Build 30-day date list
            days = []
            for i in range(30, -1, -1):
                d = (datetime.utcnow() - timedelta(days=i)).strftime('%Y-%m-%d')
                days.append(d)

            chart_data = {
                'labels': days,
                'signups': [signups_data.get(d, 0) for d in days],
                'logins': [logins_data.get(d, 0) for d in days],
                'matches': [matches_data.get(d, 0) for d in days],
            }

            # Top users by matches
            top_users = db.session.query(
                DBUser.id,
                sa_func.count(DBMatch.id).label('match_count')
            ).outerjoin(DBMatch, DBUser.id == DBMatch.user_id).group_by(DBUser.id).order_by(sa_func.count(DBMatch.id).desc()).limit(10).all()

            # Audit log
            audit_log = db.session.query(AdminAuditLog).order_by(AdminAuditLog.timestamp.desc()).limit(50).all()

            return render_template('admin/activity.html',
                                   chart_data=json.dumps(chart_data),
                                   top_users=top_users,
                                   audit_log=audit_log)
        except Exception as e:
            app.logger.error(f"[Admin] Activity page error: {e}", exc_info=True)
            return "Error loading activity", 500

    # --- System Health Page ---
    @app.route('/admin/health')
    @login_required
    @admin_required
    def admin_health():
        """System health overview"""
        try:
            health = {}

            # Disk usage
            db_path = os.path.join(basedir, 'cricket_sim.db')
            if os.path.exists(db_path):
                health['db_size_mb'] = round(os.path.getsize(db_path) / (1024 * 1024), 2)
            else:
                health['db_size_mb'] = 0

            # Data directory size
            data_dir = os.path.join(PROJECT_ROOT, "data")
            total_data_size = 0
            if os.path.isdir(data_dir):
                for dirpath, dirnames, filenames in os.walk(data_dir):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        total_data_size += os.path.getsize(fp)
            health['data_dir_mb'] = round(total_data_size / (1024 * 1024), 2)

            # Active match instances
            with MATCH_INSTANCES_LOCK:
                health['active_matches'] = len(MATCH_INSTANCES)

            # Memory usage (if psutil available)
            if psutil:
                process = psutil.Process()
                mem = process.memory_info()
                health['memory_mb'] = round(mem.rss / (1024 * 1024), 1)
                health['cpu_percent'] = process.cpu_percent(interval=0.1)

                disk = psutil.disk_usage(basedir)
                health['disk_total_gb'] = round(disk.total / (1024**3), 1)
                health['disk_used_gb'] = round(disk.used / (1024**3), 1)
                health['disk_free_gb'] = round(disk.free / (1024**3), 1)
                health['disk_percent'] = disk.percent
            else:
                health['memory_mb'] = 'N/A'
                health['cpu_percent'] = 'N/A'
                health['disk_total_gb'] = 'N/A'
                health['disk_used_gb'] = 'N/A'
                health['disk_free_gb'] = 'N/A'
                health['disk_percent'] = 'N/A'

            # Backup status
            backups = []
            if os.path.isdir(BACKUP_DIR):
                for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
                    if fn.endswith('.db'):
                        path = os.path.join(BACKUP_DIR, fn)
                        backups.append({
                            'name': fn,
                            'size_mb': round(os.path.getsize(path) / (1024 * 1024), 2),
                            'date': datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M')
                        })
            health['backups'] = backups[:10]
            health['backup_count'] = len(backups)

            # Log file size
            log_path = os.path.join(PROJECT_ROOT, "logs", "execution.log")
            if os.path.exists(log_path):
                health['log_size_mb'] = round(os.path.getsize(log_path) / (1024 * 1024), 2)
            else:
                health['log_size_mb'] = 0

            # Uptime (approx from process start)
            if psutil:
                create_time = process.create_time()
                uptime_seconds = time.time() - create_time
                hours = int(uptime_seconds // 3600)
                minutes = int((uptime_seconds % 3600) // 60)
                health['uptime'] = f"{hours}h {minutes}m"
            else:
                health['uptime'] = 'N/A'

            return render_template('admin/health.html', health=health)
        except Exception as e:
            app.logger.error(f"[Admin] Health page error: {e}", exc_info=True)
            return "Error loading health page", 500

    # --- Backup Management ---
    @app.route('/admin/backups')
    @login_required
    @admin_required
    def admin_backups():
        """List and manage database backups"""
        try:
            backups = []
            if os.path.isdir(BACKUP_DIR):
                for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
                    if fn.endswith('.db'):
                        path = os.path.join(BACKUP_DIR, fn)
                        backups.append({
                            'name': fn,
                            'size_mb': round(os.path.getsize(path) / (1024 * 1024), 2),
                            'date': datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M:%S'),
                            'age_days': round((time.time() - os.path.getmtime(path)) / 86400, 1)
                        })
            return render_template('admin/backups.html', backups=backups)
        except Exception as e:
            app.logger.error(f"[Admin] Backups page error: {e}", exc_info=True)
            return "Error loading backups", 500

    @app.route('/admin/backups/create', methods=['POST'])
    @login_required
    @admin_required
    def admin_create_backup():
        """Manually trigger a backup"""
        try:
            _run_scheduled_backup()
            log_admin_action(current_user.id, 'create_backup', None, 'Manual backup created', request.remote_addr)
            return jsonify({"message": "Backup created successfully"}), 200
        except Exception as e:
            app.logger.error(f"[Admin] Manual backup error: {e}", exc_info=True)
            return jsonify({"error": "Failed to create backup"}), 500

    @app.route('/admin/backups/<filename>/download')
    @login_required
    @admin_required
    def admin_download_backup(filename):
        """Download a specific backup file"""
        try:
            safe_name = secure_filename(filename)
            path = os.path.join(BACKUP_DIR, safe_name)
            if not os.path.exists(path):
                return jsonify({"error": "Backup not found"}), 404
            return send_file(path, as_attachment=True, download_name=safe_name, mimetype='application/x-sqlite3')
        except Exception as e:
            return jsonify({"error": "Download failed"}), 500

    @app.route('/admin/backups/<filename>/delete', methods=['POST'])
    @login_required
    @admin_required
    def admin_delete_backup(filename):
        """Delete a specific backup file"""
        try:
            safe_name = secure_filename(filename)
            path = os.path.join(BACKUP_DIR, safe_name)
            if not os.path.exists(path):
                return jsonify({"error": "Backup not found"}), 404
            os.remove(path)
            log_admin_action(current_user.id, 'delete_backup', safe_name, 'Backup file deleted', request.remote_addr)
            return jsonify({"message": f"Backup {safe_name} deleted"}), 200
        except Exception as e:
            return jsonify({"error": "Failed to delete backup"}), 500

    # --- User Impersonation ---
    @app.route('/admin/impersonate/<user_email>', methods=['POST'])
    @login_required
    @admin_required
    def admin_impersonate(user_email):
        """Impersonate a user (login as them). Stores original admin session."""
        try:
            target = db.session.get(DBUser, user_email)
            if not target:
                return jsonify({"error": "User not found"}), 404

            if target.is_admin:
                return jsonify({"error": "Cannot impersonate another admin"}), 400

            # Store admin identity in session for returning later
            session['impersonating_from'] = current_user.id
            log_admin_action(current_user.id, 'impersonate', user_email, f'Started impersonating {user_email}', request.remote_addr)

            login_user(target)
            flash(f"Now viewing as {user_email}. Click 'Stop Impersonating' to return.", "info")
            return redirect(url_for('home'))
        except Exception as e:
            app.logger.error(f"[Admin] Impersonate error: {e}", exc_info=True)
            return jsonify({"error": "Failed to impersonate"}), 500

    @app.route('/admin/stop-impersonation')
    @login_required
    def admin_stop_impersonation():
        """Return to the original admin account after impersonation."""
        try:
            original_admin = session.pop('impersonating_from', None)
            if not original_admin:
                return redirect(url_for('home'))

            admin_user = db.session.get(DBUser, original_admin)
            if admin_user and admin_user.is_admin:
                log_admin_action(original_admin, 'stop_impersonate', current_user.id, 'Stopped impersonation')
                login_user(admin_user)
                flash("Returned to admin account.", "info")
                return redirect(url_for('admin_dashboard'))
            else:
                return redirect(url_for('home'))
        except Exception as e:
            app.logger.error(f"[Admin] Stop impersonate error: {e}", exc_info=True)
            return redirect(url_for('home'))

    # --- Config Management ---
    @app.route('/admin/config')
    @login_required
    @admin_required
    def admin_config():
        """View and edit configuration"""
        try:
            config_path = os.path.join(basedir, "config", "config.yaml")
            with open(config_path, "r") as f:
                current_config = yaml.safe_load(f) or {}

            # Mask sensitive values for display
            display_config = {}
            for section, values in current_config.items():
                if isinstance(values, dict):
                    display_config[section] = {}
                    for key, val in values.items():
                        if any(s in key.lower() for s in ['token', 'secret', 'password', 'key']):
                            display_config[section][key] = '***HIDDEN***'
                        else:
                            display_config[section][key] = val
                else:
                    display_config[section] = values

            return render_template('admin/config.html', config=current_config, display_config=display_config)
        except Exception as e:
            app.logger.error(f"[Admin] Config page error: {e}", exc_info=True)
            return "Error loading config", 500

    @app.route('/admin/config/update', methods=['POST'])
    @login_required
    @admin_required
    def admin_config_update():
        """Update a config value"""
        try:
            section = request.form.get('section', '').strip()
            key = request.form.get('key', '').strip()
            value = request.form.get('value', '').strip()

            if not section or not key:
                return jsonify({"error": "Section and key are required"}), 400

            config_path = os.path.join(basedir, "config", "config.yaml")
            with open(config_path, "r") as f:
                current_config = yaml.safe_load(f) or {}

            if section not in current_config:
                current_config[section] = {}

            old_value = current_config.get(section, {}).get(key, '')
            # Preserve original type when updating config values
            if isinstance(old_value, bool):
                value = value.lower() in ('true', '1', 'yes')
            elif isinstance(old_value, int):
                try:
                    value = int(value)
                except ValueError:
                    pass
            elif isinstance(old_value, float):
                try:
                    value = float(value)
                except ValueError:
                    pass
            current_config[section][key] = value

            with open(config_path, "w") as f:
                yaml.dump(current_config, f, default_flow_style=False)

            log_admin_action(current_user.id, 'update_config', f"{section}.{key}", f"Changed from '{old_value}' to '{value}'", request.remote_addr)
            return jsonify({"message": f"Config {section}.{key} updated"}), 200
        except Exception as e:
            app.logger.error(f"[Admin] Config update error: {e}", exc_info=True)
            return jsonify({"error": "Failed to update config"}), 500

    # --- Match/Tournament Management ---
    @app.route('/admin/matches')
    @login_required
    @admin_required
    def admin_matches():
        """View and manage matches and tournaments"""
        try:
            # Active in-memory matches
            active_matches = []
            with MATCH_INSTANCES_LOCK:
                for mid, instance in MATCH_INSTANCES.items():
                    data = getattr(instance, 'data', {})
                    home = data.get('team_home', '?').split('_')[0]
                    away = data.get('team_away', '?').split('_')[0]
                    active_matches.append({
                        'id': mid,
                        'teams': f"{home} vs {away}",
                        'user': data.get('created_by', '?'),
                        'created': data.get('created_at', None),
                    })

            # Recent DB matches
            recent_matches = db.session.query(DBMatch).order_by(DBMatch.date.desc()).limit(25).all()

            # Active tournaments
            active_tournaments = db.session.query(Tournament).filter_by(status='Active').all()
            completed_tournaments = db.session.query(Tournament).filter_by(status='Completed').order_by(Tournament.created_at.desc()).limit(10).all()

            return render_template('admin/matches.html',
                                   active_matches=active_matches,
                                   recent_matches=recent_matches,
                                   active_tournaments=active_tournaments,
                                   completed_tournaments=completed_tournaments)
        except Exception as e:
            app.logger.error(f"[Admin] Matches page error: {e}", exc_info=True)
            return "Error loading matches", 500

    @app.route('/admin/matches/<match_id>/terminate', methods=['POST'])
    @login_required
    @admin_required
    def admin_terminate_match(match_id):
        """Terminate an active in-memory match"""
        try:
            with MATCH_INSTANCES_LOCK:
                if match_id in MATCH_INSTANCES:
                    del MATCH_INSTANCES[match_id]
                    log_admin_action(current_user.id, 'terminate_match', match_id, 'Active match terminated', request.remote_addr)
                    return jsonify({"message": f"Match {match_id[:8]}... terminated"}), 200
                else:
                    return jsonify({"error": "Match not found in active instances"}), 404
        except Exception as e:
            return jsonify({"error": "Failed to terminate match"}), 500

    # --- Audit Log ---
    @app.route('/admin/audit-log')
    @login_required
    @admin_required
    def admin_audit_log():
        """View full admin audit log"""
        try:
            page = request.args.get('page', 1, type=int)
            per_page = 25
            offset = (page - 1) * per_page

            total = db.session.query(AdminAuditLog).count()
            entries = db.session.query(AdminAuditLog).order_by(AdminAuditLog.timestamp.desc()).offset(offset).limit(per_page).all()

            total_pages = (total + per_page - 1) // per_page

            return render_template('admin/audit_log.html',
                                   entries=entries,
                                   page=page,
                                   total_pages=total_pages,
                                   total=total)
        except Exception as e:
            app.logger.error(f"[Admin] Audit log error: {e}", exc_info=True)
            return "Error loading audit log", 500

    # --- Maintenance Mode Toggle ---
    @app.route('/admin/maintenance/toggle', methods=['POST'])
    @login_required
    @admin_required
    def admin_toggle_maintenance():
        """Toggle maintenance mode on/off (admin only)."""
        global MAINTENANCE_MODE
        with MAINTENANCE_MODE_LOCK:
            MAINTENANCE_MODE = not MAINTENANCE_MODE
            state = 'enabled' if MAINTENANCE_MODE else 'disabled'
            # Persist to config.yaml
            try:
                config_path = os.getenv("SIMCRICKETX_CONFIG_PATH") or os.path.join(basedir, "config", "config.yaml")
                cfg = {}
                if os.path.exists(config_path):
                    with open(config_path, "r") as f:
                        cfg = yaml.safe_load(f) or {}
                cfg.setdefault("app", {})["maintenance_mode"] = MAINTENANCE_MODE
                with open(config_path, "w") as f:
                    yaml.safe_dump(cfg, f, default_flow_style=False)
            except Exception as e:
                app.logger.error(f"[Admin] Failed to persist maintenance mode to config: {e}")
        log_admin_action(current_user.id, 'toggle_maintenance', state, f'Maintenance mode {state}', request.remote_addr)
        app.logger.info(f"[Admin] Maintenance mode {state} by {current_user.id}")
        return jsonify({"maintenance_mode": MAINTENANCE_MODE, "message": f"Maintenance mode {state}"}), 200

    @app.route('/admin/maintenance/status')
    @login_required
    @admin_required
    def admin_maintenance_status():
        """Get current maintenance mode status."""
        return jsonify({"maintenance_mode": MAINTENANCE_MODE}), 200

    # --- Ban / Suspend Users ---
    @app.route('/admin/users/<user_email>/ban', methods=['POST'])
    @login_required
    @admin_required
    def admin_ban_user(user_email):
        user = db.session.get(DBUser, user_email)
        if not user:
            return jsonify({"error": "User not found"}), 404
        if user.is_admin:
            return jsonify({"error": "Cannot ban an admin"}), 400
        reason = request.form.get('reason', '').strip() or 'No reason provided'
        duration = request.form.get('duration', '').strip()  # e.g. '7' for 7 days, empty=permanent
        user.is_banned = True
        user.ban_reason = reason
        if duration and duration.isdigit() and int(duration) > 0:
            user.banned_until = datetime.utcnow() + timedelta(days=int(duration))
        else:
            user.banned_until = None  # permanent
        db.session.commit()
        # Terminate their active sessions
        ActiveSession.query.filter_by(user_id=user_email).delete()
        db.session.commit()
        until_str = f"for {duration} days" if duration and duration.isdigit() else "permanently"
        log_admin_action(current_user.id, 'ban_user', user_email, f"Banned {until_str}: {reason}", request.remote_addr)
        return jsonify({"message": f"User {user_email} banned {until_str}"}), 200

    @app.route('/admin/users/<user_email>/unban', methods=['POST'])
    @login_required
    @admin_required
    def admin_unban_user(user_email):
        user = db.session.get(DBUser, user_email)
        if not user:
            return jsonify({"error": "User not found"}), 404
        user.is_banned = False
        user.banned_until = None
        user.ban_reason = None
        db.session.commit()
        log_admin_action(current_user.id, 'unban_user', user_email, 'Ban lifted', request.remote_addr)
        return jsonify({"message": f"User {user_email} unbanned"}), 200

    # --- Force Password Reset ---
    @app.route('/admin/users/<user_email>/force-reset', methods=['POST'])
    @login_required
    @admin_required
    def admin_force_password_reset(user_email):
        user = db.session.get(DBUser, user_email)
        if not user:
            return jsonify({"error": "User not found"}), 404
        if user.is_admin:
            return jsonify({"error": "Cannot force reset on admin"}), 400
        user.force_password_reset = True
        db.session.commit()
        log_admin_action(current_user.id, 'force_password_reset', user_email, 'Flagged for password reset', request.remote_addr)
        return jsonify({"message": f"{user_email} will be forced to change password on next login"}), 200

    # --- Active Sessions ---
    @app.route('/admin/sessions')
    @login_required
    @admin_required
    def admin_sessions():
        sessions = ActiveSession.query.order_by(ActiveSession.last_active.desc()).all()
        return render_template('admin/sessions.html', sessions=sessions)

    @app.route('/admin/sessions/<int:session_id>/terminate', methods=['POST'])
    @login_required
    @admin_required
    def admin_terminate_session(session_id):
        s = db.session.get(ActiveSession, session_id)
        if not s:
            return jsonify({"error": "Session not found"}), 404
        target_user = s.user_id
        db.session.delete(s)
        db.session.commit()
        log_admin_action(current_user.id, 'terminate_session', target_user, f'Session {session_id} terminated', request.remote_addr)
        return jsonify({"message": "Session terminated"}), 200

    @app.route('/admin/sessions/cleanup', methods=['POST'])
    @login_required
    @admin_required
    def admin_cleanup_sessions():
        cutoff = datetime.utcnow() - timedelta(days=7)
        count = ActiveSession.query.filter(ActiveSession.last_active < cutoff).delete()
        db.session.commit()
        return jsonify({"message": f"Cleaned up {count} stale sessions"}), 200

    # --- Failed Login Tracker ---
    @app.route('/admin/failed-logins')
    @login_required
    @admin_required
    def admin_failed_logins():
        page = request.args.get('page', 1, type=int)
        per_page = 30
        query = FailedLoginAttempt.query.order_by(FailedLoginAttempt.timestamp.desc())
        total = query.count()
        entries = query.offset((page - 1) * per_page).limit(per_page).all()
        total_pages = (total + per_page - 1) // per_page
        # Top offending IPs (last 24h)
        cutoff_24h = datetime.utcnow() - timedelta(hours=24)
        top_ips = db.session.query(
            FailedLoginAttempt.ip_address,
            func.count(FailedLoginAttempt.id).label('count')
        ).filter(FailedLoginAttempt.timestamp >= cutoff_24h).group_by(
            FailedLoginAttempt.ip_address
        ).order_by(func.count(FailedLoginAttempt.id).desc()).limit(10).all()
        return render_template('admin/failed_logins.html',
                               entries=entries, page=page, total_pages=total_pages, total=total, top_ips=top_ips)

    @app.route('/admin/failed-logins/clear', methods=['POST'])
    @login_required
    @admin_required
    def admin_clear_failed_logins():
        count = FailedLoginAttempt.query.delete()
        db.session.commit()
        log_admin_action(current_user.id, 'clear_failed_logins', None, f'Cleared {count} entries', request.remote_addr)
        return jsonify({"message": f"Cleared {count} failed login records"}), 200

    # --- IP Blocklist ---
    @app.route('/admin/ip-blocklist')
    @login_required
    @admin_required
    def admin_ip_blocklist():
        blocked = BlockedIP.query.order_by(BlockedIP.blocked_at.desc()).all()
        return render_template('admin/ip_blocklist.html', blocked=blocked)

    @app.route('/admin/ip-blocklist/add', methods=['POST'])
    @login_required
    @admin_required
    def admin_block_ip():
        ip = request.form.get('ip_address', '').strip()
        reason = request.form.get('reason', '').strip() or 'No reason'
        if not ip:
            return jsonify({"error": "IP address required"}), 400
        if BlockedIP.query.filter_by(ip_address=ip).first():
            return jsonify({"error": "IP already blocked"}), 400
        entry = BlockedIP(ip_address=ip, reason=reason, blocked_by=current_user.id)
        db.session.add(entry)
        db.session.commit()
        log_admin_action(current_user.id, 'block_ip', ip, reason, request.remote_addr)
        return jsonify({"message": f"IP {ip} blocked"}), 200

    @app.route('/admin/ip-blocklist/<int:block_id>/remove', methods=['POST'])
    @login_required
    @admin_required
    def admin_unblock_ip(block_id):
        entry = db.session.get(BlockedIP, block_id)
        if not entry:
            return jsonify({"error": "Entry not found"}), 404
        ip = entry.ip_address
        db.session.delete(entry)
        db.session.commit()
        log_admin_action(current_user.id, 'unblock_ip', ip, 'IP unblocked', request.remote_addr)
        return jsonify({"message": f"IP {ip} unblocked"}), 200

    # --- Log Viewer ---
    @app.route('/admin/logs')
    @login_required
    @admin_required
    def admin_logs():
        log_path = os.path.join(PROJECT_ROOT, "logs", "execution.log")
        lines = []
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except Exception:
                lines = ["Error reading log file"]
        # Show last 500 lines by default, most recent first
        lines = lines[-500:]
        lines.reverse()
        return render_template('admin/logs.html', lines=lines, total_lines=len(lines))

    @app.route('/admin/logs/download')
    @login_required
    @admin_required
    def admin_download_logs():
        """Download the full execution.log file."""
        log_path = os.path.join(PROJECT_ROOT, "logs", "execution.log")
        if not os.path.exists(log_path):
            return jsonify({"error": "Log file not found"}), 404
        return send_file(log_path, as_attachment=True, download_name='execution.log', mimetype='text/plain')

    # --- Rate Limit Config ---
    @app.route('/admin/rate-limits')
    @login_required
    @admin_required
    def admin_rate_limits():
        cfg = load_config()
        rl = cfg.get('rate_limits', {})
        return render_template('admin/rate_limits.html',
                               max_requests=rl.get('max_requests', 30),
                               window_seconds=rl.get('window_seconds', 10),
                               admin_multiplier=rl.get('admin_multiplier', 3),
                               login_limit=rl.get('login_limit', '10 per minute'))

    @app.route('/admin/rate-limits/update', methods=['POST'])
    @login_required
    @admin_required
    def admin_update_rate_limits():
        try:
            config_path = os.getenv("SIMCRICKETX_CONFIG_PATH") or os.path.join(basedir, "config", "config.yaml")
            cfg = {}
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    cfg = yaml.safe_load(f) or {}
            cfg['rate_limits'] = {
                'max_requests': int(request.form.get('max_requests', 30)),
                'window_seconds': int(request.form.get('window_seconds', 10)),
                'admin_multiplier': int(request.form.get('admin_multiplier', 3)),
                'login_limit': request.form.get('login_limit', '10 per minute')
            }
            with open(config_path, "w") as f:
                yaml.safe_dump(cfg, f, default_flow_style=False)
            log_admin_action(current_user.id, 'update_rate_limits', None, json.dumps(cfg['rate_limits']), request.remote_addr)
            return jsonify({"message": "Rate limits updated (restart required for full effect)"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # --- Global Team Browser ---
    @app.route('/admin/global-teams')
    @login_required
    @admin_required
    def admin_global_teams():
        page = request.args.get('page', 1, type=int)
        search = request.args.get('q', '').strip()
        per_page = 25
        query = DBTeam.query.filter(DBTeam.is_placeholder != True)
        if search:
            query = query.filter(
                db.or_(DBTeam.name.ilike(f'%{search}%'), DBTeam.user_id.ilike(f'%{search}%'), DBTeam.short_code.ilike(f'%{search}%'))
            )
        total = query.count()
        teams = query.order_by(DBTeam.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
        total_pages = (total + per_page - 1) // per_page
        return render_template('admin/global_teams.html',
                               teams=teams, page=page, total_pages=total_pages, total=total, search=search)

    # --- Global Match Browser ---
    @app.route('/admin/global-matches')
    @login_required
    @admin_required
    def admin_global_matches():
        page = request.args.get('page', 1, type=int)
        search = request.args.get('q', '').strip()
        per_page = 25
        query = DBMatch.query
        if search:
            query = query.filter(
                db.or_(DBMatch.user_id.ilike(f'%{search}%'), DBMatch.result_description.ilike(f'%{search}%'))
            )
        total = query.count()
        matches = query.order_by(DBMatch.date.desc()).offset((page - 1) * per_page).limit(per_page).all()
        total_pages = (total + per_page - 1) // per_page
        return render_template('admin/global_matches.html',
                               matches=matches, page=page, total_pages=total_pages, total=total, search=search)

    # --- DB Export ---
    @app.route('/admin/export')
    @login_required
    @admin_required
    def admin_export_page():
        return render_template('admin/export.html')

    @app.route('/admin/export/<table>/<fmt>')
    @login_required
    @admin_required
    def admin_export_data(table, fmt):
        if fmt not in ('csv', 'json', 'txt'):
            return jsonify({"error": "Invalid format. Use csv, json, or txt"}), 400
        table_map = {
            'users': DBUser,
            'teams': DBTeam,
            'players': DBPlayer,
            'matches': DBMatch,
            'tournaments': Tournament,
            'match_scorecards': MatchScorecard,
            'tournament_teams': TournamentTeam,
            'tournament_fixtures': TournamentFixture,
            'tournament_player_stats': TournamentPlayerStatsCache,
            'match_partnerships': MatchPartnership,
            'audit_log': AdminAuditLog,
            'failed_logins': FailedLoginAttempt,
            'blocked_ips': BlockedIP,
            'active_sessions': ActiveSession,
        }
        if table not in table_map:
            return jsonify({"error": f"Unknown table: {table}"}), 400
        model = table_map[table]
        rows = model.query.all()
        # Build list of dicts from columns
        columns = [c.name for c in model.__table__.columns]
        data = []
        for row in rows:
            d = {}
            for col in columns:
                val = getattr(row, col, None)
                if isinstance(val, datetime):
                    val = val.isoformat()
                d[col] = val
            data.append(d)
        log_admin_action(current_user.id, 'export_data', f'{table}.{fmt}', f'{len(data)} rows', request.remote_addr)
        if fmt == 'json':
            return Response(json.dumps(data, indent=2, default=str),
                            mimetype='application/json',
                            headers={'Content-Disposition': f'attachment; filename={table}_export.json'})
        elif fmt == 'csv':
            import io, csv
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader()
            writer.writerows(data)
            return Response(output.getvalue(),
                            mimetype='text/csv',
                            headers={'Content-Disposition': f'attachment; filename={table}_export.csv'})
        else:  # txt
            lines = []
            for d in data:
                lines.append(' | '.join(str(d.get(c, '')) for c in columns))
            header = ' | '.join(columns)
            sep = '-' * len(header)
            content = header + '\n' + sep + '\n' + '\n'.join(lines)
            return Response(content,
                            mimetype='text/plain',
                            headers={'Content-Disposition': f'attachment; filename={table}_export.txt'})

    @app.route('/admin/export/all/<fmt>')
    @login_required
    @admin_required
    def admin_export_all(fmt):
        """Export all tables in a single ZIP file."""
        if fmt not in ('csv', 'json', 'txt'):
            return jsonify({"error": "Invalid format"}), 400
        import io, csv, zipfile
        all_tables = {
            'users': DBUser, 'teams': DBTeam, 'players': DBPlayer,
            'matches': DBMatch, 'tournaments': Tournament,
            'match_scorecards': MatchScorecard, 'tournament_teams': TournamentTeam,
            'tournament_fixtures': TournamentFixture, 'tournament_player_stats': TournamentPlayerStatsCache,
            'match_partnerships': MatchPartnership, 'audit_log': AdminAuditLog,
            'failed_logins': FailedLoginAttempt, 'blocked_ips': BlockedIP, 'active_sessions': ActiveSession,
        }
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for tbl_name, model in all_tables.items():
                columns = [c.name for c in model.__table__.columns]
                rows = model.query.all()
                data = []
                for row in rows:
                    d = {}
                    for col in columns:
                        val = getattr(row, col, None)
                        if isinstance(val, datetime):
                            val = val.isoformat()
                        d[col] = val
                    data.append(d)
                if fmt == 'json':
                    content = json.dumps(data, indent=2, default=str)
                    zf.writestr(f'{tbl_name}.json', content)
                elif fmt == 'csv':
                    output = io.StringIO()
                    writer = csv.DictWriter(output, fieldnames=columns)
                    writer.writeheader()
                    writer.writerows(data)
                    zf.writestr(f'{tbl_name}.csv', output.getvalue())
                else:
                    header = ' | '.join(columns)
                    sep = '-' * len(header)
                    lines = [' | '.join(str(d.get(c, '')) for c in columns) for d in data]
                    zf.writestr(f'{tbl_name}.txt', header + '\n' + sep + '\n' + '\n'.join(lines))
        zip_buffer.seek(0)
        log_admin_action(current_user.id, 'export_all', fmt, f'All tables exported as {fmt}', request.remote_addr)
        return Response(zip_buffer.getvalue(),
                        mimetype='application/zip',
                        headers={'Content-Disposition': f'attachment; filename=simcricketx_export_{fmt}.zip'})

    # --- Scheduled Tasks Dashboard ---
    @app.route('/admin/scheduled-tasks')
    @login_required
    @admin_required
    def admin_scheduled_tasks():
        tasks = []
        # Backup scheduler
        tasks.append({
            'name': 'Database Backup',
            'status': 'Active' if _backup_scheduler_started else 'Inactive',
            'interval': '24 hours',
            'description': 'Automatic database backup to data/backups/',
            'last_run': _get_last_backup_time(),
        })
        # Cleanup task
        tasks.append({
            'name': 'Match Instance Cleanup',
            'status': 'Active',
            'interval': '6 hours',
            'description': 'Removes old in-memory match instances and orphaned JSON files',
            'last_run': None,
        })
        # Backup retention
        tasks.append({
            'name': 'Backup Retention Cleanup',
            'status': 'Active',
            'interval': 'On each backup',
            'description': 'Removes backups older than 7 days',
            'last_run': None,
        })
        # Session cleanup hint
        tasks.append({
            'name': 'Stale Session Cleanup',
            'status': 'Manual',
            'interval': 'On demand',
            'description': 'Clean up sessions inactive for 7+ days (via Active Sessions page)',
            'last_run': None,
        })
        return render_template('admin/scheduled_tasks.html', tasks=tasks)

    def _get_last_backup_time():
        """Get timestamp of the most recent backup file."""
        backup_dir = os.path.join(PROJECT_ROOT, "data", "backups")
        if not os.path.isdir(backup_dir):
            return None
        files = [f for f in os.listdir(backup_dir) if f.endswith('.db')]
        if not files:
            return None
        files.sort(key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)), reverse=True)
        mtime = os.path.getmtime(os.path.join(backup_dir, files[0]))
        return datetime.fromtimestamp(mtime)

    # --- Request logging ---
    @app.before_request
    def log_request():
        app.logger.info(f"{request.remote_addr} {request.method} {request.path}")
        if request.path.startswith("/admin") or request.path.startswith("/__codex_probe"):
            from werkzeug.routing import MapAdapter
            adapter = app.url_map.bind("")
            try:
                endpoint, values = adapter.match(request.path, method=request.method)
                app.logger.info(f"[RouteMatch] path={request.path} endpoint={endpoint} values={values}")
            except Exception as e:
                admin_routes = sorted([r.rule for r in app.url_map.iter_rules() if r.rule.startswith('/admin')])
                app.logger.error(
                    f"[RouteMatch] no-match path={request.path} method={request.method} err={e} "
                    f"admin_routes_count={len(admin_routes)} probe_present="
                    f"{any(r.rule == '/__codex_probe' for r in app.url_map.iter_rules())}"
                )

    @app.route('/__codex_probe')
    def codex_probe():
        admin_routes = sorted([r.rule for r in app.url_map.iter_rules() if r.rule.startswith('/admin')])
        return jsonify({
            "probe": "simcricketx-admin-diagnostic",
            "file": os.path.abspath(__file__),
            "admin_route_count": len(admin_routes),
            "has_admin_activity": "/admin/activity" in admin_routes,
            "has_admin_catchall": "/admin/<path:subpath>" in admin_routes,
            "admin_routes": admin_routes,
        }), 200

    @app.route('/__codex_probe.')
    def codex_probe_dot():
        return codex_probe()

    @app.errorhandler(404)
    def handle_not_found(err):
        path = request.path or ""
        if path.startswith('/admin') or path.startswith('/__codex_probe'):
            admin_routes = sorted([r.rule for r in app.url_map.iter_rules() if r.rule.startswith('/admin')])
            app.logger.error(
                f"[RouteDebug] 404 for {path}. file={os.path.abspath(__file__)} "
                f"admin_routes_count={len(admin_routes)} admin_routes={admin_routes}"
            )
        # For API/JSON requests, return JSON
        if request.path.startswith('/api/') or request.accept_mimetypes.best == 'application/json':
            return jsonify({"error": "Not found"}), 404
        reason = getattr(err, 'description', None)
        return render_template('404.html', reason=reason), 404

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
                    # Reverse player career stats BEFORE deleting scorecards
                    old_scorecards = MatchScorecard.query.filter_by(match_id=match_id).all()
                    if old_scorecards:
                        reverse_player_aggregates(old_scorecards, logger=logger)
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
                    date=datetime.now(),
                    overs_per_side=match.data.get('overs', 20)
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
                    overs = f"{balls_total // 6}.{balls_total % 6}"
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
                fixture.winner_team_id = db_match.winner_team_id

                # Knockout/playoff fixtures MUST have a winner to progress.
                # If the match ended as a tie/no-result in a non-league stage,
                # keep the fixture as 'Scheduled' so the user can re-simulate.
                is_knockout_stage = fixture.stage and fixture.stage != 'league'
                if is_knockout_stage and not db_match.winner_team_id:
                    fixture.status = 'Scheduled'
                    fixture.standings_applied = False
                    logger.warning(
                        f"[Tournament] Knockout fixture {fixture_id} has no winner "
                        f"(stage={fixture.stage}). Fixture kept as Scheduled for re-simulation."
                    )
                else:
                    fixture.status = 'Completed'

                logger.info(f"[Tournament] Fixture {fixture_id} status set to {fixture.status}")
                
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
            except Exception:  # Fix D7: Don't swallow SystemExit/KeyboardInterrupt
                pass

    def _persist_non_tournament_match_completion(match, match_id, outcome, logger):
        """
        Persist a completed non-tournament match so it appears in history
        and can be opened via the existing scoreboard endpoint.
        """
        try:
            match.data["current_state"] = "completed"
            final_result = outcome.get("result") or getattr(match, "result", None)
            if final_result:
                match.data["result_description"] = final_result

            archiver = MatchArchiver(match.data, match)
            archiver._save_to_database()
            logger.info(f"[MatchHistory] Persisted non-tournament match {match_id}")
        except Exception as e:
            logger.error(f"[MatchHistory] Failed to persist match {match_id}: {e}", exc_info=True)

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
            
        # Count currently active users (active session in last 15 minutes)
        active_threshold = datetime.now(timezone.utc) - timedelta(minutes=15)
        active_users_count = db.session.query(ActiveSession).filter(ActiveSession.last_active >= active_threshold).distinct(ActiveSession.user_id).count()

        return render_template("home.html", user=current_user, total_visits=get_visit_counter(), matches_simulated=get_matches_simulated(), active_users=active_users_count)

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
                user = db.session.get(DBUser, email)
                if user:
                    # Check ban status
                    if user.is_banned:
                        if user.banned_until and user.banned_until <= datetime.utcnow():
                            # Temp ban expired — lift it
                            user.is_banned = False
                            user.banned_until = None
                            user.ban_reason = None
                            db.session.commit()
                        else:
                            reason = user.ban_reason or "No reason provided"
                            until = f" until {user.banned_until.strftime('%Y-%m-%d %H:%M UTC')}" if user.banned_until else " (permanent)"
                            app.logger.warning(f"[Auth] Banned user {email} attempted login")
                            return render_template("login.html", error=f"Account suspended{until}. Reason: {reason}")

                    login_user(user, remember=True, duration=app.config.get("REMEMBER_COOKIE_DURATION"))
                    session.permanent = True

                    # Track active session
                    try:
                        import secrets
                        token = secrets.token_hex(32)
                        session['session_token'] = token
                        active = ActiveSession(
                            session_token=token,
                            user_id=email,
                            ip_address=request.remote_addr,
                            user_agent=request.user_agent.string[:300] if request.user_agent.string else None
                        )
                        db.session.add(active)
                        db.session.commit()
                    except Exception as e:
                        db.session.rollback()
                        app.logger.error(f"[Auth] Session tracking error: {e}")

                    # Check force password reset
                    if user.force_password_reset:
                        session['force_password_reset'] = True
                        return redirect(url_for("force_change_password"))

                    app.logger.info(f"Successful login for {email}")
                    return redirect(url_for("home"))
            else:
                # Record failed login attempt
                try:
                    failed = FailedLoginAttempt(
                        email=email,
                        ip_address=request.remote_addr,
                        user_agent=request.user_agent.string[:300] if request.user_agent.string else None
                    )
                    db.session.add(failed)
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                return render_template("login.html", error="Invalid email or password")

        except Exception as e:
            app.logger.error(f"Login error: {e}")
            return render_template("login.html", error="System error")

    @app.route("/change-password", methods=["GET", "POST"])
    @login_required
    def force_change_password():
        """Force password change page."""
        if request.method == "GET":
            return render_template("force_change_password.html")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not new_password or len(new_password) < 8:
            return render_template("force_change_password.html", error="Password must be at least 8 characters")
        if new_password != confirm_password:
            return render_template("force_change_password.html", error="Passwords do not match")
        try:
            from werkzeug.security import generate_password_hash
            current_user.password_hash = generate_password_hash(new_password)
            current_user.force_password_reset = False
            db.session.commit()
            session.pop('force_password_reset', None)
            flash("Password changed successfully.", "success")
            return redirect(url_for("home"))
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"[Auth] Force password change error: {e}")
            return render_template("force_change_password.html", error="Failed to change password")

    @app.route("/delete_account", methods=["POST"])
    @login_required
    def delete_account():
        confirmation = request.form.get("confirm_delete", "")
        if confirmation != "DELETE":
            flash("Account deletion requires typing DELETE to confirm.", "danger")
            return redirect(url_for("home"))

        email = current_user.id
        app.logger.info(f"Account deletion requested for {email}")
        if delete_user(email, requesting_user_email=current_user.id):
            logout_user()
            return redirect(url_for("register"))
        else:
            flash("Failed to delete account. Please try again.", "danger")
            return redirect(url_for("home"))

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        # Clean up active session
        token = session.get('session_token')
        if token:
            try:
                ActiveSession.query.filter_by(session_token=token).delete()
                db.session.commit()
            except Exception:
                db.session.rollback()
        session.pop("visit_counted", None)
        session.pop("session_token", None)
        session.pop("force_password_reset", None)
        app.logger.info(f"Logout for {current_user.id}")
        logout_user()
        session.pop('_flashes', None)
        return redirect(url_for("login"))
    
    def load_user_teams(user_email):
        """Return list of team dicts created by this user from DB."""
        teams = []
        try:
            db_teams = DBTeam.query.filter_by(user_id=user_email).filter(
                DBTeam.is_placeholder != True
            ).all()
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
            db_teams = DBTeam.query.filter_by(user_id=current_user.id).filter(
                DBTeam.is_placeholder != True
            ).all()
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
            simulation_mode = str(data.get("simulation_mode", "auto")).lower()
            if simulation_mode not in {"auto", "manual"}:
                simulation_mode = "auto"
            data["simulation_mode"] = simulation_mode

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
                        "is_captain": p.is_captain,  # Captain flag for toss logic
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
            # D1: Use match_id in filename for O(1) lookup
            fname = f"match_{match_id}.json"

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
        match_data, _path, _err = _load_match_file_for_user(match_id)

        if not match_data:
            # JSON cleaned up after archiving — check DB for completed match
            db_match = DBMatch.query.filter_by(id=match_id, user_id=current_user.id).first()
            if db_match:
                return redirect(url_for("view_scoreboard", match_id=match_id))
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
                "total": total_extras,
            }
            # Fix D11: Add total score (batting runs + extras)
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
        with _get_match_file_lock(match_id):  # D3: serialize file access per match
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
        with _get_match_file_lock(match_id):  # D3: serialize file access per match
            match_data, match_path, err = _load_match_file_for_user(match_id)
            if err:
                return err

            if not match_data:
                return jsonify({"error": "Match not found"}), 404

            team_home = match_data["team_home"].split('_')[0]
            team_away = match_data["team_away"].split('_')[0]
            toss_choice = match_data["toss"]
            toss_result = random.choice(["Heads", "Tails"])
            
            # Get captain names with fallback logic
            def get_captain_name(team_players, team_short_code):
                """Find captain in playing XI, fallback to first player or team code"""
                # Try to find player with is_captain flag
                captain = next((p for p in team_players if p.get("is_captain")), None)
                if captain:
                    return captain["name"]
                
                # Fallback 1: First player in XI (for old data without is_captain)
                if team_players:
                    app.logger.warning(f"[Toss] No captain found for {team_short_code}, using first player")
                    return team_players[0]["name"]
                
                # Fallback 2: Team short code (shouldn't happen)
                app.logger.error(f"[Toss] No players in XI for {team_short_code}!")
                return team_short_code
            
            home_captain = get_captain_name(match_data["playing_xi"]["home"], team_home)
            away_captain = get_captain_name(match_data["playing_xi"]["away"], team_away)

            toss_winner = team_away if toss_choice == toss_result else team_home
            toss_decision = random.choice(["Bat", "Bowl"])

            match_data["toss_winner"] = toss_winner
            match_data["toss_decision"] = toss_decision

            with open(match_path, "w") as f:
                json.dump(match_data, f, indent=2)

            # Update the in-memory Match instance, if created
            with MATCH_INSTANCES_LOCK:
                if match_id in MATCH_INSTANCES:
                    app.logger.info(f"[ImpactSwap] Found active match instance for {match_id}. Updating state.")
                    match_instance = MATCH_INSTANCES[match_id]
                    match_instance.toss_winner   = toss_winner
                    match_instance.toss_decision = toss_decision
                    match_instance.batting_team  = match_instance.home_xi if toss_decision=="Bat" else match_instance.away_xi
                    match_instance.bowling_team  = match_instance.away_xi if match_instance.batting_team==match_instance.home_xi else match_instance.home_xi

        # Build toss commentary (outside lock — no file/instance access needed)
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
        """Handle impact player substitution with optional swaps for each team."""
        app.logger.info(f"[ImpactSwap] Starting impact player swap for match {match_id}")

        try:
            swap_data = request.get_json()
            if not swap_data:
                return jsonify({"error": "Request body is required"}), 400

            home_swap = swap_data.get("home_swap")
            away_swap = swap_data.get("away_swap")

            with _get_match_file_lock(match_id):  # D3: serialize file access per match
                match_data, match_path, err = _load_match_file_for_user(match_id)
                if err:
                    return err

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

                # Update in-memory instance under MATCH_INSTANCES_LOCK
                with MATCH_INSTANCES_LOCK:
                    if match_id in MATCH_INSTANCES:
                        app.logger.info(f"[ImpactSwap] Found active match instance for {match_id}. Updating state.")
                        match_instance = MATCH_INSTANCES[match_id]
                        match_instance.home_xi = match_data["playing_xi"]["home"]
                        match_instance.away_xi = match_data["playing_xi"]["away"]
                        match_instance.data = match_data
                        app.logger.info(f"[ImpactSwap] Instance updated. Home XI now has {len(match_instance.home_xi)} players.")
                    else:
                        app.logger.warning(f"[ImpactSwap] No active match instance found for {match_id}.")

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
                    return err if err else (jsonify({"error": "Match not found"}), 404)
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
    @rate_limit(max_requests=30, window_seconds=10)  # C3: Rate limit to prevent DoS
    def next_ball(match_id):
        try:
            with MATCH_INSTANCES_LOCK:  # Bug Fix B2: Thread-safe match creation
                if match_id not in MATCH_INSTANCES:
                    # Try loading match data from JSON file first (for active/new matches)
                    match_data, _path, err = _load_match_file_for_user(match_id)
                    if match_data:
                        if 'rain_probability' not in match_data:
                            match_data['rain_probability'] = load_config().get('rain_probability', 0.0)
                        MATCH_INSTANCES[match_id] = Match(match_data)
                    else:
                        return err if err else (jsonify({"error": "Match not found"}), 404)
                
                match = MATCH_INSTANCES[match_id]
            if match.data.get("created_by") != current_user.id:
                return jsonify({"error": "Unauthorized"}), 403
            outcome = match.next_ball()

            # Explicitly send final score and wickets clearly
            if outcome.get("match_over"):
                # Only increment if this is the first time we're seeing the match end
                # (Checking if it wasn't already marked completed prevents double counting on repeated API calls)
                first_completion = match.data.get("current_state") != "completed"
                if first_completion:
                    increment_matches_simulated()

                    # Persist completed match data in DB.
                    if match.data.get("tournament_id"):
                        _handle_tournament_match_completion(match, match_id, outcome, app.logger)
                    else:
                        _persist_non_tournament_match_completion(match, match_id, outcome, app.logger)

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
        except Exception as e:
            # Log the complete error with stack trace to execution.log
            app.logger.error(f"[NextBall] Error processing ball for match {match_id}: {e}", exc_info=True)
            
            # Also log to console for immediate visibility
            import traceback
            traceback.print_exc()
            
            # Return JSON error response instead of HTML 500 page
            return jsonify({
                "error": "An error occurred while processing the ball",
                "details": str(e),
                "match_id": match_id
            }), 500

    @app.route("/match/<match_id>/set-simulation-mode", methods=["POST"])
    @login_required
    def set_simulation_mode(match_id):
        data = request.get_json() or {}
        mode = str(data.get("mode", "auto")).lower()
        if mode not in {"auto", "manual"}:
            return jsonify({"error": "mode must be auto or manual"}), 400

        with _get_match_file_lock(match_id):
            match_data, match_path, err = _load_match_file_for_user(match_id)
            if err:
                return err

            match_data["simulation_mode"] = mode
            with open(match_path, "w", encoding="utf-8") as f:
                json.dump(match_data, f, indent=2)

        with MATCH_INSTANCES_LOCK:
            if match_id in MATCH_INSTANCES:
                match = MATCH_INSTANCES[match_id]
                if match.data.get("created_by") != current_user.id:
                    return jsonify({"error": "Unauthorized"}), 403
                match.simulation_mode = mode
                match.data["simulation_mode"] = mode

        return jsonify({"success": True, "mode": mode}), 200

    @app.route("/match/<match_id>/submit-decision", methods=["POST"])
    @login_required
    def submit_decision(match_id):
        payload = request.get_json() or {}
        selected_index = payload.get("selected_index")
        decision_type = payload.get("type")

        if selected_index is None:
            return jsonify({"error": "selected_index is required"}), 400

        with MATCH_INSTANCES_LOCK:
            if match_id not in MATCH_INSTANCES:
                match_data, _path, err = _load_match_file_for_user(match_id)
                if err:
                    return err
                if 'rain_probability' not in match_data:
                    match_data['rain_probability'] = load_config().get('rain_probability', 0.0)
                MATCH_INSTANCES[match_id] = Match(match_data)
            match = MATCH_INSTANCES[match_id]

        if match.data.get("created_by") != current_user.id:
            return jsonify({"error": "Unauthorized"}), 403

        if not match.pending_decision:
            return jsonify({"error": "No pending decision"}), 400
        if decision_type and decision_type != match.pending_decision.get("type"):
            return jsonify({"error": "Decision type mismatch"}), 400

        result, status_code = match.submit_pending_decision(selected_index)
        return jsonify(result), status_code
    

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
                # JSON cleaned up after archiving — try in-memory instance
                with MATCH_INSTANCES_LOCK:
                    inst = MATCH_INSTANCES.get(match_id)
                if inst and inst.data.get("created_by") == current_user.id:
                    match_meta = inst.data
                    app.logger.info(f"[DownloadArchive] Using in-memory match data for '{match_id}'")
                else:
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
            _temp_json_created = False
            if not original_json_path:
                # JSON cleaned up after archiving — write match_meta to temp file
                app.logger.info(f"[DownloadArchive] Original JSON cleaned up; writing temp file from match_meta")
                temp_dir = os.path.join(PROJECT_ROOT, "data", "matches")
                os.makedirs(temp_dir, exist_ok=True)
                original_json_path = os.path.join(temp_dir, f"_temp_{match_id}.json")
                with open(original_json_path, "w", encoding="utf-8") as f:
                    json.dump(match_meta, f, indent=2)
                _temp_json_created = True

            app.logger.debug(f"[DownloadArchive] Using JSON at '{original_json_path}'")

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
            finally:
                # Clean up temp JSON if we created one
                if _temp_json_created and os.path.isfile(original_json_path):
                    try:
                        os.remove(original_json_path)
                    except Exception:
                        pass

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
        match_history = []

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
                    created_at = datetime.fromtimestamp(os.path.getmtime(full_path))
                    expires_at = created_at + timedelta(days=7)

                    # Build URLs for download & delete
                    download_url = f"/archives/{username}/{fn}"
                    delete_url   = f"/archives/{username}/{fn}"
                    valid_files.append({
                        "filename":     fn,
                        "download_url": download_url,
                        "delete_url":   delete_url,
                        "created_at":   created_at,
                        "expires_at":   expires_at
                    })

                app.logger.info(f"User '{username}' has {len(valid_files)} valid archives")
                valid_files.sort(key=lambda x: x.get("created_at") or datetime.min, reverse=True)

        except Exception as e:
            app.logger.error(f"Error listing archives in '{files_dir}' for '{username}': {e}", exc_info=True)

        try:
            non_tournament_matches = (
                DBMatch.query
                .filter(
                    DBMatch.user_id == current_user.id,
                    DBMatch.tournament_id.is_(None)
                )
                .order_by(DBMatch.date.desc())
                .all()
            )

            team_ids = set()
            for m in non_tournament_matches:
                if m.home_team_id:
                    team_ids.add(m.home_team_id)
                if m.away_team_id:
                    team_ids.add(m.away_team_id)

            teams_by_id = {}
            if team_ids:
                teams_by_id = {
                    t.id: t for t in DBTeam.query.filter(DBTeam.id.in_(team_ids)).all()
                }

            for m in non_tournament_matches:
                home_name = teams_by_id.get(m.home_team_id).name if m.home_team_id in teams_by_id else "Home"
                away_name = teams_by_id.get(m.away_team_id).name if m.away_team_id in teams_by_id else "Away"
                match_history.append({
                    "match_id": m.id,
                    "home_team": home_name,
                    "away_team": away_name,
                    "result_description": m.result_description or "Match Completed",
                    "played_at": m.date,
                    "scoreline": (
                        f"{home_name} {m.home_team_score or 0}/{m.home_team_wickets or 0} "
                        f"({m.home_team_overs or '0.0'}) vs "
                        f"{away_name} {m.away_team_score or 0}/{m.away_team_wickets or 0} "
                        f"({m.away_team_overs or '0.0'})"
                    ),
                    "scoreboard_url": url_for("view_scoreboard", match_id=m.id),
                })
        except Exception as e:
            app.logger.error(f"Error loading non-tournament match history for '{username}': {e}", exc_info=True)

        return render_template("my_matches.html", files=valid_files, match_history=match_history)


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
        


    # Note: Database backup endpoint has been moved to admin routes section
    # See /admin/backup-database route above with admin_required decorator



    # ============================================================================
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
                # JSON may be cleaned up after archiving — check in-memory or DB
                authorized = False
                with MATCH_INSTANCES_LOCK:
                    if match_id in MATCH_INSTANCES:
                        authorized = MATCH_INSTANCES[match_id].data.get("created_by") == current_user.id
                if not authorized:
                    db_match = DBMatch.query.filter_by(id=match_id, user_id=current_user.id).first()
                    authorized = db_match is not None
                if not authorized:
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
                    .filter(DBTeam.id.in_(team_ids), DBTeam.is_placeholder != True)
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
            DBTeam.is_placeholder != True
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
            # Clean up all matches linked to this tournament before
            # deleting it, so scorecards, partnerships, player career
            # stats, and JSON files are properly removed.
            tournament_matches = DBMatch.query.filter_by(tournament_id=tournament_id).all()
            for m in tournament_matches:
                # Reverse player career stats
                scorecards = MatchScorecard.query.filter_by(match_id=m.id).all()
                if scorecards:
                    reverse_player_aggregates(scorecards, logger=app.logger)
                # Delete dependent rows
                db.session.query(MatchPartnership).filter_by(match_id=m.id).delete(synchronize_session=False)
                db.session.query(MatchScorecard).filter_by(match_id=m.id).delete(synchronize_session=False)
                # Delete JSON file on disk
                match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
                if os.path.isdir(match_dir):
                    for fn in os.listdir(match_dir):
                        if fn.endswith(".json"):
                            path = os.path.join(match_dir, fn)
                            try:
                                with open(path, "r", encoding="utf-8") as f:
                                    data = json.load(f)
                                if data.get("match_id") == m.id:
                                    os.remove(path)
                                    break
                            except Exception:
                                continue
                # Remove from in-memory cache
                with MATCH_INSTANCES_LOCK:
                    MATCH_INSTANCES.pop(m.id, None)
                db.session.delete(m)

            db.session.delete(t)
            db.session.commit()
            flash("Tournament deleted successfully.", "success")
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
                # Reverse player career stats BEFORE deleting scorecards
                old_scorecards = MatchScorecard.query.filter_by(match_id=match_id).all()
                if old_scorecards:
                    reverse_player_aggregates(old_scorecards, logger=app.logger)
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
                            os.remove(path)
                            app.logger.info(f"Deleted match JSON: {fn}")
                            break
                    except Exception:
                        continue

            # 4. Clear In-Memory Instance
            with MATCH_INSTANCES_LOCK:  # Bug Fix B2: Thread-safe deletion
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
            teams = Team.query.filter_by(user_id=current_user.id).filter(
                Team.is_placeholder != True
            ).all()
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
            ).outerjoin(
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



    # Register minimal fallback admin routes if any expected endpoints are missing.
    # This prevents template/url build failures when a partial app initialization occurs.
    def _register_admin_fallback(endpoint_name, route_path):
        if endpoint_name in app.view_functions:
            return

        def _missing_admin_route():
            app.logger.warning(f"[Admin] Fallback route hit for missing endpoint: {endpoint_name}")
            flash(f"{endpoint_name.replace('_', ' ').title()} is unavailable in this process.", "warning")
            return redirect('/admin/dashboard')

        app.add_url_rule(
            route_path,
            endpoint=endpoint_name,
            view_func=login_required(admin_required(_missing_admin_route))
        )

    _register_admin_fallback('admin_dashboard', '/admin/dashboard')
    _register_admin_fallback('admin_users', '/admin/users')
    _register_admin_fallback('admin_activity', '/admin/activity')
    _register_admin_fallback('admin_health', '/admin/health')
    _register_admin_fallback('admin_matches', '/admin/matches')
    _register_admin_fallback('admin_database_stats', '/admin/database/stats')
    _register_admin_fallback('admin_backups', '/admin/backups')
    _register_admin_fallback('admin_config', '/admin/config')
    _register_admin_fallback('admin_audit_log', '/admin/audit-log')

    @app.route('/admin')
    @login_required
    @admin_required
    def admin_root():
        return redirect('/admin/dashboard')

    @app.route('/admin/<path:subpath>')
    @login_required
    @admin_required
    def admin_route_catchall(subpath):
        requested = f"/admin/{subpath}"
        known_routes = sorted([r.rule for r in app.url_map.iter_rules() if r.rule.startswith('/admin')])
        app.logger.error(
            f"[Admin] Unmatched admin route: {requested}. Known admin routes: {known_routes}. File: {os.path.abspath(__file__)}"
        )

        # Avoid redirect loops if dashboard route itself is unavailable.
        if requested != '/admin/dashboard' and '/admin/dashboard' in known_routes:
            return redirect('/admin/dashboard')

        return (
            "Admin route unavailable in this running process.\n"
            f"Requested: {requested}\n"
            f"Running file: {os.path.abspath(__file__)}\n"
            f"Known admin routes: {', '.join(known_routes)}",
            503,
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    return app


# WSGI entrypoint used by gunicorn/flask CLI.
app = create_app()


def _ensure_route(rule, endpoint, view_func, methods=None):
    if any(r.rule == rule for r in app.url_map.iter_rules()):
        return
    app.add_url_rule(rule, endpoint=endpoint, view_func=view_func, methods=methods or ["GET"])


def _admin_guard_or_redirect():
    if not current_user.is_authenticated:
        return redirect(url_for("login", next=request.path))
    if not getattr(current_user, "is_admin", False):
        return jsonify({"error": "Forbidden: Admin access required"}), 403
    return None


def _global_admin_fallback(target_label):
    guard = _admin_guard_or_redirect()
    if guard is not None:
        return guard
    flash(f"{target_label} is unavailable in this process.", "warning")
    return redirect("/admin/dashboard")


_ensure_route(
    "/admin/activity",
    "admin_activity_global_fallback",
    lambda: _global_admin_fallback("Activity"),
)
_ensure_route(
    "/admin/health",
    "admin_health_global_fallback",
    lambda: _global_admin_fallback("System Health"),
)
_ensure_route(
    "/admin/backups",
    "admin_backups_global_fallback",
    lambda: _global_admin_fallback("Backups"),
)
_ensure_route(
    "/admin/database/stats",
    "admin_database_stats_global_fallback",
    lambda: _global_admin_fallback("Database"),
)
_ensure_route(
    "/admin/config",
    "admin_config_global_fallback",
    lambda: _global_admin_fallback("Config"),
)
_ensure_route(
    "/admin/matches",
    "admin_matches_global_fallback",
    lambda: _global_admin_fallback("Matches"),
)
_ensure_route(
    "/admin/audit-log",
    "admin_audit_log_global_fallback",
    lambda: _global_admin_fallback("Audit Log"),
)


def _global_probe():
    admin_routes = sorted([r.rule for r in app.url_map.iter_rules() if r.rule.startswith("/admin")])
    return jsonify(
        {
            "probe": "simcricketx-global-route-guarantee",
            "file": os.path.abspath(__file__),
            "admin_route_count": len(admin_routes),
            "has_admin_activity": "/admin/activity" in admin_routes,
            "has_admin_health": "/admin/health" in admin_routes,
            "admin_routes": admin_routes,
        }
    ), 200


_ensure_route("/__codex_probe", "codex_probe_global", _global_probe)
_ensure_route("/__codex_probe.", "codex_probe_global_dot", _global_probe)

# ?????? Run Server ??????
if __name__ == "__main__":
    import socket
    import webbrowser
    import os
    import traceback
    import threading

    try:
        # Choose host based on environment
        hostname = socket.gethostname()
        ip_address = socket.gethostbyname(hostname)
        ENV = os.getenv("ENV", "dev").lower()

        is_local = ip_address.startswith("127.") or ENV == "dev"

        HOST = "127.0.0.1" if is_local else "0.0.0.0"
        PORT = 7860
        url = f"http://{HOST}:{PORT}"

        # Startup diagnostics to catch path/route mismatch in dev.
        admin_rules = sorted(
            [r.rule for r in app.url_map.iter_rules() if r.rule.startswith('/admin')]
        )
        required_routes = ['/__codex_probe', '/admin/dashboard', '/admin/activity', '/admin/health']
        missing_required = [r for r in required_routes if not any(rule.rule == r for rule in app.url_map.iter_rules())]
        if missing_required:
            raise RuntimeError(
                f"Startup route check failed. Missing routes: {missing_required}. "
                f"Admin routes seen: {admin_rules}"
            )
        print(f"[INFO] Running from: {os.path.abspath(__file__)}")
        print(f"[INFO] Registered admin routes: {len(admin_rules)}")
        for route in admin_rules:
            print(f"  - {route}")

        # Console info
        print("[OK] SimCricketX is up and running!")
        print(f"[WEB] Access the app at: {url}")
        print("[INFO] Press Ctrl+C to stop the server.\n")

        # Cleanup tasks
        # Cleanup tasks
        with app.app_context():
            cleanup_temp_scorecard_images()
        threading.Thread(target=periodic_cleanup, args=(app,), daemon=True).start()

        # Open browser for local use only
        if is_local:
            webbrowser.open_new_tab(url)

        # Run Flask app
        # C6: Use computed HOST (127.0.0.1 for local/dev, 0.0.0.0 for prod)
        app.run(host=HOST, port=PORT, debug=is_local, use_reloader=False)

    except Exception as e:
        print("[ERROR] Failed to start SimCricketX:")
        traceback.print_exc()
