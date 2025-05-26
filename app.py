import os
import json
import logging
import yaml
from logging.handlers import RotatingFileHandler
from utils.helpers import load_config
from flask import Flask, render_template, request, redirect, url_for, flash
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

# Make sure PROJECT_ROOT is defined near the top of app.py:
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))

def load_app_config():
    base_dir = os.path.abspath(os.path.dirname(__file__))
    config_path = os.path.join(base_dir, "config", "config.yaml")
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

# ────── App Factory ──────
def create_app():
    # --- Flask setup ---
    app = Flask(__name__)
    config = load_config()
    app.config["SECRET_KEY"] = config.get("app", {}).get("secret_key", "default-dev-key")

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


    return app

# ────── Run Server ──────
if __name__ == "__main__":
    app = create_app()
    # debug=False for production
    app.run(host="0.0.0.0", port=5000, debug=False)