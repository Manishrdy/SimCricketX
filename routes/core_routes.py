"""Core app route registration (home + ground conditions)."""

import os
from datetime import datetime, timedelta, timezone

from flask import jsonify, render_template, request, session
from flask_login import current_user, login_required


def register_core_routes(
    app,
    *,
    db,
    func,
    ActiveSession,
    AnnouncementBanner,
    UserBannerDismissal,
    _get_app_version,
    get_visit_counter,
    get_matches_simulated,
    increment_visit_counter,
    basedir,
):
    def _get_changelog_for_version(version):
        """Return list of bullet strings for the given version block in changelog.txt."""
        try:
            with open(os.path.join(app.root_path, "changelog.txt")) as f:
                lines = f.readlines()
        except Exception:
            return []

        entries = []
        in_block = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(f"[{version}]"):
                in_block = True
                continue
            if in_block:
                if stripped.startswith("["):
                    break  # next version block started
                if stripped.startswith("-"):
                    entries.append(stripped[1:].strip())
        return entries

    @app.route("/")
    @login_required
    def home():
        if not session.get("visit_counted"):
            increment_visit_counter()
            session["visit_counted"] = True

        # Count currently active users (active session in last 15 minutes)
        active_threshold = datetime.now(timezone.utc) - timedelta(minutes=15)
        active_users_count = db.session.query(func.count(func.distinct(ActiveSession.user_id))).filter(ActiveSession.last_active >= active_threshold).scalar() or 0

        app_version = _get_app_version()
        changelog_entries = _get_changelog_for_version(app_version)
        announcement_banner = None

        banner = AnnouncementBanner.query.first()
        if banner and banner.is_enabled and str(banner.message or "").strip():
            dismissed = (
                db.session.query(UserBannerDismissal.id)
                .filter_by(user_id=current_user.id, banner_version=banner.version)
                .first()
            )
            if not dismissed:
                color_preset = str(getattr(banner, "color_preset", "urgent") or "urgent").strip().lower()
                if color_preset not in {"urgent", "spotlight", "calm"}:
                    color_preset = "urgent"
                position = str(getattr(banner, "position", "bottom") or "bottom").strip().lower()
                if position not in {"top", "bottom"}:
                    position = "bottom"
                announcement_banner = {
                    "message": str(banner.message),
                    "version": int(banner.version or 1),
                    "color_preset": color_preset,
                    "position": position,
                }

        return render_template(
            "home.html",
            user=current_user,
            total_visits=get_visit_counter(),
            matches_simulated=get_matches_simulated(),
            active_users=active_users_count,
            app_version=app_version,
            changelog_entries=changelog_entries,
            announcement_banner=announcement_banner,
        )

    @app.route("/announcement-banner/dismiss", methods=["POST"])
    @login_required
    def dismiss_announcement_banner():
        try:
            banner = AnnouncementBanner.query.first()
            if not banner or not banner.is_enabled or not str(banner.message or "").strip():
                return jsonify({"message": "No active announcement banner"}), 200

            existing = UserBannerDismissal.query.filter_by(
                user_id=current_user.id,
                banner_version=banner.version,
            ).first()
            if not existing:
                db.session.add(
                    UserBannerDismissal(
                        user_id=current_user.id,
                        banner_version=banner.version,
                    )
                )
                db.session.commit()

            return jsonify({"message": "Announcement banner dismissed"}), 200
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Failed to dismiss announcement banner: {e}", exc_info=True)
            return jsonify({"error": "Failed to dismiss announcement banner"}), 500

    # ───── Ground Conditions ─────

    @app.route("/ground-conditions")
    @login_required
    def ground_conditions():
        from engine.ground_config import get_effective_config
        config = get_effective_config(current_user.id)
        return render_template("ground_conditions.html", config=config)

    @app.route("/ground-conditions/save", methods=["POST"])
    @login_required
    def ground_conditions_save():
        from engine.ground_config import save_user_config
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        ok, err = save_user_config(current_user.id, data)
        if ok:
            return jsonify({"message": "Ground conditions saved successfully"}), 200
        return jsonify({"error": err}), 400

    @app.route("/ground-conditions/mode", methods=["POST"])
    @login_required
    def ground_conditions_set_mode():
        from engine.ground_config import get_effective_config, save_user_config
        mode = (request.get_json(silent=True) or {}).get("mode", "natural_game")
        cfg = get_effective_config(current_user.id)
        cfg["active_game_mode"] = mode
        ok, err = save_user_config(current_user.id, cfg)
        if ok:
            return jsonify({"message": f"Game mode set to {mode}"}), 200
        return jsonify({"error": err}), 500

    @app.route("/ground-conditions/reset", methods=["POST"])
    @login_required
    def ground_conditions_reset():
        from engine.ground_config import reset_user_config
        ok, err = reset_user_config(current_user.id)
        if ok:
            return jsonify({"message": "Reset to defaults"}), 200
        return jsonify({"error": err}), 500

    @app.route("/ground-conditions/guide")
    def ground_conditions_guide():
        return render_template("ground_conditions_guide.html")
