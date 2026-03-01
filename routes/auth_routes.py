"""Authentication and account route registration."""

from datetime import datetime

from flask import flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user


def register_auth_routes(
    app,
    *,
    limiter,
    db,
    register_user,
    verify_user,
    delete_user,
    validate_password_policy,
    verify_auth_pow_solution,
    issue_auth_pow_challenge,
    DBUser,
    FailedLoginAttempt,
    ActiveSession,
    LoginHistory,
    get_client_ip,
):
    @app.route("/register", methods=["GET", "POST"])
    @limiter.limit("5 per minute", methods=["POST"])
    def register():
        """Simplified registration route."""
        try:
            if request.method == "GET":
                return render_template("register.html")

            display_name = request.form.get("display_name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")
            challenge_id = request.form.get("challenge_id", "")
            challenge_counter = request.form.get("challenge_counter", "")
            challenge_digest = request.form.get("challenge_digest", "")

            challenge_ok, challenge_msg = verify_auth_pow_solution(
                challenge_id,
                challenge_counter,
                challenge_digest,
            )
            if not challenge_ok:
                return render_template(
                    "register.html",
                    error=f"Security challenge failed: {challenge_msg}",
                )

            if not email or "@" not in email or "." not in email:
                return render_template("register.html", error="Invalid email")
            if not display_name:
                return render_template("register.html", error="Display name is required")
            if len(display_name) > 50:
                return render_template(
                    "register.html",
                    error="Display name must be 50 characters or fewer",
                )
            if not password:
                return render_template("register.html", error="Password required")
            if password != confirm_password:
                return render_template("register.html", error="Passwords do not match")

            ok, policy_error = validate_password_policy(password)
            if not ok:
                return render_template("register.html", error=policy_error)

            if register_user(email, password, display_name=display_name):
                return redirect(url_for("login"))
            return render_template(
                "register.html",
                error="Registration failed. Please try a different email.",
            )
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
            challenge_id = request.form.get("challenge_id", "")
            challenge_counter = request.form.get("challenge_counter", "")
            challenge_digest = request.form.get("challenge_digest", "")

            if not email or not password:
                return render_template("login.html", error="Email and password required")

            challenge_ok, challenge_msg = verify_auth_pow_solution(
                challenge_id,
                challenge_counter,
                challenge_digest,
            )
            if not challenge_ok:
                return render_template(
                    "login.html",
                    error=f"Security challenge failed: {challenge_msg}",
                )

            if verify_user(email, password):
                user = db.session.get(DBUser, email)
                if user:
                    if user.is_banned:
                        if user.banned_until and user.banned_until <= datetime.utcnow():
                            user.is_banned = False
                            user.banned_until = None
                            user.ban_reason = None
                            db.session.commit()
                        else:
                            reason = user.ban_reason or "No reason provided"
                            until = (
                                f" until {user.banned_until.strftime('%Y-%m-%d %H:%M UTC')}"
                                if user.banned_until
                                else " (permanent)"
                            )
                            app.logger.warning(f"[Auth] Banned user {email} attempted login")
                            return render_template(
                                "login.html",
                                error=f"Account suspended{until}. Reason: {reason}",
                            )

                    login_user(
                        user,
                        remember=True,
                        duration=app.config.get("REMEMBER_COOKIE_DURATION"),
                    )
                    session.permanent = True
                    session["show_github_star_prompt"] = True

                    try:
                        import secrets

                        token = secrets.token_hex(32)
                        session["session_token"] = token
                        active = ActiveSession(
                            session_token=token,
                            user_id=email,
                            ip_address=get_client_ip(),
                            user_agent=request.user_agent.string[:300]
                            if request.user_agent.string
                            else None,
                        )
                        db.session.add(active)
                        db.session.add(LoginHistory(
                            user_id=email,
                            ip_address=get_client_ip(),
                            user_agent=request.user_agent.string[:300] if request.user_agent.string else None,
                            event='login',
                        ))
                        db.session.commit()
                    except Exception as e:
                        db.session.rollback()
                        app.logger.error(f"[Auth] Session tracking error: {e}")

                    if user.force_password_reset:
                        session["force_password_reset"] = True
                        return redirect(url_for("force_change_password"))

                    app.logger.info(f"Successful login for {email}")
                    return redirect(url_for("home"))
            else:
                try:
                    failed = FailedLoginAttempt(
                        email=email,
                        ip_address=get_client_ip(),
                        user_agent=request.user_agent.string[:300]
                        if request.user_agent.string
                        else None,
                    )
                    db.session.add(failed)
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                return render_template("login.html", error="Invalid email or password")

        except Exception as e:
            app.logger.error(f"Login error: {e}")
            return render_template("login.html", error="System error")

    @app.route("/auth/challenge", methods=["GET"])
    @limiter.limit("30 per minute")
    def auth_challenge():
        """Issue short-lived proof-of-work challenge for auth forms."""
        payload = issue_auth_pow_challenge()
        response = jsonify(payload)
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response, 200

    @app.route("/change-password", methods=["GET", "POST"])
    @login_required
    def force_change_password():
        """Force password change page."""
        if request.method == "GET":
            return render_template("force_change_password.html")

        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        ok, policy_error = validate_password_policy(new_password)
        if not ok:
            return render_template("force_change_password.html", error=policy_error)
        if new_password != confirm_password:
            return render_template("force_change_password.html", error="Passwords do not match")
        try:
            from werkzeug.security import generate_password_hash

            current_user.password_hash = generate_password_hash(new_password)
            current_user.force_password_reset = False
            db.session.commit()
            session.pop("force_password_reset", None)
            flash("Password changed successfully.", "success")
            return redirect(url_for("home"))
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"[Auth] Force password change error: {e}")
            return render_template("force_change_password.html", error="Failed to change password")

    @app.route("/set-display-name", methods=["GET", "POST"])
    @login_required
    def set_display_name():
        """Force display name entry for users who don't have one."""
        if request.method == "GET":
            return render_template("set_display_name.html")

        display_name = request.form.get("display_name", "").strip()
        if not display_name:
            return render_template("set_display_name.html", error="Display name is required")
        if len(display_name) < 2:
            return render_template(
                "set_display_name.html",
                error="Display name must be at least 2 characters",
            )
        if len(display_name) > 50:
            return render_template(
                "set_display_name.html",
                error="Display name must be less than 50 characters",
            )

        try:
            current_user.display_name = display_name
            db.session.commit()
            app.logger.info(f"[Auth] Display name set for {current_user.id}: {display_name}")
            flash("Display name set successfully!", "success")
            return redirect(url_for("home"))
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"[Auth] Set display name error: {e}")
            return render_template("set_display_name.html", error="Failed to set display name")

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

        flash("Failed to delete account. Please try again.", "danger")
        return redirect(url_for("home"))

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        token = session.get("session_token")
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
        session.pop("_flashes", None)
        return redirect(url_for("login"))
