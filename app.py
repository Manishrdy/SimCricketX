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


MATCH_INSTANCES = {}


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
                "timestamp": ts
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
            app.logger.info(f"Result in main.py {outcome.get("result",  "Match ended")}")

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
        result = match.next_super_over_ball()
        
        return jsonify(result)
    
    return app

# ────── Run Server ──────
if __name__ == "__main__":
    app = create_app()
    # debug=False for production
    app.run(host="0.0.0.0", port=2624, debug=False)