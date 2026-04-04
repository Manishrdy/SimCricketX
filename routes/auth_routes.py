"""Authentication and account route registration."""

import re
import secrets
import hashlib
from datetime import datetime, timedelta

from flask import flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from utils.email_service import (
    send_verification_email,
    send_password_reset_email,
    send_account_deletion_email,
)
from utils.exception_tracker import log_exception

# Resend-verification rate-limit constants
_RESEND_MAX = 3
_RESEND_WINDOW_HOURS = 4

# Account lockout constants (configurable via app.config)
_LOCKOUT_MAX_ATTEMPTS = 5
_LOCKOUT_DURATION_MINUTES = 30
_LOCKOUT_WINDOW_MINUTES = 60


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
    AuthEventLog,
    get_client_ip,
    generate_email_verify_token,
    generate_password_reset_token,
    update_user_email,
):
    def _log_auth_event(event_type, email, details=None, user_id=None):
        """Persist an AuthEventLog record. Best-effort — never raises."""
        try:
            entry = AuthEventLog(
                event_type=event_type,
                email=email,
                user_id=user_id,
                details=details,
                ip_address=get_client_ip(),
                status='pending',
            )
            db.session.add(entry)
            db.session.commit()
        except Exception as exc:
            log_exception(exc)
            db.session.rollback()
            app.logger.error(f"[AuthEvent] Failed to log {event_type} for {email}: {exc}")

    # Endpoints exempt from the force-email-verify redirect
    _FORCE_VERIFY_EXEMPT = {
        'force_verify_email',
        'force_verify_email_send',
        'force_verify_email_change',
        'verify_email',
        'logout',
        'static',
        'auth_challenge',
    }

    @app.before_request
    def enforce_force_email_verify():
        """Redirect authenticated users who still need to re-verify their email."""
        if not current_user.is_authenticated:
            return
        if not getattr(current_user, 'force_email_verify', False):
            session.pop("force_email_verify", None)
            return
        if request.endpoint in _FORCE_VERIFY_EXEMPT:
            return
        return redirect(url_for('force_verify_email'))

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

            if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
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
                # Send verification email immediately after registration
                user = db.session.get(DBUser, email)
                if user and user.email_verify_token:
                    verify_link = url_for(
                        "verify_email", token=user.email_verify_token, _external=True
                    )
                    sent = send_verification_email(
                        email,
                        display_name or email,
                        verify_link,
                    )
                    if not sent:
                        _log_auth_event(
                            'email_send_failure',
                            email,
                            details="send_verification_email returned False on registration",
                            user_id=email,
                        )
                session["pending_verify_email"] = email
                return redirect(url_for("verify_email_pending"))
            return render_template(
                "register.html",
                error="Registration failed. Please try a different email.",
            )
        except Exception as e:
            log_exception(e)
            app.logger.error(f"Registration error: {e}")
            return render_template("register.html", error="System error")

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=["POST"])
    def login():
        try:
            if request.method == "GET":
                if current_user.is_authenticated and session.get("session_token"):
                    return redirect(url_for("home"))
                return render_template("login.html")

            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            remember_me = request.form.get("remember_me") == "on"
            challenge_id = request.form.get("challenge_id", "")
            challenge_counter = request.form.get("challenge_counter", "")
            challenge_digest = request.form.get("challenge_digest", "")

            if not email or not password:
                return render_template("login.html", error="Email and password are required.", error_type="validation")

            challenge_ok, challenge_msg = verify_auth_pow_solution(
                challenge_id,
                challenge_counter,
                challenge_digest,
            )
            if not challenge_ok:
                return render_template(
                    "login.html",
                    error="Security check failed. Please refresh the page and try again.",
                    error_type="security",
                )

            # ── Lockout pre-check ──────────────────────────────────────────────
            user_precheck = db.session.get(DBUser, email)
            # Expire the cached object so the next attribute access issues a fresh
            # SELECT, preventing stale identity-map data from hiding an active lockout.
            if user_precheck:
                db.session.expire(user_precheck)
            if user_precheck and user_precheck.lockout_until:
                now = datetime.utcnow()
                if user_precheck.lockout_until > now:
                    unlock_time = user_precheck.lockout_until.strftime('%Y-%m-%d %H:%M UTC')
                    app.logger.warning(f"[Auth] Locked account {email} attempted login")
                    return render_template(
                        "login.html",
                        error=f"Account temporarily locked due to too many failed attempts. Try again after {unlock_time}.",
                        error_type="locked",
                    )
                else:
                    # Auto-expire lockout
                    user_precheck.lockout_until = None
                    user_precheck.lockout_count = 0
                    user_precheck.lockout_window_start = None
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

            if verify_user(email, password):
                user = db.session.get(DBUser, email)
                if user:
                    if not user.email_verified:
                        session["pending_verify_email"] = email
                        return render_template(
                            "login.html",
                            error="Please verify your email before signing in.",
                            error_type="unverified",
                            unverified_email=email,
                        )

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
                                error_type="banned",
                            )

                    # Clear lockout on successful login
                    if user.lockout_count > 0 or user.lockout_until:
                        user.lockout_count = 0
                        user.lockout_until = None
                        user.lockout_window_start = None

                    login_user(
                        user,
                        remember=remember_me,
                        duration=app.config.get("REMEMBER_COOKIE_DURATION") if remember_me else None,
                    )
                    session.permanent = remember_me
                    session["show_github_star_prompt"] = True

                    try:
                        # ── Enforce 1 active session: revoke all previous ──────
                        ActiveSession.query.filter_by(user_id=email).delete(synchronize_session=False)
                        db.session.flush()

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
                        log_exception(e)
                        db.session.rollback()
                        app.logger.error(f"[Auth] Session tracking error: {e}")

                    if user.force_email_verify:
                        session["force_email_verify"] = True
                        app.logger.info(f"[Auth] Redirecting {email} to force email verify")
                        return redirect(url_for("force_verify_email"))

                    if user.force_password_reset:
                        session["force_password_reset"] = True
                        return redirect(url_for("force_change_password"))

                    app.logger.info(f"Successful login for {email}")
                    return redirect(url_for("home"))
            else:
                # ── Record failed attempt + lockout logic ──────────────────────
                try:
                    failed = FailedLoginAttempt(
                        email=email,
                        ip_address=get_client_ip(),
                        user_agent=request.user_agent.string[:300]
                        if request.user_agent.string
                        else None,
                    )
                    db.session.add(failed)
                    db.session.flush()

                    user_fail = db.session.get(DBUser, email)
                    if user_fail:
                        now = datetime.utcnow()
                        lockout_max = app.config.get("LOCKOUT_MAX_ATTEMPTS", _LOCKOUT_MAX_ATTEMPTS)
                        lockout_dur = app.config.get("LOCKOUT_DURATION_MINUTES", _LOCKOUT_DURATION_MINUTES)
                        lockout_win = app.config.get("LOCKOUT_WINDOW_MINUTES", _LOCKOUT_WINDOW_MINUTES)

                        window_expired = (
                            user_fail.lockout_window_start is None
                            or (now - user_fail.lockout_window_start) > timedelta(minutes=lockout_win)
                        )
                        if window_expired:
                            user_fail.lockout_count = 0
                            user_fail.lockout_window_start = now

                        user_fail.lockout_count = (user_fail.lockout_count or 0) + 1

                        if user_fail.lockout_count >= lockout_max:
                            user_fail.lockout_until = now + timedelta(minutes=lockout_dur)
                            _log_auth_event(
                                'account_locked',
                                email,
                                details=f"Locked after {user_fail.lockout_count} failed attempts",
                                user_id=email,
                            )
                            app.logger.warning(f"[Auth] Account {email} locked after {user_fail.lockout_count} failed attempts")

                    db.session.commit()
                except Exception:
                    db.session.rollback()
                return render_template("login.html", error="Invalid email or password.", error_type="credentials")

        except Exception as e:
            log_exception(e)
            app.logger.error(f"Login error: {e}")
            return render_template("login.html", error="A system error occurred. Please try again.", error_type="system")

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
            log_exception(e)
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
            log_exception(e)
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

        # Capture user details before deletion — the record won't exist afterward
        email = current_user.id
        display_name = current_user.display_name or email
        _now = datetime.utcnow()
        deletion_date = f"{_now.strftime('%B')} {_now.day}, {_now.strftime('%Y at %H:%M UTC')}"

        app.logger.info(f"Account deletion requested for {email}")
        if delete_user(email, requesting_user_email=current_user.id):
            logout_user()
            # Fire-and-forget — don't block the redirect on email success
            try:
                send_account_deletion_email(email, display_name, deletion_date)
            except Exception as exc:
                log_exception(exc)
                app.logger.error(f"[Auth] Deletion email failed for {email}: {exc}")
            return redirect(url_for("register"))

        flash("Failed to delete account. Please try again.", "danger")
        return redirect(url_for("home"))

    @app.route("/account/sessions", methods=["GET"])
    @login_required
    def account_sessions():
        """Show all active sessions for the current user."""
        sessions = ActiveSession.query.filter_by(
            user_id=current_user.id
        ).order_by(ActiveSession.last_active.desc()).all()
        current_token = session.get("session_token")
        return render_template("account_sessions.html", sessions=sessions, current_token=current_token)

    @app.route("/account/sessions/revoke", methods=["POST"])
    @login_required
    def revoke_session():
        """Revoke a specific session by ID."""
        session_id = request.form.get("session_id", type=int)
        current_token = session.get("session_token")
        if not session_id:
            flash("Invalid request.", "danger")
            return redirect(url_for("account_sessions"))
        target = ActiveSession.query.filter_by(
            id=session_id, user_id=current_user.id
        ).first()
        if not target:
            flash("Session not found.", "danger")
            return redirect(url_for("account_sessions"))
        if target.session_token == current_token:
            flash("Cannot revoke your current session. Use Sign Out instead.", "warning")
            return redirect(url_for("account_sessions"))
        try:
            db.session.delete(target)
            db.session.commit()
            app.logger.info(f"[Auth] Session {session_id} revoked by {current_user.id}")
            flash("Session revoked successfully.", "success")
        except Exception as e:
            log_exception(e)
            db.session.rollback()
            app.logger.error(f"[Auth] Session revoke error: {e}")
            flash("Failed to revoke session.", "danger")
        return redirect(url_for("account_sessions"))

    @app.route("/account/sessions/revoke-all", methods=["POST"])
    @login_required
    def revoke_all_sessions():
        """Revoke all sessions except the current one."""
        current_token = session.get("session_token")
        try:
            deleted = ActiveSession.query.filter(
                ActiveSession.user_id == current_user.id,
                ActiveSession.session_token != current_token
            ).delete(synchronize_session=False)
            db.session.commit()
            app.logger.info(f"[Auth] {deleted} other session(s) revoked by {current_user.id}")
            flash(f"Signed out of {deleted} other device(s).", "success")
        except Exception as e:
            log_exception(e)
            db.session.rollback()
            app.logger.error(f"[Auth] Revoke-all error: {e}")
            flash("Failed to sign out other devices.", "danger")
        return redirect(url_for("account_sessions"))

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

    # ── Email verification ─────────────────────────────────────────────────────

    @app.route("/verify-email")
    def verify_email():
        """Consume a one-time email verification token."""
        token = request.args.get("token", "").strip()
        if not token:
            flash("Invalid verification link.", "danger")
            return redirect(url_for("login"))

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user = DBUser.query.filter_by(email_verify_token=token_hash).first()
        if not user:
            flash("Invalid or already-used verification link.", "danger")
            return redirect(url_for("login"))

        if datetime.utcnow() > user.email_verify_token_expires:
            session["pending_verify_email"] = user.id
            _log_auth_event(
                'verify_token_expired',
                user.id,
                details="User clicked expired verification link",
                user_id=user.id,
            )
            flash("Verification link has expired. Request a new one below.", "warning")
            return redirect(url_for("verify_email_pending"))

        try:
            user.email_verified = True
            user.force_email_verify = False
            user.email_verify_token = None
            user.email_verify_token_expires = None
            db.session.commit()
            app.logger.info(f"[Auth] Email verified for {user.id}")
        except Exception as e:
            log_exception(e)
            db.session.rollback()
            app.logger.error(f"[Auth] Email verification DB error: {e}")
            flash("Something went wrong. Please try again.", "danger")
            return redirect(url_for("login"))

        session.pop("force_email_verify", None)
        flash("Email verified! You can now sign in.", "success")
        return redirect(url_for("login"))

    @app.route("/verify-email-pending")
    def verify_email_pending():
        """Holding page shown after registration or when verification is needed."""
        email = session.get("pending_verify_email", "")
        return render_template("verify_email_pending.html", email=email)

    # ── Force email verification (for pre-existing users) ──────────────────────

    @app.route("/force-verify-email", methods=["GET"])
    @login_required
    def force_verify_email():
        """Full-page prompt for users who must re-verify their email."""
        if not current_user.force_email_verify:
            return redirect(url_for("home"))
        return render_template(
            "force_verify_email.html",
            email=current_user.id,
            display_name=current_user.display_name or current_user.id,
        )

    @app.route("/force-verify-email/send", methods=["POST"])
    @login_required
    @limiter.limit("5 per hour")
    def force_verify_email_send():
        """Send a verification link to the user's current email."""
        if not current_user.force_email_verify:
            return redirect(url_for("home"))
        email = current_user.id
        token = generate_email_verify_token(email)
        if token:
            verify_link = url_for("verify_email", token=token, _external=True)
            sent = send_verification_email(email, current_user.display_name or email, verify_link)
            if sent:
                flash("Verification link sent! Check your inbox (and spam folder).", "success")
            else:
                flash("Failed to send email. Please try again shortly.", "danger")
                _log_auth_event(
                    'email_send_failure',
                    email,
                    details="force_verify_email_send: send_verification_email returned False",
                    user_id=email,
                )
        else:
            flash("Could not generate a verification token. Please try again.", "danger")
        return redirect(url_for("force_verify_email"))

    @app.route("/force-verify-email/change-email", methods=["POST"])
    @login_required
    @limiter.limit("5 per hour")
    def force_verify_email_change():
        """Let the user switch to a new email, then send verification to it."""
        if not current_user.force_email_verify:
            return redirect(url_for("home"))

        new_email = request.form.get("new_email", "").strip().lower()
        if not new_email or "@" not in new_email or "." not in new_email:
            flash("Please enter a valid email address.", "danger")
            return redirect(url_for("force_verify_email"))

        old_email = current_user.id
        if new_email == old_email:
            flash("That's already your current email. Use 'Verify existing email' instead.", "warning")
            return redirect(url_for("force_verify_email"))

        # Change the email in the DB (cascades to all FK tables)
        ok, msg = update_user_email(old_email, new_email)
        if not ok:
            flash(f"Could not change email: {msg}", "danger")
            return redirect(url_for("force_verify_email"))

        # Reload the user record under the new PK and reset verification flags
        new_user = db.session.get(DBUser, new_email)
        if not new_user:
            app.logger.error(f"[Auth] force_verify_email_change: couldn't reload user after rename {old_email} -> {new_email}")
            flash("Email changed but your account couldn't be reloaded. Please sign in again.", "warning")
            logout_user()
            return redirect(url_for("login"))

        try:
            new_user.email_verified = False
            new_user.force_email_verify = True
            db.session.commit()
        except Exception as e:
            log_exception(e)
            db.session.rollback()
            app.logger.error(f"[Auth] force_verify_email_change: flag reset failed: {e}")

        # Generate verification token for the new email
        token = generate_email_verify_token(new_email)

        # Re-authenticate under the new identity (PK changed)
        logout_user()
        refreshed = db.session.get(DBUser, new_email)
        if refreshed:
            login_user(refreshed, remember=True, duration=app.config.get("REMEMBER_COOKIE_DURATION"))
            session.permanent = True
            session["force_email_verify"] = True

        # Send verification email to the new address
        if token:
            verify_link = url_for("verify_email", token=token, _external=True)
            sent = send_verification_email(new_email, (refreshed.display_name if refreshed else None) or new_email, verify_link)
            if sent:
                app.logger.info(f"[Auth] Email changed {old_email} -> {new_email}; verification sent")
                flash(f"Email updated to {new_email}. A verification link has been sent — check your inbox.", "success")
            else:
                _log_auth_event(
                    'email_send_failure',
                    new_email,
                    details="force_verify_email_change: send_verification_email returned False after email change",
                    user_id=new_email,
                )
                flash(f"Email updated to {new_email}, but we couldn't send the verification email. Use 'Verify existing email' to retry.", "warning")
        else:
            flash(f"Email updated to {new_email}, but verification token generation failed. Please try again.", "warning")

        return redirect(url_for("force_verify_email"))

    @app.route("/resend-verification", methods=["POST"])
    @limiter.limit("10 per hour", methods=["POST"])  # broad IP guard; per-email logic below
    def resend_verification():
        """Resend the verification email.
        Per-email cap: 3 attempts per 4-hour window (DB-backed, persistent).
        """
        email = request.form.get("email", "").strip().lower()
        session["pending_verify_email"] = email

        user = db.session.get(DBUser, email) if email else None

        if user and not user.email_verified:
            now = datetime.utcnow()
            window_start = user.verify_resend_window_start
            window_expired = (
                window_start is None
                or (now - window_start) > timedelta(hours=_RESEND_WINDOW_HOURS)
            )

            if window_expired:
                # Fresh window — reset counter
                user.verify_resend_count = 0
                user.verify_resend_window_start = now

            if user.verify_resend_count >= _RESEND_MAX:
                # Rate limit hit — log the event and tell the user
                hours_left = _RESEND_WINDOW_HOURS - int(
                    (now - user.verify_resend_window_start).total_seconds() / 3600
                )
                app.logger.warning(f"[Auth] Resend rate limit hit for {email}")
                _log_auth_event(
                    'resend_rate_limit',
                    email,
                    details=f"Resend cap ({_RESEND_MAX}/{_RESEND_WINDOW_HOURS}h) hit. "
                            f"~{max(1, hours_left)}h remaining in window.",
                    user_id=user.id,
                )
                flash(
                    f"Too many resend attempts. Please wait up to {_RESEND_WINDOW_HOURS} hours "
                    "before trying again, or contact support.",
                    "warning",
                )
                return redirect(url_for("verify_email_pending"))

            # Send the email
            token = generate_email_verify_token(email)
            if token:
                verify_link = url_for("verify_email", token=token, _external=True)
                sent = send_verification_email(email, user.display_name or email, verify_link)
                if sent:
                    user.verify_resend_count += 1
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    app.logger.info(f"[Auth] Verification email resent to {email} "
                                    f"(attempt {user.verify_resend_count}/{_RESEND_MAX})")
                else:
                    db.session.rollback()
                    app.logger.error(f"[Auth] Resend email send failure for {email}")
                    _log_auth_event(
                        'email_send_failure',
                        email,
                        details="send_verification_email returned False on resend",
                        user_id=user.id,
                    )

        flash(
            "If that email is registered and unverified, a new link has been sent.",
            "info",
        )
        return redirect(url_for("verify_email_pending"))

    # ── Password reset ─────────────────────────────────────────────────────────

    @app.route("/forgot-password", methods=["GET", "POST"])
    @limiter.limit("5 per hour", methods=["POST"])
    def forgot_password():
        """Step 1 — User submits their email to receive a reset link."""
        if request.method == "GET":
            return render_template("forgot_password.html")

        email = request.form.get("email", "").strip().lower()

        # Always show the same response to prevent email enumeration
        if email:
            token = generate_password_reset_token(email)
            if token:
                user = db.session.get(DBUser, email)
                reset_link = url_for("reset_password", token=token, _external=True)
                sent = send_password_reset_email(
                    email,
                    user.display_name or email,
                    reset_link,
                )
                if sent:
                    app.logger.info(f"[Auth] Password reset email sent to {email}")
                else:
                    app.logger.error(f"[Auth] Password reset email send failure for {email}")
                    _log_auth_event(
                        'email_send_failure',
                        email,
                        details="send_password_reset_email returned False",
                        user_id=user.id if user else None,
                    )

        flash(
            "If that email is registered, a password reset link has been sent.",
            "info",
        )
        return redirect(url_for("forgot_password"))

    @app.route("/reset-password", methods=["GET", "POST"])
    @limiter.limit("10 per hour", methods=["POST"])
    def reset_password():
        """Step 2 — User clicks the emailed link and sets a new password."""
        token = request.args.get("token", "").strip()

        if not token:
            flash("Invalid reset link.", "danger")
            return redirect(url_for("forgot_password"))

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user = DBUser.query.filter_by(reset_token=token_hash).first()
        if not user:
            flash("Invalid or already-used reset link.", "danger")
            return redirect(url_for("forgot_password"))

        # Expire cached state so the expiry column is read fresh from the DB.
        db.session.expire(user)
        if not user.reset_token_expires or datetime.utcnow() > user.reset_token_expires:
            _log_auth_event(
                'reset_token_expired',
                user.id,
                details="User clicked expired password-reset link",
                user_id=user.id,
            )
            flash("This reset link has expired. Please request a new one.", "warning")
            return redirect(url_for("forgot_password"))

        if request.method == "GET":
            return render_template("reset_password.html", token=token)

        # POST — validate and apply new password
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        ok, policy_error = validate_password_policy(new_password)
        if not ok:
            return render_template("reset_password.html", token=token, error=policy_error)
        if new_password != confirm_password:
            return render_template("reset_password.html", token=token, error="Passwords do not match.")

        try:
            from werkzeug.security import generate_password_hash

            user.password_hash = generate_password_hash(new_password)
            user.reset_token = None
            user.reset_token_expires = None
            user.force_password_reset = False
            # Proving ownership of the inbox also verifies the email
            user.email_verified = True

            # Invalidate all active sessions so old sessions can't be reused
            ActiveSession.query.filter_by(user_id=user.id).delete(synchronize_session=False)

            db.session.commit()
            app.logger.info(f"[Auth] Password reset completed for {user.id}")
        except Exception as e:
            log_exception(e)
            db.session.rollback()
            app.logger.error(f"[Auth] Password reset DB error for {user.id}: {e}")
            return render_template("reset_password.html", token=token, error="Something went wrong. Please try again.")

        flash("Password reset successfully! You can now sign in with your new password.", "success")
        return redirect(url_for("login"))

    # ── Account Settings ────────────────────────────────────────────────────────

    @app.route("/account/settings", methods=["GET"])
    @login_required
    def account_settings():
        """Account settings page — display name and email change."""
        return render_template("account_settings.html")

    @app.route("/account/settings/display-name", methods=["POST"])
    @login_required
    @limiter.limit("5 per hour")
    def account_change_display_name():
        """Change the current user's display name."""
        new_name = request.form.get("display_name", "").strip()
        if not new_name or len(new_name) < 2:
            flash("Display name must be at least 2 characters.", "danger")
            return redirect(url_for("account_settings"))
        if len(new_name) > 50:
            flash("Display name must be 50 characters or fewer.", "danger")
            return redirect(url_for("account_settings"))
        try:
            current_user.display_name = new_name
            db.session.commit()
            app.logger.info(f"[Auth] Display name updated for {current_user.id}: {new_name}")
            flash("Display name updated successfully.", "success")
        except Exception as e:
            log_exception(e)
            db.session.rollback()
            app.logger.error(f"[Auth] Display name update error: {e}")
            flash("Failed to update display name. Please try again.", "danger")
        return redirect(url_for("account_settings"))

    @app.route("/account/settings/request-email-change", methods=["POST"])
    @login_required
    @limiter.limit("3 per hour")
    def account_request_email_change():
        """Step 1 — store pending email and send verification link to new address."""
        from werkzeug.security import check_password_hash

        new_email = request.form.get("new_email", "").strip().lower()
        current_password = request.form.get("current_password", "")

        if not new_email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', new_email):
            flash("Please enter a valid email address.", "danger")
            return redirect(url_for("account_settings"))

        if new_email == current_user.id:
            flash("That is already your current email address.", "warning")
            return redirect(url_for("account_settings"))

        if db.session.get(DBUser, new_email):
            flash("That email address is already registered to another account.", "danger")
            return redirect(url_for("account_settings"))

        if not check_password_hash(current_user.password_hash, current_password):
            flash("Current password is incorrect.", "danger")
            return redirect(url_for("account_settings"))

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        try:
            current_user.pending_email = new_email
            current_user.pending_email_token = token_hash
            current_user.pending_email_token_expires = datetime.utcnow() + timedelta(hours=6)
            db.session.commit()
        except Exception as e:
            log_exception(e)
            db.session.rollback()
            app.logger.error(f"[Auth] Email change request error: {e}")
            flash("Failed to initiate email change. Please try again.", "danger")
            return redirect(url_for("account_settings"))

        verify_link = url_for("account_confirm_email_change", token=token, _external=True)
        sent = send_verification_email(new_email, current_user.display_name or current_user.id, verify_link)
        if sent:
            app.logger.info(f"[Auth] Email change requested: {current_user.id} -> {new_email}")
            flash(f"Verification link sent to {new_email}. Click the link in that email to confirm the change.", "success")
        else:
            flash("Could not send verification email. Please try again later.", "danger")
        return redirect(url_for("account_settings"))

    @app.route("/account/settings/cancel-email-change", methods=["POST"])
    @login_required
    def account_cancel_email_change():
        """Cancel a pending email change."""
        try:
            current_user.pending_email = None
            current_user.pending_email_token = None
            current_user.pending_email_token_expires = None
            db.session.commit()
            flash("Pending email change cancelled.", "success")
        except Exception as e:
            log_exception(e)
            db.session.rollback()
            app.logger.error(f"[Auth] Cancel email change error: {e}")
            flash("Failed to cancel. Please try again.", "danger")
        return redirect(url_for("account_settings"))

    @app.route("/account/confirm-email-change")
    def account_confirm_email_change():
        """Step 2 — user clicks link in new email inbox; commit the PK cascade."""
        token = request.args.get("token", "").strip()
        if not token:
            flash("Invalid confirmation link.", "danger")
            return redirect(url_for("login"))

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user = DBUser.query.filter_by(pending_email_token=token_hash).first()
        if not user:
            # Render the login template directly so the error message is present
            # in the response body regardless of whether flash messages are shown.
            return render_template(
                "login.html",
                error="Invalid or already-used confirmation link.",
                error_type="invalid",
            )

        # Expire cached state so the expiry column is read fresh from the DB.
        db.session.expire(user)
        if not user.pending_email_token_expires or datetime.utcnow() > user.pending_email_token_expires:
            flash("Confirmation link has expired. Please request a new email change from account settings.", "warning")
            return render_template(
                "login.html",
                error="Confirmation link has expired. Please request a new email change from account settings.",
                error_type="expired",
            )

        new_email = user.pending_email
        old_email = user.id

        # Clear pending fields before the PK cascade
        try:
            user.pending_email = None
            user.pending_email_token = None
            user.pending_email_token_expires = None
            db.session.commit()
        except Exception:
            db.session.rollback()

        ok, msg = update_user_email(old_email, new_email)
        if not ok:
            app.logger.error(f"[Auth] Email change cascade failed {old_email} -> {new_email}: {msg}")
            flash(f"Could not complete email change: {msg}", "danger")
            return redirect(url_for("login"))

        app.logger.info(f"[Auth] Email changed: {old_email} -> {new_email}")
        # Force re-login under the new identity
        logout_user()
        flash("Email changed successfully. Please sign in with your new email address.", "success")
        return redirect(url_for("login"))

    # ── Login History ────────────────────────────────────────────────────────────

    @app.route("/account/login-history")
    @login_required
    def account_login_history():
        """Show the current user's login history, paginated."""
        page = request.args.get("page", 1, type=int)
        per_page = 25
        history = LoginHistory.query.filter_by(
            user_id=current_user.id
        ).order_by(LoginHistory.timestamp.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )
        return render_template("account_login_history.html", history=history)
