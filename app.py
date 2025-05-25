import os
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
from flask_login import login_required
from engine.team import Team, save_team, PITCH_PREFERENCES
from engine.player import Player, PLAYER_ROLES, BATTING_HANDS, BOWLING_TYPES, BOWLING_HANDS
from auth.user_auth import load_credentials  # adjust import path if needed



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
                path = os.path.join("data", "teams", f"{team.short_code}.json")
                with open(path, "w") as f:
                    json.dump(data, f, indent=2)

                # 3d. Log, flash, redirect
                app.logger.info(f"Team '{team.name}' created by {email} ({user_id})")
                flash(f"✅ Team '{team.name}' saved!", "success")
                return redirect(url_for("home"))

            except Exception as e:
                app.logger.error(f"Unexpected error saving team '{name}': {e}", exc_info=True)
                flash(f"❌ Unexpected error: {e}", "danger")
                return render_template("team_create.html")

        # GET: Show form
        return render_template("team_create.html")
    
    return app

# ────── Run Server ──────
if __name__ == "__main__":
    app = create_app()
    # debug=False for production
    app.run(host="0.0.0.0", port=5000, debug=False)