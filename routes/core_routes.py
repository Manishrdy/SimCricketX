"""Core app route registration (home + ground conditions)."""

import os
from datetime import datetime, timedelta

from flask import jsonify, render_template, request, send_from_directory, session, redirect, url_for, flash
from flask_login import current_user, login_required
from utils.exception_tracker import log_exception
from database.models import User, Match, SupportPromptEvent
from utils import support_prompt
from utils.changelog import load_changelog


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
    def _active_user_count():
        """Return distinct users with activity inside the presence window.

        ActiveSession timestamps are stored as naive UTC values, so use the
        same representation for reliable comparisons on every database.
        """
        window_minutes = max(1, int(app.config.get("ONLINE_ACTIVITY_MINUTES", 5)))
        active_threshold = datetime.utcnow() - timedelta(minutes=window_minutes)
        count = (
            db.session.query(func.count(func.distinct(ActiveSession.user_id)))
            .filter(ActiveSession.last_active >= active_threshold)
            .scalar()
        )
        return count or 0, window_minutes

    def _get_changelog_for_version(version):
        """Return list of bullet strings for the given version block in changelog.txt."""
        try:
            with open(os.path.join(app.root_path, "changelog.txt")) as f:
                lines = f.readlines()
        except Exception:
            log_exception(source="backend")
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
    def home():
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        active_users_count, online_activity_minutes = _active_user_count()
        platform_stats = {
            "matches_simulated": get_matches_simulated(),
            "matches_today": Match.query.filter(
                Match.date >= today_start, Match.date < tomorrow_start
            ).count(),
            "total_signups": User.query.count(),
            "signups_today": User.query.filter(
                User.created_at >= today_start, User.created_at < tomorrow_start
            ).count(),
            "active_users": active_users_count,
            "online_activity_minutes": online_activity_minutes,
        }
        if not current_user.is_authenticated:
            return render_template(
                "landing.html",
                total_visits=get_visit_counter(),
                **platform_stats,
            )

        if not session.get("visit_counted"):
            increment_visit_counter()
            session["visit_counted"] = True

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

        show_support_prompt = False
        try:
            show_support_prompt = support_prompt.should_show_popup(
                current_user, Match.query.filter_by(user_id=current_user.id).count()
            )
        except Exception as e:
            # The dashboard must render even if the prompt check fails.
            log_exception(e)

        return render_template(
            "home.html",
            user=current_user,
            total_visits=get_visit_counter(),
            **platform_stats,
            app_version=app_version,
            changelog_entries=changelog_entries,
            announcement_banner=announcement_banner,
            show_support_prompt=show_support_prompt,
        )

    @app.route("/support-prompt/event", methods=["POST"])
    @login_required
    def support_prompt_event():
        """Record what the user did with the support popup (shown/closed/clicked/optout)."""
        payload = request.get_json(silent=True) or {}
        event = str(payload.get("event") or "")
        try:
            if not support_prompt.record_event(current_user, event):
                return jsonify({"error": "Unknown event"}), 400
            # The user's own state is what stops the popup repeating, so it is
            # committed first; the analytics history is best-effort and must
            # never undo it (e.g. if that table is missing or locked).
            db.session.commit()
            source = str(payload.get("source") or "popup")
            try:
                db.session.add(SupportPromptEvent(
                    user_id=current_user.id, event=event,
                    source=source if source in support_prompt.SOURCES else "popup",
                ))
                db.session.commit()
            except Exception as history_error:
                db.session.rollback()
                log_exception(history_error)
            return jsonify({"ok": True}), 200
        except Exception as e:
            log_exception(e)
            db.session.rollback()
            app.logger.error(f"Failed to record support prompt event: {e}", exc_info=True)
            return jsonify({"error": "Failed to record event"}), 500

    @app.route("/api/presence")
    @login_required
    def presence():
        """Return the current recent-activity estimate for the home page."""
        active_users_count, window_minutes = _active_user_count()
        response = jsonify({
            "active_users": active_users_count,
            "window_minutes": window_minutes,
        })
        response.headers["Cache-Control"] = "no-store"
        return response

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
            log_exception(e)
            db.session.rollback()
            app.logger.error(f"Failed to dismiss announcement banner: {e}", exc_info=True)
            return jsonify({"error": "Failed to dismiss announcement banner"}), 500

    # ───── Ground Conditions ─────

    def _requested_format(payload=None):
        """Resolve the target match format from the query string or JSON body.

        Unknown values fall back to the default rather than erroring — a bad
        ?format= should show the T20 editor, not a 400.
        """
        from engine.ground_config import normalise_format
        raw = request.args.get("format")
        if raw is None and payload is not None:
            raw = payload.get("match_format")
        if request.method == "POST":
            from engine.format_catalog import SUPPORTED_FORMATS
            if raw is not None and raw not in SUPPORTED_FORMATS:
                from flask import abort
                abort(400, "Invalid or unsupported match format")
        return normalise_format(raw)

    @app.route("/ground-conditions")
    @login_required
    def ground_conditions():
        flash('Ground conditions now live in your private stadiums. Existing setups are preserved.', 'info')
        return redirect(url_for('stadiums'))

    @app.post('/ground-conditions/save')
    @app.post('/ground-conditions/mode')
    @app.post('/ground-conditions/reset')
    @login_required
    def ground_conditions_retired():
        return jsonify(error='Ground Conditions has retired. Configure your private stadium instead.'), 410

    @app.get('/ground-conditions/guide')
    def ground_conditions_guide():
        return redirect(url_for('stadium_conditions_guide'))

    @app.route("/about")
    def about():
        return render_template("about.html")

    @app.route("/changelog")
    def changelog():
        releases = load_changelog(os.path.join(app.root_path, "changelog.txt"))
        return render_template(
            "changelog.html",
            releases=releases,
            total_changes=sum(len(r["entries"]) for r in releases),
        )

    @app.route("/llm.txt")
    @app.route("/llms.txt")
    def llm_guide():
        return send_from_directory(basedir, "llm.txt", mimetype="text/plain")

    @app.route("/robots.txt")
    def robots_txt():
        return send_from_directory(basedir, "robots.txt", mimetype="text/plain")

    @app.route("/.well-known/security.txt")
    @app.route("/security.txt")
    def security_txt():
        return send_from_directory(basedir, "security.txt", mimetype="text/plain")

    @app.route("/sitemap.xml")
    def sitemap_xml():
        pages = [
            ("https://simcricketx.app/",       "weekly",  "1.0"),
            ("https://simcricketx.app/about",   "monthly", "0.7"),
            ("https://simcricketx.app/changelog", "weekly", "0.5"),
        ]
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for loc, changefreq, priority in pages:
            lines.append(
                f"  <url><loc>{loc}</loc>"
                f"<changefreq>{changefreq}</changefreq>"
                f"<priority>{priority}</priority></url>"
            )
        lines.append("</urlset>")
        return "\n".join(lines), 200, {"Content-Type": "application/xml"}
