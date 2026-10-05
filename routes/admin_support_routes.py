"""Admin analytics for the Buy Me a Coffee prompt.

Reads support_prompt_events (one row per shown / closed / clicked / optout)
so the admin can see who clicked and how interaction trends per day.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from flask import jsonify, render_template, request
from flask_login import login_required
from sqlalchemy import func

from auth.decorators import admin_required
from database import db
from database.models import SupportPromptEvent, User
from utils.support_prompt import EVENTS, SOURCES

RANGES = (7, 30, 90, 0)  # days; 0 = all time
DEFAULT_RANGE = 30
PER_PAGE = 50


def _range_start(days: int, now: datetime):
    return now - timedelta(days=days) if days else None


def _scoped(query, start):
    return query.filter(SupportPromptEvent.created_at >= start) if start else query


def support_stats(days: int = DEFAULT_RANGE, now: datetime | None = None) -> dict:
    """Totals, per-source split and per-day counts (UTC dates) for the window."""
    start = _range_start(days, now or datetime.utcnow())
    E = SupportPromptEvent

    totals = {e: {"events": 0, "users": 0} for e in EVENTS}
    for event, n, users in _scoped(
        db.session.query(E.event, func.count(E.id), func.count(func.distinct(E.user_id))), start
    ).group_by(E.event):
        if event in totals:
            totals[event] = {"events": n, "users": users}

    by_source = {s: {e: 0 for e in EVENTS} for s in SOURCES}
    for source, event, n in _scoped(
        db.session.query(E.source, E.event, func.count(E.id)), start
    ).group_by(E.source, E.event):
        if source in by_source and event in by_source[source]:
            by_source[source][event] = n

    daily = {}
    for day, event, n in _scoped(
        db.session.query(func.date(E.created_at), E.event, func.count(E.id)), start
    ).group_by(func.date(E.created_at), E.event):
        daily.setdefault(str(day), {e: 0 for e in EVENTS})[event] = n
    daily_rows = [{"date": d, **daily[d]} for d in sorted(daily, reverse=True)]

    shown_users = totals["shown"]["users"]
    return {
        "days": days,
        "totals": totals,
        "by_source": by_source,
        "daily": daily_rows,
        # Share of people who were shown the popup that went on to click.
        # Clicks from the post-match note have no "shown" row, so this is a
        # ratio of unique users, not events, and can exceed what the popup alone earned.
        "click_rate": round(100 * totals["clicked"]["users"] / shown_users, 1) if shown_users else None,
    }


def register_admin_support_routes(app, *, db=db):

    def _days_arg():
        days = request.args.get("days", DEFAULT_RANGE, type=int)
        return days if days in RANGES else DEFAULT_RANGE

    @app.route("/admin/support-prompt")
    @login_required
    @admin_required
    def admin_support_prompt():
        days = _days_arg()
        event_filter = request.args.get("event", "")
        if event_filter not in EVENTS:
            event_filter = ""
        page = max(request.args.get("page", 1, type=int), 1)

        q = (
            db.session.query(SupportPromptEvent, User.display_name)
            .join(User, User.id == SupportPromptEvent.user_id)
            .order_by(SupportPromptEvent.created_at.desc(), SupportPromptEvent.id.desc())
        )
        q = _scoped(q, _range_start(days, datetime.utcnow()))
        if event_filter:
            q = q.filter(SupportPromptEvent.event == event_filter)
        total = q.count()
        rows = q.offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()

        return render_template(
            "admin/support_prompt.html",
            stats=support_stats(days),
            rows=rows,
            days=days,
            ranges=RANGES,
            event_filter=event_filter,
            events=EVENTS,
            page=page,
            total=total,
            total_pages=max((total + PER_PAGE - 1) // PER_PAGE, 1),
        )

    @app.route("/api/admin/support-prompt/stats")
    @login_required
    @admin_required
    def admin_support_prompt_stats():
        return jsonify(support_stats(_days_arg()))
