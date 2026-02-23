# -*- coding: utf-8 -*-
"""SimCricketX Flask Application"""

from __future__ import annotations

# CRITICAL: Fix Windows console encoding BEFORE any other imports
import sys
import io
import os

# Force UTF-8 encoding for all I/O operations on Windows
if sys.platform == "win32":
    # Ensure stdout and stderr use UTF-8
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer,
            encoding='utf-8',
            errors='replace',
            line_buffering=True,
            write_through=True
        )
    if hasattr(sys.stderr, 'buffer'):
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer,
            encoding='utf-8',
            errors='replace',
            line_buffering=True,
            write_through=True
        )
    
    # Set environment variables for UTF-8
    os.environ['PYTHONIOENCODING'] = 'utf-8'

# Now import everything else
import json
import re
import logging
import yaml
import uuid
import sqlite3
import hashlib
import secrets
import ipaddress
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
    log_admin_action,
    validate_password_policy
)
from engine.team import Team, save_team, PITCH_PREFERENCES
from engine.player import Player, PLAYER_ROLES, BATTING_HANDS, BOWLING_TYPES, BOWLING_HANDS
import random
import shutil
import time
import threading
import traceback
from pathlib import Path

from werkzeug.utils import secure_filename
from engine.stats_aggregator import StatsAggregator 
from engine.stats_service import StatsService
import glob
import pandas as pd 
from tabulate import tabulate
from flask import Response
from sqlalchemy.orm import joinedload, aliased
from routes.stats_routes import register_stats_routes
from routes.tournament_routes import register_tournament_routes
from routes.auth_routes import register_auth_routes
from routes.team_routes import register_team_routes
from routes.core_routes import register_core_routes
from routes.match_routes import register_match_routes
from routes.admin_routes import register_admin_routes

# SocketIO optional dependency — app works normally via HTTP if not installed
_SOCKETIO_AVAILABLE = False
try:
    from flask_socketio import SocketIO
    socketio = SocketIO()
    _SOCKETIO_AVAILABLE = True
except ImportError:
    socketio = None

# Add this import for system monitoring
try:
    import psutil
except ImportError:
    psutil = None

