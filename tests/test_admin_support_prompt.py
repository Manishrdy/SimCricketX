"""Admin analytics for the support prompt: event history, aggregation, access."""

from datetime import datetime, timedelta

from database import db
from database.models import SupportPromptEvent, User
from routes.admin_support_routes import support_stats

NOW = datetime(2026, 10, 5, 12, 0, 0)


def _add(user_id, event, when, source="popup"):
    db.session.add(SupportPromptEvent(user_id=user_id, event=event, source=source, created_at=when))


def _seed(regular_user):
    uid = regular_user.id
    _add(uid, "shown", NOW - timedelta(days=1))
    _add(uid, "clicked", NOW - timedelta(days=1, hours=-1))
    _add(uid, "shown", NOW - timedelta(days=2))
    _add(uid, "closed", NOW - timedelta(days=2))
    _add(uid, "clicked", NOW - timedelta(hours=1), source="postmatch")
    _add(uid, "optout", NOW - timedelta(days=40))
    db.session.commit()


def test_stats_group_by_day_source_and_event(app, regular_user):
    _seed(regular_user)
    stats = support_stats(30, now=NOW)

    assert stats["totals"]["shown"] == {"events": 2, "users": 1}
    assert stats["totals"]["clicked"] == {"events": 2, "users": 1}
    assert stats["totals"]["closed"]["events"] == 1
    assert stats["totals"]["optout"]["events"] == 0  # 40 days ago, outside the window
    assert stats["by_source"]["postmatch"]["clicked"] == 1
    assert stats["by_source"]["popup"]["clicked"] == 1

    days = {row["date"]: row for row in stats["daily"]}
    assert days["2026-10-04"]["shown"] == 1 and days["2026-10-04"]["clicked"] == 1
    assert days["2026-10-03"]["closed"] == 1
    assert [r["date"] for r in stats["daily"]] == sorted(days, reverse=True)
    assert stats["click_rate"] == 100.0


def test_all_time_range_includes_old_events(app, regular_user):
    _seed(regular_user)
    assert support_stats(0, now=NOW)["totals"]["optout"]["events"] == 1


def test_empty_stats_have_no_click_rate(app):
    stats = support_stats(30, now=NOW)
    assert stats["click_rate"] is None and stats["daily"] == []


def test_admin_page_lists_who_clicked(admin_client, regular_user):
    _seed(regular_user)
    # seeded relative to a fixed NOW; use all-time so the page window cannot miss them
    body = admin_client.get("/admin/support-prompt?days=0&event=clicked").get_data(as_text=True)
    assert regular_user.id in body
    assert "Buy me a coffee" in body


def test_admin_json_endpoint(admin_client, regular_user):
    _seed(regular_user)
    data = admin_client.get("/api/admin/support-prompt/stats?days=0").get_json()
    assert data["totals"]["clicked"]["events"] == 2
    assert data["days"] == 0


def test_bad_filters_fall_back_to_defaults(admin_client):
    assert admin_client.get("/admin/support-prompt?days=abc&event=nope&page=-3").status_code == 200
    assert admin_client.get("/api/admin/support-prompt/stats?days=999").get_json()["days"] == 30


def test_non_admin_is_forbidden(authenticated_client):
    assert authenticated_client.get("/admin/support-prompt").status_code == 403
    assert authenticated_client.get("/api/admin/support-prompt/stats").status_code == 403


def test_anonymous_is_redirected(client):
    assert client.get("/admin/support-prompt").status_code == 302


def test_event_endpoint_logs_history_with_source(authenticated_client, regular_user):
    post = authenticated_client.post
    assert post("/support-prompt/event", json={"event": "shown", "source": "popup"}).status_code == 200
    assert post("/support-prompt/event", json={"event": "clicked", "source": "postmatch"}).status_code == 200
    assert post("/support-prompt/event", json={"event": "closed", "source": "weird"}).status_code == 200
    assert post("/support-prompt/event", json={"event": "bogus"}).status_code == 400

    db.session.expire_all()
    rows = SupportPromptEvent.query.filter_by(user_id=regular_user.id).order_by(SupportPromptEvent.id).all()
    assert [(r.event, r.source) for r in rows] == [
        ("shown", "popup"), ("clicked", "postmatch"), ("closed", "popup")]  # unknown source -> popup


def test_deleting_a_user_removes_their_events(app, regular_user):
    _seed(regular_user)
    db.session.execute(db.text("PRAGMA foreign_keys=ON"))
    db.session.delete(db.session.get(User, regular_user.id))
    db.session.commit()
    assert SupportPromptEvent.query.count() == 0
