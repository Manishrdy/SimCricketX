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


MATCH_INSTANCES = {}

# How old is “too old”? 7 days → 7*24*3600 seconds
PROD_MAX_AGE = 7 * 24 * 3600

# Make sure PROJECT_ROOT is defined near the top of app.py:
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
CREDENTIALS_FILE = 'auth/credentials.json'

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

# ────── App Factory ──────
def create_app():
    # --- Flask setup ---
    app = Flask(__name__)
    config = load_config()
    

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
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # --- Logging setup ---
    base_dir = os.path.abspath(os.path.dirname(__file__))
    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "execution.log")

    handler = RotatingFileHandler(log_path, maxBytes=10*1024*1024, backupCount=5)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]"
    ))
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)

    # --- Flask-Login setup ---
    login_manager = LoginManager(app)
    login_manager.login_view = "login"

    class User(UserMixin):
        def __init__(self, email):
            self.id = email

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
        if request.method == "POST":
            email = request.form["email"].strip().lower()
            password = request.form["password"]
            app.logger.info(f"Registration attempt for {email}")
            # Registration route
            if register_user(email, password):
                flash("✅ Registration successful! Please log in.", "success")
                return redirect(url_for("login"))
            else:
                flash("❌ User already exists!", "danger")
        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form["email"].strip().lower()
            password = request.form["password"]
            app.logger.info(f"Login attempt for {email}")
            
            if verify_user(email, password):
                app.logger.info("Inside verify_user")
                user = User(email)
                login_user(user)
                session.pop('_flashes', None)  # 🧼 clear any prior flashes
                flash("✅ Logged in successfully!", "success")
                return redirect(url_for("home"))
            else:
                flash("❌ Invalid email or password.", "danger")
        return render_template("login.html")

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

        # ① Clean up old ZIPs before doing anything else
        clean_old_archives(PROD_MAX_AGE)

        teams = load_user_teams(current_user.id)

        if request.method == "POST":
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

            # Step 3: Replace playing_xi with full player data + will_bowl flag
            def enrich_xi(selected, full_team):
                enriched = []
                for sel in selected:
                    match = next((p for p in full_team["players"] if p["name"] == sel["name"]), None)
                    if match:
                        enriched_player = match.copy()
                        enriched_player["will_bowl"] = sel.get("will_bowl", False)
                        enriched.append(enriched_player)
                return enriched

            data["playing_xi"]["home"] = enrich_xi(data["playing_xi"]["home"], full_home)
            data["playing_xi"]["away"] = enrich_xi(data["playing_xi"]["away"], full_away)

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
                "rain_probability": data.get("rain_probability", 0.0)  # ADD THIS
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


    @app.route("/match/<match_id>/next-ball")
    @login_required
    def next_ball(match_id):
        if match_id not in MATCH_INSTANCES:
            match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
            match_data = None
            for fn in os.listdir(match_dir):
                with open(os.path.join(match_dir, fn)) as f:
                    data = json.load(f)
                    if data["match_id"] == match_id:
                        match_data = data
                        break
            if not match_data:
                return jsonify({"error": "Match not found"}), 404
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

    return app

# ────── Run Server ──────
if __name__ == "__main__":
    app = create_app()
    
    # Set ARCHIVES_FOLDER for use in routes, e.g., for sending ZIPs
    import os
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    app.config["ARCHIVES_FOLDER"] = os.path.join(BASE_DIR, "data")

    # Ensure critical config is present
    if not app.config.get("SECRET_KEY"):
        import secrets
        generated_key = secrets.token_hex(32)
        print("[WARNING] No SECRET_KEY set — generating random one (not persistent)")
        app.config["SECRET_KEY"] = generated_key

    # Enforce secure session cookie (important for HTTPS)
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    # Start Flask app
    app.run(host="0.0.0.0", port=7860, debug=False)