import os
import json
import logging
import yaml
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler
from utils.helpers import load_config
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
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
from flask import Flask, render_template, redirect, url_for, flash
from flask_login import LoginManager, login_required, logout_user, current_user
from flask import session
from flask import request, render_template, flash, redirect, url_for
from flask_login import login_required, current_user
from engine.team import Team, save_team, PITCH_PREFERENCES
from engine.player import Player, PLAYER_ROLES, BATTING_HANDS, BOWLING_TYPES, BOWLING_HANDS
from auth.user_auth import load_credentials  # adjust import path if needed
from flask import send_from_directory
import random
import shutil
from flask import send_file
from flask import Flask, request, jsonify, send_file
import time
import threading
import logging
import sys
import traceback
from flask_login import UserMixin

# Add this import for system monitoring
try:
    import psutil
except ImportError:
    psutil = None



MATCH_INSTANCES = {}

# How old is “too old”? 7 days → 7*24*3600 seconds
PROD_MAX_AGE = 7 * 24 * 3600

# Make sure PROJECT_ROOT is defined near the top of app.py:
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
CREDENTIALS_FILE = 'auth/credentials.json'

# ===== AUTHENTICATION DEBUG LOGGER SETUP =====
def setup_auth_debug_logger():
    """
    Create a dedicated logger for authentication debugging.
    Logs to 'auth_debug.log' in the root folder with extensive detail.
    """
    auth_logger = logging.getLogger('auth_debug')
    auth_logger.setLevel(logging.DEBUG)

    # Prevent duplicate handlers
    if auth_logger.handlers:
        return auth_logger

    # Create file handler for auth debug logs with UTF-8 encoding
    log_path = os.path.join(PROJECT_ROOT, "auth_debug.log")
    handler = RotatingFileHandler(
        log_path,
        maxBytes=50*1024*1024,  # 50MB
        backupCount=10,
        encoding='utf-8'  # <-- ADD THIS LINE
    )
    handler.setLevel(logging.DEBUG)

    # Create detailed formatter
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s:%(lineno)d - %(funcName)s() - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)

    # Add handler to logger
    auth_logger.addHandler(handler)

    # Also log to console for immediate feedback, with UTF-8 encoding
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    # Set encoding for the console handler
    console_handler.setFormatter(logging.Formatter(
        '[AUTH-DEBUG] %(asctime)s - %(message)s',
        datefmt='%H:%M:%S'
    ))
    # Python 3.9+ allows setting encoding directly. For broader compatibility,
    # we rely on the handler using the system's encoding but this is where
    # the error originates. A better fix is to handle it globally.
    # The file handler fix is most important for logs you want to keep.

    auth_logger.addHandler(console_handler)

    return auth_logger

# Initialize the auth debug logger
auth_debug_logger = setup_auth_debug_logger()


# Helper function to log request details
def log_request_details(request, auth_debug_logger, operation):
    """Log comprehensive request details for debugging"""
    try:
        auth_debug_logger.info(f"=== {operation.upper()} REQUEST STARTED ===")
        auth_debug_logger.debug(f"Request method: {request.method}")
        auth_debug_logger.debug(f"Request URL: {request.url}")
        auth_debug_logger.debug(f"Request endpoint: {request.endpoint}")
        auth_debug_logger.debug(f"Request remote_addr: {request.remote_addr}")
        auth_debug_logger.debug(f"Request user_agent: {request.headers.get('User-Agent', 'Unknown')}")
        auth_debug_logger.debug(f"Request referrer: {request.headers.get('Referer', 'None')}")
        
        # Log form data (excluding sensitive info)
        if request.method == "POST":
            form_keys = list(request.form.keys()) if request.form else []
            auth_debug_logger.debug(f"Form fields present: {form_keys}")
            
        # Log session info
        auth_debug_logger.debug(f"Session keys: {list(session.keys())}")
        auth_debug_logger.debug(f"Current user authenticated: {current_user.is_authenticated if hasattr(current_user, 'is_authenticated') else 'Unknown'}")
        
    except Exception as e:
        auth_debug_logger.error(f"Error logging request details: {e}")


# Helper function to log system state
def log_system_state(auth_debug_logger, operation):
    """Log current system state for debugging"""
    try:
        auth_debug_logger.info(f"=== {operation.upper()} SYSTEM STATE ===")
        
        # Check file system
        auth_debug_logger.debug(f"PROJECT_ROOT: {PROJECT_ROOT}")
        auth_debug_logger.debug(f"CREDENTIALS_FILE path: {CREDENTIALS_FILE}")
        auth_debug_logger.debug(f"Credentials file exists: {os.path.exists(CREDENTIALS_FILE)}")
        
        if os.path.exists(CREDENTIALS_FILE):
            file_size = os.path.getsize(CREDENTIALS_FILE)
            auth_debug_logger.debug(f"Credentials file size: {file_size} bytes")
            
            # Check file permissions
            auth_debug_logger.debug(f"Credentials file readable: {os.access(CREDENTIALS_FILE, os.R_OK)}")
            auth_debug_logger.debug(f"Credentials file writable: {os.access(CREDENTIALS_FILE, os.W_OK)}")
        
        # Check auth directory
        auth_dir = os.path.join(PROJECT_ROOT, "auth")
        auth_debug_logger.debug(f"Auth directory exists: {os.path.exists(auth_dir)}")
        auth_debug_logger.debug(f"Auth directory writable: {os.access(auth_dir, os.W_OK) if os.path.exists(auth_dir) else 'N/A'}")
        
        # Memory info
        import psutil
        if psutil:
            memory = psutil.virtual_memory()
            auth_debug_logger.debug(f"System memory usage: {memory.percent}%")
            auth_debug_logger.debug(f"Available memory: {memory.available / (1024*1024):.1f} MB")
        
    except Exception as e:
        auth_debug_logger.error(f"Error logging system state: {e}")


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

