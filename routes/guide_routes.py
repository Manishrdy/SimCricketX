"""Onboarding guide: template context + progress API.

All state rules live in utils/guide_state.py; this module only wires them to
requests. The front end is static/js/scx_guide.js (engine) and
static/js/scx_guide_tours.js (tour content).
"""

from __future__ import annotations

from flask import jsonify, request, session
from flask_login import current_user, login_required

from database import db
from database.models import Match, Team
from utils import guide_state as gs
from utils.exception_tracker import log_exception


def _user_key():
    return f"user:{current_user.get_id()}" if current_user.is_authenticated else request.remote_addr


def _counts(user_id: str) -> dict:
    teams = db.session.query(Team.is_draft).filter(
        Team.user_id == user_id,
        Team.is_placeholder.isnot(True),
    ).all()
    published = sum(1 for (is_draft,) in teams if not is_draft)
    matches = db.session.query(Match.id).filter(Match.user_id == user_id).count()
    return {
        "any_teams": len(teams),
        "published": published,
        "drafts": len(teams) - published,
        "matches": matches,
    }


def guide_context(user, endpoint: str | None, path: str) -> dict | None:
    """Everything the client needs to decide which tour (if any) to show on
    this page. ``None`` means the page carries no guide at all."""
    page = gs.GUIDE_PAGES.get(endpoint or "")
    if page is None or path.startswith("/admin"):
        return None

    state = gs.load_state(user.guide_state)
    journey = {"status": state.get("journey")}
    if journey["status"] not in ("dismissed", "done"):
        counts = _counts(user.id)
        journey["status"] = gs.journey_status(
            state, any_teams=counts["any_teams"], matches=counts["matches"]
        )
        if journey["status"] in ("eligible", "active"):
            journey.update(
                stage=gs.journey_stage(counts["published"], counts["matches"]),
                published=counts["published"],
                drafts=counts["drafts"],
            )
    return {
        "page": page,
        "seen": state["seen"],
        "journey": journey,
        # An admin impersonating a user sees the page as the user does, but
        # must not tick off that user's tours.
        "readonly": bool(session.get("impersonating_from")),
    }


def register_guide_routes(app, *, limiter=None):
    def limit(rule):
        if limiter is None:
            return lambda f: f
        return limiter.limit(rule, key_func=_user_key)

    @app.context_processor
    def inject_guide():
        if not current_user.is_authenticated:
            return {"guide": None}
        try:
            return {"guide": guide_context(current_user, request.endpoint, request.path or "")}
        except Exception:
            # The guide is a nicety: a failure here must never take a page down.
            log_exception(source="backend", context={"feature": "guide_context"})
            return {"guide": None}

    def _save(state):
        current_user.guide_state = gs.dump_state(state)
        db.session.commit()
        return jsonify({
            "ok": True,
            "seen": state["seen"],
            "journey": state.get("journey"),
        })

    @app.route("/api/guide/progress", methods=["POST"])
    @login_required
    @limit("120 per minute")
    def guide_progress():
        if session.get("impersonating_from"):
            return jsonify({"error": "Guide progress is read-only while impersonating."}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON object."}), 400

        state = gs.load_state(current_user.guide_state)
        try:
            if "tour" in data:
                gs.mark_tour(state, data.get("tour"), data.get("status"))
            if "journey" in data:
                gs.set_journey(state, data.get("journey"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if "tour" not in data and "journey" not in data:
            return jsonify({"error": "Nothing to update."}), 400
        try:
            return _save(state)
        except Exception:
            db.session.rollback()
            log_exception(source="backend", context={"feature": "guide_progress"})
            return jsonify({"error": "Could not save guide progress."}), 500

    @app.route("/api/guide/reset", methods=["POST"])
    @login_required
    @limit("20 per minute")
    def guide_reset():
        if session.get("impersonating_from"):
            return jsonify({"error": "Guide progress is read-only while impersonating."}), 403
        try:
            return _save(gs.reset_state())
        except Exception:
            db.session.rollback()
            log_exception(source="backend", context={"feature": "guide_reset"})
            return jsonify({"error": "Could not reset the guide."}), 500
