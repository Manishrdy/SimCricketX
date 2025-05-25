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
                flash("✅ Logged in successfully!", "success")
                return redirect(url_for("home"))
            else:
                flash("❌ Invalid credentials!", "danger")
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
        flash("You have been logged out.", "info")
        return redirect(url_for("login"))

    return app

# ────── Run Server ──────
if __name__ == "__main__":
    app = create_app()
    # debug=False for production
    app.run(host="0.0.0.0", port=5000, debug=False)