# ────── App Factory ──────
def create_app():
    # --- Flask setup ---
    app = Flask(__name__)
    config = load_config()

    @app.before_request
    def configure_session_cookie():
        is_secure = request.is_secure or (request.headers.get('X-Forwarded-Proto') == 'https')
        app.config["SESSION_COOKIE_SECURE"] = is_secure
        app.logger.info(f"[Session] Setting SESSION_COOKIE_SECURE to {is_secure} (HTTPS: {request.scheme}, X-Forwarded-Proto: {request.headers.get('X-Forwarded-Proto')})")
    

    # 1a) Try to read from config file
    secret = None
    try:
        secret = config.get("app", {}).get("secret_key", None)
        if not secret or not isinstance(secret, str):
            raise ValueError("Invalid secret_key in config")
    except Exception as e:
        # Log if config file is missing or malformed
        print(f"[WARN] Could not read secret_key from config.yaml: {e}")

    # 1b) Fallback to an environment variable (HF Secrets) or hardcoded fallback
    if not secret:
        secret = os.getenv("FLASK_SECRET_KEY", None)
        if not secret:
            # Last fallback (non-secure): random bytes on each start
            # WARNING: this means users must re-login after each rebuild!
            secret = os.urandom(24).hex()
            print("[WARN] Using random Flask SECRET_KEY—sessions won't persist across restarts")

    app.config["SECRET_KEY"] = secret

    # 1c) Force cookies to be secure (HF uses HTTPS)
    # app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # --- Logging setup ---
    base_dir = os.path.abspath(os.path.dirname(__file__))
    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "execution.log")

    # Add encoding='utf-8' to the handler
    handler = RotatingFileHandler(
        log_path,
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'  # <-- ADD THIS LINE
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]"
    ))
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)

    # --- Flask-Login setup ---
    login_manager = LoginManager(app)
    login_manager.login_view = "login"

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

    # ───── Routes ─────

    @app.route("/")
    @login_required
    def home():
        print(f"[DEBUG] current_user.is_authenticated = {current_user.is_authenticated}")
        print(f"[DEBUG] current_user.get_id() = {current_user.get_id()}")
        return render_template("home.html", user=current_user)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        """
        Enhanced registration route with comprehensive debugging
        """
        global auth_debug_logger
        
        # Initialize debug session
        debug_session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        auth_debug_logger.info(f"🔥🔥🔥 REGISTRATION SESSION {debug_session_id} STARTED 🔥🔥🔥")
        
        try:
            # Log request details
            log_request_details(request, auth_debug_logger, "registration")
            log_system_state(auth_debug_logger, "registration")
            
            if request.method == "GET":
                auth_debug_logger.info(f"[{debug_session_id}] GET request - serving registration form")
                auth_debug_logger.debug(f"[{debug_session_id}] Rendering registration template")
                return render_template("register.html")
            
            # POST request processing
            auth_debug_logger.info(f"[{debug_session_id}] POST request - processing registration")
            
            # Extract form data with extensive validation
            auth_debug_logger.debug(f"[{debug_session_id}] Extracting form data...")
            
            try:
                raw_email = request.form.get("email", "")
                raw_password = request.form.get("password", "")
                
                auth_debug_logger.debug(f"[{debug_session_id}] Raw email extracted: '{raw_email}' (type: {type(raw_email)}, length: {len(raw_email) if raw_email else 0})")
                auth_debug_logger.debug(f"[{debug_session_id}] Raw password extracted: [REDACTED] (type: {type(raw_password)}, length: {len(raw_password) if raw_password else 0})")
                
                # Process email
                if not raw_email:
                    auth_debug_logger.error(f"[{debug_session_id}] ❌ Email field is empty or missing")
                    flash("❌ Email is required!", "danger")
                    return render_template("register.html")
                
                email = raw_email.strip().lower()
                auth_debug_logger.debug(f"[{debug_session_id}] Processed email: '{email}' (length: {len(email)})")
                
                # Validate email format
                if "@" not in email or "." not in email:
                    auth_debug_logger.error(f"[{debug_session_id}] ❌ Invalid email format: '{email}'")
                    flash("❌ Invalid email format!", "danger")
                    return render_template("register.html")
                
                # Process password
                if not raw_password:
                    auth_debug_logger.error(f"[{debug_session_id}] ❌ Password field is empty or missing")
                    flash("❌ Password is required!", "danger")
                    return render_template("register.html")
                
                password = raw_password
                auth_debug_logger.debug(f"[{debug_session_id}] Password validation passed (length: {len(password)})")
                
                # Log before registration attempt
                auth_debug_logger.info(f"[{debug_session_id}] 🚀 Attempting user registration for: '{email}'")
                auth_debug_logger.debug(f"[{debug_session_id}] Calling register_user function...")
                
                # Call register_user with timing
                registration_start = datetime.now()
                auth_debug_logger.debug(f"[{debug_session_id}] Registration start time: {registration_start}")
                
                try:
                    registration_result = register_user(email, password)
                    registration_end = datetime.now()
                    registration_duration = (registration_end - registration_start).total_seconds()
                    
                    auth_debug_logger.debug(f"[{debug_session_id}] Registration end time: {registration_end}")
                    auth_debug_logger.debug(f"[{debug_session_id}] Registration duration: {registration_duration:.3f} seconds")
                    auth_debug_logger.info(f"[{debug_session_id}] register_user returned: {registration_result}")
                    
                    if registration_result:
                        auth_debug_logger.info(f"[{debug_session_id}] ✅ Registration successful for: '{email}'")
                        
                        # Log post-registration state
                        auth_debug_logger.debug(f"[{debug_session_id}] Checking post-registration state...")
                        
                        # Check if user appears in credentials now
                        try:
                            from auth.user_auth import load_credentials
                            post_reg_creds = load_credentials()
                            if email in post_reg_creds:
                                auth_debug_logger.info(f"[{debug_session_id}] ✅ User found in local credentials after registration")
                                user_data_keys = list(post_reg_creds[email].keys())
                                auth_debug_logger.debug(f"[{debug_session_id}] User data fields: {user_data_keys}")
                            else:
                                auth_debug_logger.warning(f"[{debug_session_id}] ⚠️ User NOT found in local credentials after registration")
                                auth_debug_logger.debug(f"[{debug_session_id}] Available users in credentials: {list(post_reg_creds.keys())}")
                        except Exception as cred_check_error:
                            auth_debug_logger.error(f"[{debug_session_id}] ❌ Error checking post-registration credentials: {cred_check_error}")
                            auth_debug_logger.debug(f"[{debug_session_id}] Credentials check traceback: {traceback.format_exc()}")
                        
                        # Flash success message and redirect
                        auth_debug_logger.debug(f"[{debug_session_id}] Setting success flash message")
                        flash("✅ Registration successful! Please log in.", "success")
                        
                        auth_debug_logger.debug(f"[{debug_session_id}] Redirecting to login page")
                        return redirect(url_for("login"))
                        
                    else:
                        auth_debug_logger.warning(f"[{debug_session_id}] ⚠️ Registration failed for: '{email}' - User already exists")
                        auth_debug_logger.debug(f"[{debug_session_id}] Setting error flash message")
                        flash("❌ User already exists!", "danger")
                        
                except Exception as reg_func_error:
                    auth_debug_logger.error(f"[{debug_session_id}] ❌ Exception in register_user function: {reg_func_error}")
                    auth_debug_logger.error(f"[{debug_session_id}] Registration function traceback: {traceback.format_exc()}")
                    flash("❌ Registration failed due to system error!", "danger")
                    
            except Exception as form_error:
                auth_debug_logger.error(f"[{debug_session_id}] ❌ Error processing form data: {form_error}")
                auth_debug_logger.error(f"[{debug_session_id}] Form processing traceback: {traceback.format_exc()}")
                flash("❌ Error processing registration form!", "danger")
            
            # Return to registration form
            auth_debug_logger.debug(f"[{debug_session_id}] Returning to registration template")
            return render_template("register.html")
            
        except Exception as route_error:
            auth_debug_logger.critical(f"[{debug_session_id}] 💥 CRITICAL ERROR in registration route: {route_error}")
            auth_debug_logger.critical(f"[{debug_session_id}] Route error traceback: {traceback.format_exc()}")
            flash("❌ System error during registration!", "danger")
            return render_template("register.html")
            
        finally:
            auth_debug_logger.info(f"🏁🏁🏁 REGISTRATION SESSION {debug_session_id} COMPLETED 🏁🏁🏁")


    @app.route("/login", methods=["GET", "POST"])
    def login():
        """
        Enhanced login route with comprehensive debugging
        """
        global auth_debug_logger
        
        # Initialize debug session
        debug_session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        auth_debug_logger.info(f"🔥🔥🔥 LOGIN SESSION {debug_session_id} STARTED 🔥🔥🔥")
        
        try:
            # Log request details
            log_request_details(request, auth_debug_logger, "login")
            log_system_state(auth_debug_logger, "login")
            
            if request.method == "GET":
                auth_debug_logger.info(f"[{debug_session_id}] GET request - serving login form")
                auth_debug_logger.debug(f"[{debug_session_id}] Checking current user state...")
                
                if current_user.is_authenticated:
                    auth_debug_logger.info(f"[{debug_session_id}] User already authenticated: {current_user.id}")
                    auth_debug_logger.debug(f"[{debug_session_id}] Redirecting authenticated user to home")
                    return redirect(url_for("home"))
                
                auth_debug_logger.debug(f"[{debug_session_id}] Rendering login template")
                return render_template("login.html")
            
            # POST request processing
            auth_debug_logger.info(f"[{debug_session_id}] POST request - processing login")
            
            # Extract form data with extensive validation
            auth_debug_logger.debug(f"[{debug_session_id}] Extracting form data...")
            
            try:
                raw_email = request.form.get("email", "")
                raw_password = request.form.get("password", "")
                
                auth_debug_logger.debug(f"[{debug_session_id}] Raw email extracted: '{raw_email}' (type: {type(raw_email)}, length: {len(raw_email) if raw_email else 0})")
                auth_debug_logger.debug(f"[{debug_session_id}] Raw password extracted: [REDACTED] (type: {type(raw_password)}, length: {len(raw_password) if raw_password else 0})")
                
                # Process email
                if not raw_email:
                    auth_debug_logger.error(f"[{debug_session_id}] ❌ Email field is empty or missing")
                    flash("❌ Email is required!", "danger")
                    return render_template("login.html")
                
                email = raw_email.strip().lower()
                auth_debug_logger.debug(f"[{debug_session_id}] Processed email: '{email}' (length: {len(email)})")
                
                # Validate email format
                if "@" not in email or "." not in email:
                    auth_debug_logger.error(f"[{debug_session_id}] ❌ Invalid email format: '{email}'")
                    flash("❌ Invalid email format!", "danger")
                    return render_template("login.html")
                
                # Process password
                if not raw_password:
                    auth_debug_logger.error(f"[{debug_session_id}] ❌ Password field is empty or missing")
                    flash("❌ Password is required!", "danger")
                    return render_template("login.html")
                
                password = raw_password
                auth_debug_logger.debug(f"[{debug_session_id}] Password validation passed (length: {len(password)})")
                
                # Check local credentials before verification
                auth_debug_logger.debug(f"[{debug_session_id}] Pre-verification credentials check...")
                try:
                    from auth.user_auth import load_credentials
                    pre_verify_creds = load_credentials()
                    auth_debug_logger.debug(f"[{debug_session_id}] Total users in local credentials: {len(pre_verify_creds)}")
                    auth_debug_logger.debug(f"[{debug_session_id}] Users in credentials: {list(pre_verify_creds.keys())}")
                    
                    if email in pre_verify_creds:
                        auth_debug_logger.info(f"[{debug_session_id}] ✅ User found in local credentials before verification")
                        user_data_keys = list(pre_verify_creds[email].keys())
                        auth_debug_logger.debug(f"[{debug_session_id}] User data fields: {user_data_keys}")
                    else:
                        auth_debug_logger.warning(f"[{debug_session_id}] ⚠️ User NOT found in local credentials before verification")
                        auth_debug_logger.info(f"[{debug_session_id}] Will attempt Google Sheets fallback during verification")
                        
                except Exception as pre_cred_error:
                    auth_debug_logger.error(f"[{debug_session_id}] ❌ Error loading pre-verification credentials: {pre_cred_error}")
                    auth_debug_logger.debug(f"[{debug_session_id}] Pre-verification credentials error traceback: {traceback.format_exc()}")
                
                # Log before verification attempt
                auth_debug_logger.info(f"[{debug_session_id}] 🚀 Attempting user verification for: '{email}'")
                auth_debug_logger.debug(f"[{debug_session_id}] Calling verify_user function...")
                
                # Call verify_user with timing
                verification_start = datetime.now()
                auth_debug_logger.debug(f"[{debug_session_id}] Verification start time: {verification_start}")
                
                try:
                    verification_result = verify_user(email, password)
                    verification_end = datetime.now()
                    verification_duration = (verification_end - verification_start).total_seconds()
                    
                    auth_debug_logger.debug(f"[{debug_session_id}] Verification end time: {verification_end}")
                    auth_debug_logger.debug(f"[{debug_session_id}] Verification duration: {verification_duration:.3f} seconds")
                    auth_debug_logger.info(f"[{debug_session_id}] verify_user returned: {verification_result}")
                    
                    if verification_result:
                        auth_debug_logger.info(f"[{debug_session_id}] ✅ Verification successful for: '{email}'")
                        
                        # Log post-verification state
                        auth_debug_logger.debug(f"[{debug_session_id}] Checking post-verification state...")
                        
                        # Check if user appears in credentials now (in case of Google Sheets sync)
                        try:
                            post_verify_creds = load_credentials()
                            if email in post_verify_creds:
                                auth_debug_logger.info(f"[{debug_session_id}] ✅ User found in local credentials after verification")
                                user_data_keys = list(post_verify_creds[email].keys())
                                auth_debug_logger.debug(f"[{debug_session_id}] User data fields: {user_data_keys}")
                            else:
                                auth_debug_logger.warning(f"[{debug_session_id}] ⚠️ User still NOT found in local credentials after verification")
                                auth_debug_logger.debug(f"[{debug_session_id}] Available users in credentials: {list(post_verify_creds.keys())}")
                        except Exception as post_cred_error:
                            auth_debug_logger.error(f"[{debug_session_id}] ❌ Error checking post-verification credentials: {post_cred_error}")
                            auth_debug_logger.debug(f"[{debug_session_id}] Post-verification credentials check traceback: {traceback.format_exc()}")
                        
                        # Create user object and login
                        auth_debug_logger.debug(f"[{debug_session_id}] Creating User object for: '{email}'")
                        try:
                            # Use the User class that's defined in the create_app() scope
                            user = User(email)
                            auth_debug_logger.debug(f"[{debug_session_id}] User object created: {user}")
                            auth_debug_logger.debug(f"[{debug_session_id}] User ID: {user.id}")
                            
                            auth_debug_logger.debug(f"[{debug_session_id}] Calling login_user...")
                            login_user(user)
                            auth_debug_logger.info(f"[{debug_session_id}] ✅ login_user completed successfully")
                            
                            # Clear any existing flash messages
                            auth_debug_logger.debug(f"[{debug_session_id}] Clearing existing flash messages")
                            session.pop('_flashes', None)
                            
                            # Set success flash message
                            auth_debug_logger.debug(f"[{debug_session_id}] Setting success flash message")
                            flash("✅ Logged in successfully!", "success")
                            
                            # Log successful login
                            auth_debug_logger.info(f"[{debug_session_id}] ✅ LOGIN SUCCESSFUL for: '{email}'")
                            app.logger.info(f"Successful login for {email}")
                            
                            auth_debug_logger.debug(f"[{debug_session_id}] Redirecting to home page")
                            return redirect(url_for("home"))
                            
                        except Exception as login_obj_error:
                            auth_debug_logger.error(f"[{debug_session_id}] ❌ Error creating user object or logging in: {login_obj_error}")
                            auth_debug_logger.error(f"[{debug_session_id}] Login object error traceback: {traceback.format_exc()}")
                            flash("❌ Login system error!", "danger")
                        
                    else:
                        auth_debug_logger.warning(f"[{debug_session_id}] ⚠️ Verification failed for: '{email}'")
                        auth_debug_logger.debug(f"[{debug_session_id}] Setting error flash message")
                        flash("❌ Invalid email or password.", "danger")
                        
                except Exception as verify_func_error:
                    auth_debug_logger.error(f"[{debug_session_id}] ❌ Exception in verify_user function: {verify_func_error}")
                    auth_debug_logger.error(f"[{debug_session_id}] Verification function traceback: {traceback.format_exc()}")
                    flash("❌ Login failed due to system error!", "danger")
                    
            except Exception as form_error:
                auth_debug_logger.error(f"[{debug_session_id}] ❌ Error processing form data: {form_error}")
                auth_debug_logger.error(f"[{debug_session_id}] Form processing traceback: {traceback.format_exc()}")
                flash("❌ Error processing login form!", "danger")
            
            # Return to login form
            auth_debug_logger.debug(f"[{debug_session_id}] Returning to login template")
            return render_template("login.html")
            
        except Exception as route_error:
            auth_debug_logger.critical(f"[{debug_session_id}] 💥 CRITICAL ERROR in login route: {route_error}")
            auth_debug_logger.critical(f"[{debug_session_id}] Route error traceback: {traceback.format_exc()}")
            flash("❌ System error during login!", "danger")
            return render_template("login.html")
            
        finally:
            auth_debug_logger.info(f"🏁🏁🏁 LOGIN SESSION {debug_session_id} COMPLETED 🏁🏁🏁")

    @app.route("/debug-auth-logs")
    def debug_auth_logs():
        """Debug route to view recent auth logs"""
        try:
            log_path = os.path.join(PROJECT_ROOT, "auth_debug.log")
            if os.path.exists(log_path):
                with open(log_path, 'r') as f:
                    logs = f.read()
                return f"<pre>{logs}</pre>"
            else:
                return "Auth debug log file not found"
        except Exception as e:
            return f"Error reading auth debug logs: {e}"


    @app.route("/download-auth-logs")
    def download_auth_logs():
        """Download the auth debug log file"""
        try:
            log_path = os.path.join(PROJECT_ROOT, "auth_debug.log")
            if os.path.exists(log_path):
                return send_file(
                    log_path, 
                    mimetype="text/plain",
                    as_attachment=True,
                    download_name="auth_debug.log"
                )
            else:
                return jsonify({"error": "Auth debug log file not found"}), 404
        except Exception as e:
            return jsonify({"error": f"Failed to download auth logs: {str(e)}"}), 500

    @app.route("/delete_account", methods=["POST"])
    @login_required
    def delete_account():
        email = current_user.id
        app.logger.info(f"Account deletion requested for {email}")
        if delete_user(email):
            logout_user()
            flash("Your account has been deleted.", "info")
            return redirect(url_for("register"))
        else:
            flash("Account deletion failed.", "danger")
            return redirect(url_for("home"))

    @app.route("/logout")
    @login_required
    def logout():
        app.logger.info(f"Logout for {current_user.id}")
        logout_user()
        session.pop('_flashes', None)  # ⬅️ Clear previous flash messages
        flash("✅ You have been logged out.", "success")
        return redirect(url_for("login"))
    
    def load_user_teams(user_email):
        """Return list of team dicts created by this user."""
        teams = []
        teams_dir = os.path.join(PROJECT_ROOT, "data", "teams")
        if os.path.isdir(teams_dir):
            for fn in os.listdir(teams_dir):
                if not fn.endswith(".json"): continue
                # Expecting filenames like SHORT_user@example.com.json
                if not fn.endswith(f"_{user_email}.json"):
                    continue
                path = os.path.join(teams_dir, fn)
                try:
                    with open(path) as f:
                        data = json.load(f)
                    # Override short_code for UI
                    data["short_code"] = fn.rsplit("_",1)[0]
                    teams.append(data)
                except Exception as e:
                    app.logger.error(f"Error loading team {fn}: {e}", exc_info=True)
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
                    flash("❌ All team fields are required.", "danger")
                    return render_template("team_create.html")

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
                        flash(f"❌ Error in player {i+1}: {e}", "danger")
                        app.logger.error(f"Error in player creation: {e}", exc_info=True)
                        return render_template("team_create.html")

                # Validate player count
                if len(players) < 15 or len(players) > 18:
                    flash("❌ You must enter between 15 and 18 players.", "danger")
                    return render_template("team_create.html")
                
                # Validate at least 1 wicketkeeper
                wk_count = sum(1 for p in players if p.role == "Wicketkeeper")
                if wk_count < 1:
                    flash("❌ You need at least one Wicketkeeper.", "danger")
                    return render_template("team_create.html")

                # Validate minimum 6 bowlers/all-rounders
                bowl_count = sum(1 for p in players if p.role in ["Bowler", "All-rounder"])
                if bowl_count < 6:
                    flash("❌ You need at least six Bowler/All-rounder roles.", "danger")
                    return render_template("team_create.html")
                
                # Read team color
                color = request.form["team_color"]

                # For now: auto-pick captain and wicketkeeper as first ones matching
                captain = next((p.name for p in players if p.role in ["Batsman", "All-rounder", "Wicketkeeper"]), players[0].name)
                wicketkeeper = next((p.name for p in players if p.role == "Wicketkeeper"), None)
                if not wicketkeeper:
                    flash("❌ At least one player must be a Wicketkeeper.", "danger")
                    return render_template("team_create.html")

                # 3. Create and save team
                team = Team(
                    name=name,
                    short_code=short_code,
                    home_ground=home_ground,
                    pitch_preference=pitch,
                    team_color=color,
                    players=players,
                    captain=captain,
                    wicketkeeper=wicketkeeper
                )

                # 3a. Load local credentials and grab user_id & email
                creds = load_credentials()
                user_record = creds.get(current_user.id, {})
                user_id = user_record.get("user_id")
                email   = current_user.id

                # 3b. Build the raw dict and inject the metadata
                data = team.to_dict()
                data["created_by_user_id"] = user_id
                data["created_by_email"]   = email

                # 3c. Write it manually instead of save_team()
                import os, json
                filename = f"{team.short_code}_{current_user.id}.json"
                path = os.path.join("data", "teams", filename)

                with open(path, "w") as f:
                    json.dump(data, f, indent=2)

                # 3d. Log, flash, redirect
                app.logger.info(f"Team '{team.name}' created by {email} ({user_id})")
                flash(f"✅ Team '{team.name}' saved!", "success")
                return redirect(url_for("home"))

            except Exception as e:
                app.logger.error(f"Unexpected error saving team '{name}': {e}", exc_info=True)
                flash(f"❌ Unexpected error saving team: {e}", "danger")
                return render_template("team_create.html")

        # GET: Show form
        return render_template("team_create.html")
    
    @app.route("/teams/manage")
    @login_required
    def manage_teams():
        teams_dir = os.path.join(PROJECT_ROOT, "data", "teams")
        user_email = current_user.id

        teams = []
        if os.path.exists(teams_dir):
            for fn in os.listdir(teams_dir):
                if fn.endswith(".json"):
                    path = os.path.join(teams_dir, fn)
                    try:
                        with open(path, "r") as f:
                            data = json.load(f)
                        # Only include teams created by this user
                        if data.get("created_by_email") == current_user.id:
                            # Extract just short_code from filename
                            short_code = fn.rsplit("_", 1)[0].replace(".json", "")
                            data["short_code"] = short_code
                            teams.append(data)
                    except Exception as e:
                        app.logger.error(f"Error loading team file {fn}: {e}", exc_info=True)
        return render_template("manage_teams.html", teams=teams)


    @app.route("/team/delete", methods=["POST"])
    @login_required
    def delete_team():
        short_code = request.form.get("short_code")
        if not short_code:
            flash("❌ No team specified for deletion.", "danger")
            return redirect(url_for("manage_teams"))

        # Build the path to the JSON file
        # Find matching file for this user
        teams_dir = os.path.join(PROJECT_ROOT, "data", "teams")
        filename = f"{short_code}_{current_user.id}.json"
        team_path = os.path.join(PROJECT_ROOT, "data", "teams", filename)

        # Check file exists
        if not os.path.exists(team_path):
            flash(f"❌ Team '{short_code}' not found.", "danger")
            return redirect(url_for("manage_teams"))

        # Verify ownership
        try:
            with open(team_path, "r") as f:
                data = json.load(f)
            owner = data.get("created_by_email")
            if owner != current_user.id:
                flash("❌ You don’t have permission to delete this team.", "danger")
                app.logger.warning(f"Unauthorized delete attempt by {current_user.id} on {short_code}")
                return redirect(url_for("manage_teams"))
        except Exception as e:
            app.logger.error(f"Error reading team file for deletion: {e}", exc_info=True)
            flash("❌ Could not verify team ownership.", "danger")
            return redirect(url_for("manage_teams"))

        # Perform deletion
        try:
            os.remove(team_path)
            app.logger.info(f"Team '{short_code}' deleted by {current_user.id}")
            flash(f"✅ Team '{short_code}' has been deleted.", "success")
        except Exception as e:
            app.logger.error(f"Error deleting team file: {e}", exc_info=True)
            flash("❌ Error deleting the team. Please try again.", "danger")

        return redirect(url_for("manage_teams"))


    @app.route("/team/<short_code>/edit", methods=["GET", "POST"])
    @login_required
    def edit_team(short_code):
        teams_dir = os.path.join(PROJECT_ROOT, "data", "teams")
        user_id = current_user.id
        filename = f"{short_code}_{current_user.id}.json"
        team_path = os.path.join(teams_dir, filename)

        app.logger.info(f"user_id: {user_id}", exc_info=True)
        app.logger.info(f"filename: {filename}", exc_info=True)


        # 1. Must exist
        if not os.path.exists(team_path):
            flash(f"❌ Team '{short_code}' not found.", "danger")
            return redirect(url_for("manage_teams"))

        # 2. Load & verify ownership
        try:
            with open(team_path, "r") as f:
                raw = json.load(f)
        except Exception as e:
            app.logger.error(f"Error reading team for edit: {e}", exc_info=True)
            flash("❌ Could not load team.", "danger")
            return redirect(url_for("manage_teams"))

        if raw.get("created_by_email") != current_user.id:
            flash("❌ You don’t have permission to edit this team.", "danger")
            app.logger.warning(f"Unauthorized edit attempt by {current_user.id} on {short_code}")
            return redirect(url_for("manage_teams"))

        # POST: process the edited form
        if request.method == "POST":
            # (Reuse your create logic, but overwrite the same file)
            name  = request.form["team_name"].strip()
            code  = request.form["short_code"].strip().upper()
            home  = request.form["home_ground"].strip()
            pitch = request.form["pitch_preference"]
            color = request.form["team_color"]
            
            # Gather players from form
            names = request.form.getlist("player_name")
            roles = request.form.getlist("player_role")
            bats  = request.form.getlist("batting_rating")
            bowls = request.form.getlist("bowling_rating")
            fields= request.form.getlist("fielding_rating")
            bhands= request.form.getlist("batting_hand")
            btypes= request.form.getlist("bowling_type")
            bhand2s = request.form.getlist("bowling_hand")

            players = []
            for i in range(len(names)):
                try:
                    p = Player(
                        name=names[i],
                        role=roles[i],
                        batting_rating=int(bats[i]),
                        bowling_rating=int(bowls[i]),
                        fielding_rating=int(fields[i]),
                        batting_hand=bhands[i],
                        bowling_type=btypes[i] or "",
                        bowling_hand=bhand2s[i] or ""
                    )
                    players.append(p)
                except Exception as e:
                    flash(f"❌ Error in player {i+1}: {e}", "danger")
                    app.logger.error(f"Team creation failed: {e}", exc_info=True)
                    return render_template("team_create.html", team=raw, edit=True)

            # Validate counts
            if not (15 <= len(players) <= 18):
                flash("❌ You must have between 15 and 18 players.", "danger")
                return render_template("team_create.html", team=raw, edit=True)
            if sum(1 for p in players if p.role == "Wicketkeeper") < 1:
                flash("❌ You need at least one Wicketkeeper.", "danger")
                return render_template("team_create.html", team=raw, edit=True)
            if sum(1 for p in players if p.role in ["Bowler","All-rounder"]) < 6:
                flash("❌ You need at least six Bowlers/All-rounders.", "danger")
                return render_template("team_create.html", team=raw, edit=True)

            # Determine captain & wicketkeeper from dropdowns
            captain     = request.form.get("captain")
            wicketkeeper= request.form.get("wicketkeeper")

            # Build new team dict
            new_team = Team(
                name=name,
                short_code=code,
                home_ground=home,
                pitch_preference=pitch,
                team_color=color,
                players=players,
                captain=captain,
                wicketkeeper=wicketkeeper
            ).to_dict()

            # Preserve creator metadata
            new_team["created_by_email"]   = raw["created_by_email"]
            new_team["created_by_user_id"] = raw["created_by_user_id"]

            # inside if request.method=="POST":, after reading form short_code:
            orig_code = short_code             # the URL‐param code
            new_code  = code                   # the form‐submitted code

            teams_dir = os.path.join(PROJECT_ROOT, "data", "teams")
            user_id = raw.get("created_by_user_id")
            new_path = os.path.join(teams_dir, f"{new_code}_{current_user.id}.json")
            old_path = os.path.join(teams_dir, f"{orig_code}_{current_user.id}.json")


            # 1️⃣ If the short code changed, rename the file on disk
            if orig_code != new_code:
                try:
                    os.rename(old_path, new_path)
                    app.logger.info(f"Renamed team file {orig_code}.json → {new_code}.json")
                except Exception as rename_err:
                    app.logger.error(f"Error renaming team file: {rename_err}", exc_info=True)
                    flash("❌ Could not rename team file on short code change.", "danger")
                    return redirect(url_for("manage_teams"))

            # Overwrite JSON file
            try:
                with open(new_path if orig_code != new_code else old_path, "w") as f:
                    json.dump(new_team, f, indent=2)
                app.logger.info(f"Team '{code}' updated by {current_user.id}")
                flash("✅ Team updated successfully!", "success")
            except Exception as e:
                app.logger.error(f"Error saving edited team: {e}", exc_info=True)
                flash("❌ Error saving team. Please try again.", "danger")

            return redirect(url_for("manage_teams"))

        # GET: render the same form, passing raw JSON and an edit flag
        return render_template("team_create.html", team=raw, edit=True)
    
    
    @app.route("/match/setup", methods=["GET", "POST"])
    @login_required
    def match_setup():
        teams = load_user_teams(current_user.id)

        if request.method == "POST":
            clean_old_archives(PROD_MAX_AGE)
            cleanup_temp_scorecard_images()

            data = request.get_json()

            # Step 1: Extract base team short codes
            home_code = data["team_home"].split("_")[0]
            away_code = data["team_away"].split("_")[0]

            # Step 2: Load full team data from disk
            def load_team(full_filename):
                path = os.path.join(PROJECT_ROOT, "data", "teams", full_filename + ".json")
                with open(path) as f:
                    return json.load(f)

            full_home = load_team(data["team_home"])
            full_away = load_team(data["team_away"])

            # Step 3: Generic function to enrich player lists (XI and substitutes)
            def enrich_player_list(players_to_enrich, full_team_data):
                enriched = []
                for player_info in players_to_enrich:
                    # Find the full player data from the team file
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
                "timestamp": ts,
                "rain_probability": data.get("rain_probability", 0.0)
            })

            with open(path, "w") as f:
                json.dump(data, f, indent=2)

            app.logger.info(f"[MatchSetup] Saved {fname} for {user}")
            return jsonify(match_id=match_id), 200

        return render_template("match_setup.html", teams=teams)

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
            flash("❌ Match not found or access denied.", "danger")
            return redirect(url_for("home"))

        # Render the detail page, passing the loaded JSON
        return render_template("match_detail.html", match=match_data)
    
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
        
        # ───── NEW: update the in-memory Match, if created
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
                        f"<br>🧢 <strong>Striker:</strong> {batting_team[0]['name']}<br>" \
                        f"🎯 <strong>Non-striker:</strong> {batting_team[1]['name']}"
        
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
            # 🟢 START: CRITICAL FIX - UPDATE IN-MEMORY INSTANCE
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
            # 🔴 END: CRITICAL FIX
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
            result = outcome.get("result", "Match ended")
            # After
            app.logger.info(
    f"Result in main.py {outcome.get('result', 'Match ended')}"
)

            return jsonify({
                "innings_end":     True,                              # ← flag it as an innings end
                "innings_number":  2,                                 # ← second innings
                "match_over":      True,
                "commentary":      outcome.get("commentary", "<b>Match Over!</b>"),
                "scorecard_data":  outcome.get("scorecard_data"),     # ← your detailed card
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
            print(f"🐛 DEBUG: Received commentary request for match {match_id}")
            
            data = request.get_json()
            commentary_html = data.get('commentary_html', '')
            
            print(f"🐛 DEBUG: Commentary HTML length: {len(commentary_html)}")
            print(f"🐛 DEBUG: Contains 'End of over': {'End of over' in commentary_html}")
            print(f"🐛 DEBUG: First 300 chars: {commentary_html[:300]}")
            
            if not commentary_html:
                return jsonify({"error": "No commentary provided"}), 400
            
            # Store commentary for the match instance
            if match_id in MATCH_INSTANCES:
                match_instance = MATCH_INSTANCES[match_id]
                
                # Convert HTML to clean text list for archiving
                frontend_commentary = html_to_commentary_list(commentary_html)
                print(f"🐛 DEBUG: Converted to {len(frontend_commentary)} commentary items")
                
                # Replace the backend commentary with frontend commentary
                match_instance.frontend_commentary_captured = frontend_commentary
                
                # DON'T trigger archive creation here - it already happened
                # Just store the commentary for next time
                print(f"🐛 DEBUG: Stored frontend commentary for future use")
                
                app.logger.info(f"[Commentary] Captured {len(frontend_commentary)} items for match {match_id}")
                return jsonify({"message": "Commentary captured successfully"}), 200
            else:
                print(f"🐛 DEBUG: Match instance {match_id} not found in MATCH_INSTANCES")
                return jsonify({"error": "Match instance not found"}), 404
                
        except Exception as e:
            print(f"🐛 DEBUG: Error in save_commentary: {e}")
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

            # ─── A) Extract HTML content from request ───────────────────────────
            payload = request.get_json() or {}
            html_content = payload.get("html_content")
            if not html_content:
                app.logger.error("[DownloadArchive] No HTML content provided in request payload")
                return jsonify({"error": "HTML content is required"}), 400

            app.logger.debug(f"[DownloadArchive] Received HTML content length: {len(html_content):,} characters")
            if len(html_content) < 1000:
                app.logger.warning("[DownloadArchive] HTML content seems unusually short (< 1,000 chars)")

            # ─── B) Load match metadata ─────────────────────────────────────────
            match_meta = load_match_metadata(match_id)
            if not match_meta:
                app.logger.error(f"[DownloadArchive] Match metadata not found for match_id='{match_id}'")
                return jsonify({"error": "Match not found"}), 404

            # Verify ownership
            created_by = match_meta.get("created_by")
            if created_by != current_user.id:
                app.logger.warning(f"[DownloadArchive] Unauthorized access: user='{current_user.id}' attempted to archive match='{match_id}'")
                return jsonify({"error": "Unauthorized"}), 403

            # ─── C) Retrieve or rehydrate match instance ─────────────────────────
            match_instance = MATCH_INSTANCES.get(match_id)
            if not match_instance:
                app.logger.info(f"[DownloadArchive] Match instance not in memory; recreating minimal Match for '{match_id}'")
                from engine.match import Match
                match_instance = Match(match_meta)

            # ─── D) Locate original JSON file on disk ───────────────────────────
            from match_archiver import find_original_json_file
            original_json_path = find_original_json_file(match_id)
            if not original_json_path:
                app.logger.error(f"[DownloadArchive] Original JSON file not found for match_id='{match_id}'")
                return jsonify({"error": "Original match file not found"}), 404

            app.logger.debug(f"[DownloadArchive] Found original JSON at '{original_json_path}'")

            # ─── E) Extract commentary log ───────────────────────────────────────
            if getattr(match_instance, "frontend_commentary_captured", None):
                commentary_log = match_instance.frontend_commentary_captured
                app.logger.info(f"[DownloadArchive] Using frontend commentary (items={len(commentary_log)})")
            elif getattr(match_instance, "commentary", None):
                commentary_log = match_instance.commentary
                app.logger.info(f"[DownloadArchive] Using backend commentary (items={len(commentary_log)})")
            else:
                commentary_log = ["Match completed - commentary preserved in HTML"]
                app.logger.warning("[DownloadArchive] No commentary found; using fallback single-line log")

            # ─── F) Instantiate MatchArchiver and create ZIP ────────────────────
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

            # ─── G) Compute and confirm ZIP path on disk ─────────────────────────
            zip_path = os.path.join(PROJECT_ROOT, "data", zip_name)
            if not os.path.isfile(zip_path):
                app.logger.error(f"[DownloadArchive] ZIP file missing after creation: '{zip_path}'")
                return jsonify({"error": "Archive ZIP file not found"}), 500

            zip_size = os.path.getsize(zip_path)
            app.logger.info(f"[DownloadArchive] ZIP successfully created: '{zip_name}' ({zip_size:,} bytes)")

            # ─── H) Stream the ZIP file back to the browser ─────────────────────
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
    def delete_archive(archive_name):
        """
        DELETE endpoint to remove an archive file.
        Production considerations:
        - Prevent path traversal by normalizing and checking for “..” segments.
        - Use a configured ARCHIVES_FOLDER to locate files.
        - Log each attempt and handle exceptions cleanly.
        - Return appropriate status codes and JSON messages.
        """

        # 1. Normalize and validate the incoming path to prevent traversal
        safe_name = os.path.normpath(archive_name)
        if os.path.isabs(safe_name) or '..' in safe_name.split(os.path.sep):
            app.logger.warning(f"Invalid delete path attempt: {archive_name}")
            return jsonify({'error': 'Invalid file path'}), 400

        # 2. Build the absolute path under ARCHIVES_FOLDER
        archive_folder = app.config.get('ARCHIVES_FOLDER')
        if not archive_folder:
            app.logger.error("ARCHIVES_FOLDER is not configured")
            return jsonify({'error': 'Server misconfiguration'}), 500

        file_path = os.path.join(archive_folder, safe_name)

        # 3. Check existence
        if not os.path.isfile(file_path):
            app.logger.info(f"Delete requested for non-existent file: {file_path}")
            return jsonify({'error': 'File not found'}), 404

        # 4. Attempt removal
        try:
            os.remove(file_path)
            app.logger.info(f"Deleted archive: {file_path}")
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
        
    @app.route("/api/teams/list", methods=["GET"])
    def list_all_teams():
        try:
            teams_dir = os.path.join(PROJECT_ROOT, "data", "teams")
            if not os.path.isdir(teams_dir):
                return jsonify({"teams": []}), 200

            files = [f for f in os.listdir(teams_dir) if f.endswith(".json")]
            app.logger.info(f"[TeamsList] {len(files)} team files found")
            return jsonify({"teams": files}), 200

        except Exception as e:
            app.logger.error(f"[TeamsList] Error: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
        
    @app.route("/api/teams/by-user", methods=["GET"])
    def list_teams_by_user():
        try:
            teams_dir = os.path.join(PROJECT_ROOT, "data", "teams")
            if not os.path.isdir(teams_dir):
                return jsonify({"users": {}}), 200

            user_files = {}
            for f in os.listdir(teams_dir):
                if f.endswith(".json") and "_" in f:
                    parts = f.rsplit("_", 1)
                    if len(parts) == 2:
                        user_email = parts[1].replace(".json", "")
                        user_files.setdefault(user_email, []).append(f)

            app.logger.info(f"[TeamsByUser] Grouped files by {len(user_files)} users")
            return jsonify({"users": user_files}), 200

        except Exception as e:
            app.logger.error(f"[TeamsByUser] Error: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @app.route("/api/teams/download/<filename>", methods=["GET"])
    def download_team_file(filename):
        try:
            # Basic security validation
            if not filename.endswith(".json") or "/" in filename or "\\" in filename:
                app.logger.warning(f"[DownloadTeam] Invalid filename requested: {filename}")
                return jsonify({"error": "Invalid filename"}), 400

            teams_dir = os.path.join(PROJECT_ROOT, "data", "teams")
            full_path = os.path.join(teams_dir, filename)

            if not os.path.isfile(full_path):
                app.logger.warning(f"[DownloadTeam] File not found: {filename}")
                return jsonify({"error": "File not found"}), 404

            app.logger.info(f"[DownloadTeam] Sending file: {filename}")
            return send_file(full_path, mimetype="application/json", as_attachment=True)

        except Exception as e:
            app.logger.error(f"[DownloadTeam] Error downloading {filename}: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
        
    
    @app.route('/download-encryption-key')
    def download_encryption_key():
        """
        Download the encryption key file.
        Internal debugging route - no authentication required.
        """
        encryption_key_file = 'auth/encryption.key'
        
        try:
            # Check if file exists
            if not os.path.exists(encryption_key_file):
                app.logger.warning(f"Encryption key file not found: {encryption_key_file}")
                return jsonify({"error": "Encryption key file not found"}), 404
            
            app.logger.info("Encryption key downloaded (debug route)")
            return send_file(encryption_key_file, as_attachment=True, download_name='encryption.key')
            
        except Exception as e:
            app.logger.error(f"Error downloading encryption key: {e}", exc_info=True)
            return jsonify({"error": f"Failed to download encryption key: {str(e)}"}), 500

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

    return app

# ────── Run Server ──────
if __name__ == "__main__":
    import os
    import secrets
    from flask import request

    app = create_app()

    # Set archive folder path
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    app.config["ARCHIVES_FOLDER"] = os.path.join(BASE_DIR, "data")

    # Ensure SECRET_KEY is present
    if not app.config.get("SECRET_KEY"):
        generated_key = secrets.token_hex(32)
        print("[WARNING] No SECRET_KEY set — generating random one (not persistent)")
        app.config["SECRET_KEY"] = generated_key

    # Secure cookie config
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = False  # Explicitly set to False for local dev
    app.config["PERMANENT_SESSION_LIFETIME"] = 604800  # 7 days in seconds

    # 🔥 FINAL: Force Flask to bind on all interfaces
    print("🔁 Launching Flask server on 0.0.0.0:7860")
    app.run(host="0.0.0.0", port=7860, debug=False)
