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

        return render_template(
            "home.html",
            user=current_user,
            total_visits=get_visit_counter(),
            matches_simulated=get_matches_simulated(),
            active_users=active_users_count,
            app_version=app_version,
            changelog_entries=changelog_entries,
        )

    # ───── Ground Conditions ─────

    @app.route("/ground-conditions")
    @login_required
    def ground_conditions():
        from engine.ground_config import get_config
        config = get_config() or {}
        return render_template("ground_conditions.html", config=config)

    @app.route("/ground-conditions/save", methods=["POST"])
    @login_required
    def ground_conditions_save():
        from engine.ground_config import save_config
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        ok, err = save_config(data)
        if ok:
            return jsonify({"message": "Ground conditions saved successfully"}), 200
        return jsonify({"error": err}), 400

    @app.route("/ground-conditions/mode", methods=["POST"])
    @login_required
    def ground_conditions_set_mode():
        import yaml as _yaml
        from engine.ground_config import reload as reload_gc
        data = request.get_json(silent=True) or {}
        mode = data.get("mode", "natural_game")
        gc_path = os.path.join(basedir, "config", "ground_conditions.yaml")
        try:
            with open(gc_path, "r", encoding="utf-8") as f:
                cfg = _yaml.safe_load(f) or {}
            cfg["active_game_mode"] = mode
            with open(gc_path, "w", encoding="utf-8") as f:
                _yaml.dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            reload_gc()
            return jsonify({"message": f"Game mode set to {mode}"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/ground-conditions/reset", methods=["POST"])
    @login_required
    def ground_conditions_reset():
        from engine.ground_config import reset_to_defaults
        ok, err = reset_to_defaults()
        if ok:
            return jsonify({"message": "Reset to defaults"}), 200
        return jsonify({"error": err}), 500

    @app.route("/ground-conditions/guide")
    def ground_conditions_guide():
        return render_template("ground_conditions_guide.html")