from database import db
from database.models import User as DBUser, Team as DBTeam, Player as DBPlayer, Tournament, TournamentTeam, TournamentFixture
from database.models import Match as DBMatch, MatchScorecard, TournamentPlayerStatsCache, MatchPartnership, AdminAuditLog  # Distinct from engine.match.Match
from database.models import FailedLoginAttempt, BlockedIP, ActiveSession, LoginHistory, IPWhitelistEntry
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
IP_WHITELIST_MODE = False

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
        cutoff_time = current_time - (24 * 3600)  # 24 hours ago

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
    trust_proxy_headers = bool(config.get("security", {}).get("trust_proxy_headers", False))

    ADMIN_CONFIG_ALLOWLIST = {
        "app": {"maintenance_mode": bool},
        "rate_limits": {
            "max_requests": int,
            "window_seconds": int,
            "admin_multiplier": int,
            "login_limit": str,
        },
        "bot_defense": {
            "enabled": bool,
            "base_difficulty": int,
            "elevated_difficulty": int,
            "high_difficulty": int,
            "elevated_threshold": int,
            "high_threshold": int,
            "window_minutes": int,
            "ttl_seconds": int,
            "max_counter": int,
            "max_iterations": int,
            "trusted_ip_prefixes": str,
        },
    }

    bot_defense_settings = {
        "enabled": bool(config.get("bot_defense", {}).get("enabled", True)),
        "base_difficulty": int(config.get("bot_defense", {}).get("base_difficulty", 3)),
        "elevated_difficulty": int(config.get("bot_defense", {}).get("elevated_difficulty", 4)),
        "high_difficulty": int(config.get("bot_defense", {}).get("high_difficulty", 5)),
        "elevated_threshold": int(config.get("bot_defense", {}).get("elevated_threshold", 5)),
        "high_threshold": int(config.get("bot_defense", {}).get("high_threshold", 20)),
        "window_minutes": int(config.get("bot_defense", {}).get("window_minutes", 15)),
        "ttl_seconds": int(config.get("bot_defense", {}).get("ttl_seconds", 180)),
        "max_counter": int(config.get("bot_defense", {}).get("max_counter", 10_000_000)),
        "max_iterations": int(config.get("bot_defense", {}).get("max_iterations", 1_500_000)),
        "trusted_ip_prefixes": str(config.get("bot_defense", {}).get("trusted_ip_prefixes", "")).strip(),
    }

    def get_client_ip() -> str:
        """Resolve client IP safely; only trust proxy headers when explicitly enabled."""
        if trust_proxy_headers:
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                first = forwarded.split(",")[0].strip()
                if first:
                    return first
        return (request.remote_addr or "").strip()

    def parse_ip(value: str):
        try:
            return ipaddress.ip_address((value or "").strip())
        except ValueError:
            return None

    def is_path_within_base(base_dir: str, candidate_path: str) -> bool:
        """True only when candidate resolves under base_dir (safe on Windows/symlinks)."""
        try:
            base_path = Path(base_dir).resolve(strict=False)
            target_path = Path(candidate_path).resolve(strict=False)
            target_path.relative_to(base_path)
            return True
        except Exception:
            return False

    def is_ip_blocked(client_ip: str) -> bool:
        """Check whether a client IP is blocked by exact match or CIDR entry."""
        ip_obj = parse_ip(client_ip)
        if not ip_obj:
            return False
        blocked_entries = BlockedIP.query.all()
        for entry in blocked_entries:
            raw = (entry.ip_address or "").strip()
            if not raw:
                continue
            if "/" in raw:
                try:
                    if ip_obj in ipaddress.ip_network(raw, strict=False):
                        return True
                except ValueError:
                    continue
            elif raw == str(ip_obj):
                return True
        return False

    def coerce_config_value(raw_value: str, expected_type):
        if expected_type is bool:
            v = (raw_value or "").strip().lower()
            if v in {"true", "1", "yes", "on"}:
                return True
            if v in {"false", "0", "no", "off"}:
                return False
            raise ValueError("Boolean values must be one of: true/false, 1/0, yes/no, on/off")
        if expected_type is int:
            return int((raw_value or "").strip())
        if expected_type is float:
            return float((raw_value or "").strip())
        return (raw_value or "").strip()

    def _is_trusted_bot_defense_ip(ip_addr: str) -> bool:
        raw_rules = bot_defense_settings.get("trusted_ip_prefixes", "") or ""
        rules = [r.strip() for r in raw_rules.split(",") if r.strip()]
        if not rules:
            return False
        ip_obj = parse_ip(ip_addr or "")
        if not ip_obj:
            return False
        ip_text = str(ip_obj)
        for rule in rules:
            try:
                if "/" in rule:
                    if ip_obj in ipaddress.ip_network(rule, strict=False):
                        return True
                elif rule.endswith("."):
                    if ip_text.startswith(rule):
                        return True
                elif ip_text == rule:
                    return True
            except Exception:
                continue
        return False

    # --- Modern Bot Defense (Proof-of-Work challenge) ---
    _auth_pow_challenges = {}  # challenge_id -> challenge data
    _auth_pow_lock = threading.Lock()

    def _cleanup_auth_pow_challenges(now_ts=None):
        now_ts = now_ts or time.time()
        stale_ids = []
        for cid, ch in _auth_pow_challenges.items():
            if ch.get("used") or ch.get("expires_at", 0) < now_ts:
                stale_ids.append(cid)
        for cid in stale_ids:
            _auth_pow_challenges.pop(cid, None)

    def _auth_pow_difficulty_for_ip(ip_addr: str) -> int:
        """Adaptive challenge hardness based on recent failed logins from same IP."""
        if _is_trusted_bot_defense_ip(ip_addr):
            return 0
        try:
            cutoff = datetime.utcnow() - timedelta(minutes=max(1, int(bot_defense_settings.get("window_minutes", 15))))
            recent_failures = db.session.query(FailedLoginAttempt).filter(
                FailedLoginAttempt.ip_address == (ip_addr or ""),
                FailedLoginAttempt.timestamp >= cutoff
            ).count()
        except Exception:
            recent_failures = 0

        high_threshold = max(1, int(bot_defense_settings.get("high_threshold", 20)))
        elevated_threshold = max(1, int(bot_defense_settings.get("elevated_threshold", 5)))
        base_difficulty = max(1, int(bot_defense_settings.get("base_difficulty", 3)))
        elevated_difficulty = max(1, int(bot_defense_settings.get("elevated_difficulty", 4)))
        high_difficulty = max(1, int(bot_defense_settings.get("high_difficulty", 5)))

        if recent_failures >= high_threshold:
            return high_difficulty
        if recent_failures >= elevated_threshold:
            return elevated_difficulty
        return base_difficulty

    def issue_auth_pow_challenge():
        if not bool(bot_defense_settings.get("enabled", True)):
            return {
                "enabled": False,
                "algorithm": "none",
                "ttl_seconds": 0,
                "max_iterations": 0,
            }
        now_ts = time.time()
        with _auth_pow_lock:
            _cleanup_auth_pow_challenges(now_ts=now_ts)
            challenge_id = secrets.token_urlsafe(18)
            nonce = secrets.token_hex(16)
            ip_addr = get_client_ip()
            difficulty = _auth_pow_difficulty_for_ip(ip_addr)
            ttl_seconds = max(30, int(bot_defense_settings.get("ttl_seconds", 180)))
            _auth_pow_challenges[challenge_id] = {
                "nonce": nonce,
                "difficulty": difficulty,
                "ip": ip_addr,
                "created_at": now_ts,
                "expires_at": now_ts + ttl_seconds,
                "used": False,
            }

        return {
            "challenge_id": challenge_id,
            "nonce": nonce,
            "difficulty": difficulty,
            "algorithm": "sha256-prefix-zeros",
            "ttl_seconds": ttl_seconds,
            "max_iterations": max(10_000, int(bot_defense_settings.get("max_iterations", 1_500_000))),
            "enabled": True,
        }

    def verify_auth_pow_solution(challenge_id: str, counter_raw: str, digest_raw: str) -> tuple[bool, str]:
        if app.config.get("TESTING"):
            return True, ""
        if not bool(bot_defense_settings.get("enabled", True)):
            return True, ""
        if _is_trusted_bot_defense_ip(get_client_ip()):
            return True, ""
        if not challenge_id:
            return False, "Missing challenge"
        try:
            counter = int((counter_raw or "").strip())
        except Exception:
            return False, "Invalid challenge counter"
        if counter < 0 or counter > max(10_000, int(bot_defense_settings.get("max_counter", 10_000_000))):
            return False, "Invalid challenge counter"

        digest = (digest_raw or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            return False, "Invalid challenge digest"

        now_ts = time.time()
        with _auth_pow_lock:
            _cleanup_auth_pow_challenges(now_ts=now_ts)
            challenge = _auth_pow_challenges.get(challenge_id)
            if not challenge:
                return False, "Challenge expired"
            if challenge.get("used"):
                return False, "Challenge already used"
            if challenge.get("expires_at", 0) < now_ts:
                _auth_pow_challenges.pop(challenge_id, None)
                return False, "Challenge expired"

            # Bind challenge to source IP to make replaying across clients harder.
            if challenge.get("ip") != get_client_ip():
                return False, "Challenge source mismatch"

            nonce = challenge.get("nonce", "")
            difficulty = int(challenge.get("difficulty", 3))
            expected = hashlib.sha256(f"{nonce}:{counter}".encode("utf-8")).hexdigest()
            if expected != digest:
                return False, "Challenge verification failed"
            if not expected.startswith("0" * difficulty):
                return False, "Challenge verification failed"

            challenge["used"] = True
            _auth_pow_challenges.pop(challenge_id, None)
            return True, ""

    @app.context_processor
    def inject_route_helpers():
        def has_endpoint(endpoint_name):
            return endpoint_name in app.view_functions
        return {"has_endpoint": has_endpoint, "maintenance_mode": MAINTENANCE_MODE}

    @app.context_processor
    def inject_user_stats():
        """Inject user statistics for profile dropdown"""
        if current_user.is_authenticated:
            try:
                user_stats = {
                    'teams_count': db.session.query(DBTeam).filter_by(user_id=current_user.id).count(),
                    'matches_count': db.session.query(DBMatch).filter_by(user_id=current_user.id).count(),
                    'tournaments_count': db.session.query(Tournament).filter_by(user_id=current_user.id).count()
                }
                return {'user_stats': user_stats}
            except Exception:
                return {'user_stats': {'teams_count': 0, 'matches_count': 0, 'tournaments_count': 0}}
        return {}

    def _get_app_version():
        try:
            with open(os.path.join(app.root_path, "version.txt"), encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return "0.0.0"

    @app.context_processor
    def inject_app_version():
        return {"app_version": _get_app_version()}

    # --- Admin timezone filter ---
    # Wraps a UTC datetime as a <time class="utc-time"> element so client-side JS
    # can convert it to the admin's preferred timezone (IST / PST / browser auto).
    from markupsafe import Markup

    def _admin_localtime(dt, seconds=False):
        """Jinja2 filter: render a UTC datetime as a timezone-convertible <time> element."""
        if not dt:
            return '-'
        iso = dt.strftime('%Y-%m-%dT%H:%M:%S') + 'Z'
        fmt = '%Y-%m-%d %H:%M:%S' if seconds else '%Y-%m-%d %H:%M'
        fallback = dt.strftime(fmt) + ' UTC'
        sec_attr = ' data-seconds="1"' if seconds else ''
        return Markup(f'<time class="utc-time"{sec_attr} datetime="{iso}">{fallback}</time>')

    app.jinja_env.filters['localtime'] = _admin_localtime

    @app.before_request
    def check_maintenance_mode():
        """Block non-admin users when maintenance mode is active."""
        if not MAINTENANCE_MODE:
            return None
        # Always allow static files
        if request.path.startswith('/static'):
            return None
        # Allow login/logout so admin can authenticate
        if request.endpoint in ('login', 'logout', 'static', 'auth_challenge', 'admin_stop_impersonation'):
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
            client_ip = get_client_ip()
            if is_ip_blocked(client_ip):
                return jsonify({"error": "Access denied"}), 403
        except Exception:
            pass
        return None

    @app.before_request
    def check_ip_whitelist():
        """When whitelist mode is on, only allow IPs on the whitelist (admins always pass)."""
        global IP_WHITELIST_MODE
        if not IP_WHITELIST_MODE:
            return None
        if request.path.startswith('/static'):
            return None
        # Admins bypass whitelist
        if current_user.is_authenticated and getattr(current_user, 'is_admin', False):
            return None
        # Login/logout/register always allowed so users can authenticate
        if request.endpoint in ('login', 'logout', 'register', 'static'):
            return None
        try:
            client_ip = get_client_ip()
            allowed = IPWhitelistEntry.query.filter_by(ip_address=client_ip).first()
            if not allowed:
                return render_template('maintenance.html',
                                       reason='Access restricted to whitelisted IPs only.'), 403
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
        if request.endpoint in ('force_change_password', 'logout', 'static', 'admin_stop_impersonation'):
            return None
        if getattr(current_user, 'force_password_reset', False):
            session['force_password_reset'] = True
            return redirect(url_for('force_change_password'))
        return None


    @app.before_request
    def check_display_name():
        """Redirect users who don't have a display name set."""
        try:
            if not current_user.is_authenticated:
                return None

            if request.path.startswith('/static'):
                return None
            if request.path == '/favicon.ico':
                return None

            # Allow auth and setup routes to avoid redirect loops.
            if request.endpoint in (
                'set_display_name',
                'logout',
                'login',
                'register',
                'static',
                'force_change_password',
                'admin_stop_impersonation',
            ):
                return None

            # Re-fetch from DB to avoid stale/detached user objects in request context.
            user_row = db.session.get(DBUser, current_user.id)
            raw_display_name = (user_row.display_name if user_row else None)
            normalized = (raw_display_name or "").strip()

            # Treat textual null sentinels as missing as well.
            missing_display_name = (not normalized) or (normalized.lower() in {"none", "null"})
            if missing_display_name:
                app.logger.info(
                    f"[DisplayName Check] Redirecting {current_user.id} to set display name"
                )
                return redirect(url_for('set_display_name'))

            return None
        except Exception as e:
            app.logger.error(f"[DisplayName Check] Guard error: {e}", exc_info=True)
            # Fail closed for authenticated users if guard errors unexpectedly.
            if current_user.is_authenticated and request.endpoint != 'set_display_name':
                return redirect(url_for('set_display_name'))
            return None



    @app.before_request
    def update_session_activity():
        """Update last_active and enforce that authenticated sessions are revocable."""
        if not current_user.is_authenticated:
            return None
        if request.endpoint in ('login', 'logout', 'static'):
            return None

        token = session.get('session_token')
        if not token:
            app.logger.warning(f"[Auth] Missing session token for authenticated user {current_user.id}")
            logout_user()
            session.clear()
            if request.path.startswith('/api/') or request.accept_mimetypes.best == 'application/json':
                return jsonify({"error": "Session expired. Please log in again."}), 401
            return redirect(url_for('login'))

        try:
            active = ActiveSession.query.filter_by(
                session_token=token,
                user_id=current_user.id
            ).first()
            if not active:
                app.logger.warning(f"[Auth] Revoked or invalid session token for {current_user.id}")
                logout_user()
                session.clear()
                if request.path.startswith('/api/') or request.accept_mimetypes.best == 'application/json':
                    return jsonify({"error": "Session expired. Please log in again."}), 401
                return redirect(url_for('login'))

            active.last_active = datetime.utcnow()
            db.session.commit()
        except Exception:
            db.session.rollback()
        return None

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
    env_name = str(os.getenv("ENV", "dev")).lower()
    secure_cookie_default = str(os.getenv("SESSION_COOKIE_SECURE", "")).strip().lower()
    if secure_cookie_default in {"1", "true", "yes", "on"}:
        app.config["SESSION_COOKIE_SECURE"] = True
    elif secure_cookie_default in {"0", "false", "no", "off"}:
        app.config["SESSION_COOKIE_SECURE"] = False
    else:
        app.config["SESSION_COOKIE_SECURE"] = (env_name == "prod")
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

    # Console handler for terminal visibility.
    # Use stdout because some launchers/IDEs suppress stderr by default.
    console_stream = sys.stdout if getattr(sys, "stdout", None) else sys.stderr
    console_handler = logging.StreamHandler(console_stream)
    console_handler.setLevel(logging.DEBUG)

    # Formatter for both
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # Setup root logger explicitly for deterministic behavior.
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # App logger
    app_logger = logging.getLogger("SimCricketX")
    app_logger.setLevel(logging.DEBUG)  # You can change to INFO for production
    app_logger.handlers.clear()
    app_logger.addHandler(file_handler)
    app_logger.addHandler(console_handler)
    app_logger.propagate = False
    app.logger = app_logger

    # Werkzeug request logger
    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.setLevel(logging.INFO)
    werkzeug_logger.handlers.clear()
    werkzeug_logger.addHandler(file_handler)
    werkzeug_logger.addHandler(console_handler)
    werkzeug_logger.propagate = False

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

    def _list_backup_files(prefix_filter=None):
        files = []
        if not os.path.isdir(BACKUP_DIR):
            return files
        for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if not fn.endswith('.db'):
                continue
            if prefix_filter and not fn.startswith(prefix_filter):
                continue
            if prefix_filter is None and fn.startswith('pre_restore_'):
                continue
            path = os.path.join(BACKUP_DIR, fn)
            files.append({
                'name': fn,
                'size_mb': round(os.path.getsize(path) / (1024 * 1024), 2),
                'date': datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M:%S'),
                'age_days': round((time.time() - os.path.getmtime(path)) / 86400, 1)
            })
        return files

    def _persist_maintenance_mode(enabled: bool):
        global MAINTENANCE_MODE
        with MAINTENANCE_MODE_LOCK:
            MAINTENANCE_MODE = bool(enabled)
            try:
                config_path = os.getenv("SIMCRICKETX_CONFIG_PATH") or os.path.join(basedir, "config", "config.yaml")
                cfg = {}
                if os.path.exists(config_path):
                    with open(config_path, "r") as f:
                        cfg = yaml.safe_load(f) or {}
                cfg.setdefault("app", {})["maintenance_mode"] = MAINTENANCE_MODE
                with open(config_path, "w") as f:
                    yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
            except Exception as e:
                app.logger.error(f"[Admin] Failed to persist maintenance mode to config: {e}")

    def _verify_sqlite_integrity(db_file: str):
        conn = None
        try:
            conn = sqlite3.connect(db_file)
            row = conn.execute("PRAGMA integrity_check;").fetchone()
            status = (row[0] if row and row[0] else "").strip().lower()
            return status == "ok", (row[0] if row and row[0] else "integrity check failed")
        except Exception as e:
            return False, str(e)
        finally:
            if conn:
                conn.close()

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

    # --- Admin Routes (Phase 5 extraction) ---
    register_admin_routes(
        app,
        login_required=login_required,
        admin_required=admin_required,
        db=db,
        basedir=basedir,
        config=config,
        load_config=load_config,
        get_client_ip=get_client_ip,
        parse_ip=parse_ip,
        is_path_within_base=is_path_within_base,
        coerce_config_value=coerce_config_value,
        ADMIN_CONFIG_ALLOWLIST=ADMIN_CONFIG_ALLOWLIST,
        bot_defense_settings=bot_defense_settings,
        _check_backup_rate_limit=_check_backup_rate_limit,
        _run_scheduled_backup=_run_scheduled_backup,
        _list_backup_files=_list_backup_files,
        _verify_sqlite_integrity=_verify_sqlite_integrity,
        _backup_scheduler_started=_backup_scheduler_started,
        _persist_maintenance_mode=_persist_maintenance_mode,
        get_maintenance_mode=lambda: MAINTENANCE_MODE,
        psutil=psutil,
        log_admin_action=log_admin_action,
        update_user_email=update_user_email,
        update_user_password=update_user_password,
        delete_user=delete_user,
        register_user=register_user,
        BLOCKED_IP_MODEL=BlockedIP,
        FAILED_LOGIN_MODEL=FailedLoginAttempt,
        ACTIVE_SESSION_MODEL=ActiveSession,
        AUDIT_MODEL=AdminAuditLog,
        LOGIN_HISTORY_MODEL=LoginHistory,
        IP_WHITELIST_MODEL=IPWhitelistEntry,
        DBUser=DBUser,
        DBTeam=DBTeam,
        DBPlayer=DBPlayer,
        DBMatch=DBMatch,
        Tournament=Tournament,
        MatchScorecard=MatchScorecard,
        TournamentTeam=TournamentTeam,
        TournamentFixture=TournamentFixture,
        TournamentPlayerStatsCache=TournamentPlayerStatsCache,
        MatchPartnership=MatchPartnership,
        PROJECT_ROOT=PROJECT_ROOT,
        MATCH_INSTANCES=MATCH_INSTANCES,
        MATCH_INSTANCES_LOCK=MATCH_INSTANCES_LOCK,
        text=text,
        get_whitelist_mode=lambda: IP_WHITELIST_MODE,
    )

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
    @login_required
    @admin_required
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
    @login_required
    @admin_required
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

    # --- Core Routes (Phase 4 extraction) ---
    register_core_routes(
        app,
        db=db,
        func=func,
        ActiveSession=ActiveSession,
        _get_app_version=_get_app_version,
        get_visit_counter=get_visit_counter,
        get_matches_simulated=get_matches_simulated,
        increment_visit_counter=increment_visit_counter,
        basedir=basedir,
    )

    # --- Auth Routes (Phase 3 extraction) ---
    register_auth_routes(
        app,
        limiter=limiter,
        db=db,
        register_user=register_user,
        verify_user=verify_user,
        delete_user=delete_user,
        validate_password_policy=validate_password_policy,
        verify_auth_pow_solution=verify_auth_pow_solution,
        issue_auth_pow_challenge=issue_auth_pow_challenge,
        DBUser=DBUser,
        FailedLoginAttempt=FailedLoginAttempt,
        ActiveSession=ActiveSession,
        LoginHistory=LoginHistory,
        get_client_ip=get_client_ip,
    )

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


    # --- Team Routes (Phase 3 extraction) ---
    register_team_routes(
        app,
        db=db,
        Player=Player,
        DBTeam=DBTeam,
        DBPlayer=DBPlayer,
    )

    # --- Match & Archive Routes (Phase 4 extraction) ---
    register_match_routes(
        app,
        limiter=limiter,
        db=db,
        Match=Match,
        Tournament=Tournament,
        TournamentFixture=TournamentFixture,
        DBTeam=DBTeam,
        DBMatch=DBMatch,
        MatchScorecard=MatchScorecard,
        MatchPartnership=MatchPartnership,
        load_user_teams=load_user_teams,
        clean_old_archives=clean_old_archives,
        PROD_MAX_AGE=PROD_MAX_AGE,
        cleanup_temp_scorecard_images=cleanup_temp_scorecard_images,
        PROJECT_ROOT=PROJECT_ROOT,
        MATCH_INSTANCES=MATCH_INSTANCES,
        MATCH_INSTANCES_LOCK=MATCH_INSTANCES_LOCK,
        _get_match_file_lock=_get_match_file_lock,
        _load_match_file_for_user=_load_match_file_for_user,
        load_config=load_config,
        increment_matches_simulated=increment_matches_simulated,
        rate_limit=rate_limit,
        _handle_tournament_match_completion=_handle_tournament_match_completion,
        _persist_non_tournament_match_completion=_persist_non_tournament_match_completion,
        load_match_metadata=load_match_metadata,
        _is_valid_match_id=_is_valid_match_id,
        reverse_player_aggregates=reverse_player_aggregates,
    )

    # --- Tournament Routes (Phase 2 extraction) ---
    register_tournament_routes(
        app,
        db=db,
        tournament_engine=tournament_engine,
        Tournament=Tournament,
        DBTeam=DBTeam,
        DBMatch=DBMatch,
        MatchScorecard=MatchScorecard,
        MatchPartnership=MatchPartnership,
        TournamentFixture=TournamentFixture,
        reverse_player_aggregates=reverse_player_aggregates,
        MATCH_INSTANCES=MATCH_INSTANCES,
        MATCH_INSTANCES_LOCK=MATCH_INSTANCES_LOCK,
        PROJECT_ROOT=PROJECT_ROOT,
    )

    # ===== Statistics / Comparison / Stats APIs (Phase 1 extraction) =====
    register_stats_routes(
        app,
        limiter=limiter,
        db=db,
        Team=Team,
        Tournament=Tournament,
        DBPlayer=DBPlayer,
        DBTeam=DBTeam,
        DBMatch=DBMatch,
        MatchScorecard=MatchScorecard,
        MatchPartnership=MatchPartnership,
        StatsService=StatsService,
        aliased=aliased,
        func=func,
    )


    # ======================================================================
    # SocketIO Event Handlers
    # Additive: the HTTP POST /next-ball route above is completely unchanged.
    # These handlers mirror that route's logic using closures over the local
    # helper functions (_load_match_file_for_user, etc.) defined above.
    # ======================================================================
    if _SOCKETIO_AVAILABLE and socketio:
        socketio.init_app(app, async_mode='threading',
                          cors_allowed_origins="*",
                          logger=False, engineio_logger=False)

        @socketio.on('next_ball')
        def _ws_next_ball(data):
            from flask_socketio import emit as ws_emit
            from flask_login import current_user

            if not current_user.is_authenticated:
                ws_emit('ws_error', {'message': 'Not authenticated'})
                return

            match_id = (data or {}).get('match_id', '')
            if not match_id:
                ws_emit('ws_error', {'message': 'match_id required'})
                return

            try:
                with MATCH_INSTANCES_LOCK:
                    if match_id not in MATCH_INSTANCES:
                        match_data_ws, _path_ws, _err_ws = _load_match_file_for_user(match_id)
                        if match_data_ws:
                            if 'rain_probability' not in match_data_ws:
                                match_data_ws['rain_probability'] = load_config().get('rain_probability', 0.0)
                            MATCH_INSTANCES[match_id] = Match(match_data_ws)
                        else:
                            ws_emit('ws_error', {'message': 'Match not found'})
                            return
                    match = MATCH_INSTANCES[match_id]

                if match.data.get('created_by') != current_user.id:
                    ws_emit('ws_error', {'message': 'Unauthorized'})
                    return

                outcome = match.next_ball()

                if outcome.get('match_over'):
                    first_completion = match.data.get('current_state') != 'completed'
                    if first_completion:
                        increment_matches_simulated()
                        if match.data.get('tournament_id'):
                            _handle_tournament_match_completion(match, match_id, outcome, app.logger)
                        else:
                            _persist_non_tournament_match_completion(match, match_id, outcome, app.logger)
                    ws_emit('ball_result', {**outcome, 'match_over': True})
                else:
                    ws_emit('ball_result', outcome)

            except Exception as exc:
                app.logger.error(f'[WS next_ball] match={match_id}: {exc}', exc_info=True)
                ws_emit('ws_error', {'message': 'Internal error', 'details': str(exc)})

        app.logger.info('[SocketIO] WebSocket support enabled (threading mode).')

    return app


# WSGI entrypoint used by gunicorn/flask CLI.
app = create_app()

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

        # Run Flask app (SocketIO-aware when available, plain Flask otherwise)
        # C6: Use computed HOST (127.0.0.1 for local/dev, 0.0.0.0 for prod)
        if _SOCKETIO_AVAILABLE and socketio:
            socketio.run(app, host=HOST, port=PORT, debug=is_local,
                         use_reloader=False, allow_unsafe_werkzeug=True)
        else:
            app.run(host=HOST, port=PORT, debug=is_local, use_reloader=False)

    except Exception as e:
        print("[ERROR] Failed to start SimCricketX:")
        traceback.print_exc()
