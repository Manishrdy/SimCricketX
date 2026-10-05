"""Support (Buy Me a Coffee) prompt: cooldown rules, migration, endpoint, and
that the dashboard / post-match surfaces render only for users who may be asked."""

import contextlib
import types
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash

from database import db
from database.models import ActiveSession, Match, User
from utils import support_prompt as sp

NOW = datetime(2026, 10, 5, 12, 0, 0)


def _user(**kw):
    base = dict(support_prompt_shown_at=None, support_prompt_dismissed_at=None,
                support_prompt_clicked_at=None, support_prompt_optout_at=None, is_admin=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


# ── Pure rules ───────────────────────────────────────────────────────────────

def test_never_asked_with_enough_matches_is_due():
    assert sp.should_show_popup(_user(), 5, NOW)


def test_not_asked_before_the_match_floor():
    assert not sp.should_show_popup(_user(), sp.MIN_MATCHES - 1, NOW)
    assert not sp.should_show_postmatch_note(_user(), sp.MIN_MATCHES - 1, NOW)


def test_admins_are_never_asked():
    assert not sp.should_show_popup(_user(is_admin=True), 50, NOW)
    assert not sp.should_show_postmatch_note(_user(is_admin=True), 50, NOW)


def test_popup_cooldown_is_fifteen_days_from_shown():
    assert not sp.should_show_popup(_user(support_prompt_shown_at=NOW - timedelta(days=14, hours=23)), 9, NOW)
    assert sp.should_show_popup(_user(support_prompt_shown_at=NOW - timedelta(days=15, minutes=1)), 9, NOW)


def test_click_silences_for_ninety_days_on_every_surface():
    clicked = _user(support_prompt_clicked_at=NOW - timedelta(days=89),
                    support_prompt_shown_at=NOW - timedelta(days=89))
    assert not sp.should_show_popup(clicked, 9, NOW)
    assert not sp.should_show_postmatch_note(clicked, 9, NOW)
    later = _user(support_prompt_clicked_at=NOW - timedelta(days=91),
                  support_prompt_shown_at=NOW - timedelta(days=91))
    assert sp.should_show_popup(later, 9, NOW)
    assert sp.should_show_postmatch_note(later, 9, NOW)


def test_optout_is_permanent():
    gone = _user(support_prompt_optout_at=NOW - timedelta(days=1000))
    assert not sp.should_show_popup(gone, 99, NOW)
    assert not sp.should_show_postmatch_note(gone, 99, NOW)


def test_postmatch_note_ignores_the_popup_cooldown():
    assert sp.should_show_postmatch_note(_user(support_prompt_shown_at=NOW - timedelta(days=1)), 9, NOW)


def test_record_event_stamps_the_right_columns():
    u = _user()
    assert sp.record_event(u, "shown", NOW)
    assert u.support_prompt_shown_at == NOW and u.support_prompt_dismissed_at is None
    later = NOW + timedelta(seconds=30)
    assert sp.record_event(u, "closed", later)
    assert u.support_prompt_dismissed_at == later and u.support_prompt_shown_at == NOW
    assert sp.record_event(u, "clicked", later)
    assert u.support_prompt_clicked_at == later
    assert sp.record_event(u, "optout", later)
    assert u.support_prompt_optout_at == later


def test_record_event_rejects_unknown_events():
    u = _user()
    assert not sp.record_event(u, "explode", NOW)
    assert u.support_prompt_shown_at is None


# ── Migration ────────────────────────────────────────────────────────────────

def test_migration_adds_columns_once(tmp_path):
    from migrations.add_user_support_prompt import COLUMNS, run_migration

    engine = create_engine(f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id TEXT PRIMARY KEY)"))
        conn.execute(text("INSERT INTO users (id) VALUES ('a@b.c')"))
    fake_db = types.SimpleNamespace(engine=engine)
    fake_app = types.SimpleNamespace(app_context=lambda: contextlib.nullcontext())

    run_migration(fake_db, fake_app)
    run_migration(fake_db, fake_app)  # idempotent

    with engine.connect() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(users)")).fetchall()]
        row = conn.execute(text(f"SELECT {', '.join(COLUMNS)} FROM users")).fetchone()
    assert all(c in cols for c in COLUMNS)
    assert row == (None, None, None, None)  # no backfill: existing users read as "never asked"


# ── Routes ───────────────────────────────────────────────────────────────────

def _make_user(email="coffee@example.com", **kw):
    user = User(id=email, password_hash=generate_password_hash("Password123!"), display_name="Coffee",
                is_admin=False, is_banned=False, force_password_reset=False, email_verified=True,
                created_at=datetime.utcnow(), **kw)
    db.session.add(user)
    db.session.commit()
    return user


def _login(app, user):
    client = app.test_client()
    token = f"tok-{user.id}"
    db.session.add(ActiveSession(session_token=token, user_id=user.id, ip_address="127.0.0.1",
                                 user_agent="pytest", login_at=datetime.utcnow(),
                                 last_active=datetime.utcnow()))
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = user.id
        sess["_fresh"] = True
        sess["session_token"] = token
    return client


def _play(user, n):
    for _ in range(n):
        db.session.add(Match(id=str(uuid.uuid4()), user_id=user.id))
    db.session.commit()


def _fresh(user):
    db.session.expire_all()
    return db.session.get(User, user.id)


def test_popup_markup_only_for_eligible_users(app):
    veteran = _make_user("vet@example.com")
    _play(veteran, 5)
    body = _login(app, veteran).get("/").get_data(as_text=True)
    assert 'id="support-backdrop"' in body
    assert "https://buymeacoffee.com/manishrdy" in body

    rookie = _make_user("rookie@example.com")
    _play(rookie, 2)
    assert 'id="support-backdrop"' not in _login(app, rookie).get("/").get_data(as_text=True)


def test_popup_is_not_rendered_inside_cooldown(app):
    user = _make_user("cool@example.com", support_prompt_shown_at=datetime.utcnow() - timedelta(days=3))
    _play(user, 8)
    assert 'id="support-backdrop"' not in _login(app, user).get("/").get_data(as_text=True)


def test_rendering_the_page_does_not_stamp_shown(app):
    """Only the client reports 'shown', so a popup suppressed by a tour or the
    changelog dialog does not burn the user's 15 days."""
    user = _make_user("quiet@example.com")
    _play(user, 6)
    _login(app, user).get("/")
    assert _fresh(user).support_prompt_shown_at is None


def test_event_endpoint_records_and_validates(app):
    user = _make_user("events@example.com")
    client = _login(app, user)

    assert client.post("/support-prompt/event", json={"event": "shown"}).status_code == 200
    assert _fresh(user).support_prompt_shown_at is not None

    assert client.post("/support-prompt/event", json={"event": "optout"}).status_code == 200
    assert _fresh(user).support_prompt_optout_at is not None

    assert client.post("/support-prompt/event", json={"event": "nope"}).status_code == 400
    assert client.post("/support-prompt/event", data="not json").status_code == 400


def test_event_endpoint_requires_login(client):
    res = client.post("/support-prompt/event", json={"event": "shown"})
    assert res.status_code in (302, 401)


def test_about_page_links_to_support_for_visitors(client):
    res = client.get("/about")
    assert res.status_code == 200
    assert "https://buymeacoffee.com/manishrdy" in res.get_data(as_text=True)


def test_dashboard_footer_links_to_support(app):
    user = _make_user("footer@example.com")
    body = _login(app, user).get("/").get_data(as_text=True)
    assert "Buy me a coffee" in body
    assert "https://buymeacoffee.com/manishrdy" in body


# ── Post-match note on the live match page ───────────────────────────────────

def _render_match_page(app, client, user_id):
    """Write a minimal live FC match to disk and fetch /match/<id>."""
    import copy
    import json
    import os

    import app as app_module
    from tests.test_fc_format import _squad

    match_id = str(uuid.uuid4())
    data = {
        "match_id": match_id, "created_by": user_id, "timestamp": "2026-10-05T12:00:00",
        "team_home": "HOM_1", "team_away": "AWY_1", "stadium": "Test Ground", "pitch": "Hard",
        "toss": "Heads", "toss_winner": "HOM", "toss_decision": "Bat", "match_format": "FC",
        "days": 5, "simulation_mode": "auto", "rain_probability": 0.0,
        "playing_xi": {"home": copy.deepcopy(_squad("HOM")), "away": copy.deepcopy(_squad("AWY"))},
        "substitutes": {"home": [], "away": []}, "weather_forecast": "clear",
    }
    match_dir = os.path.join(app_module.PROJECT_ROOT, "data", "matches")
    os.makedirs(match_dir, exist_ok=True)
    path = os.path.join(match_dir, f"match_{match_id}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    try:
        res = client.get(f"/match/{match_id}")
        assert res.status_code == 200
        return res.get_data(as_text=True)
    finally:
        os.remove(path)


def test_postmatch_note_rendered_for_an_experienced_user(app):
    user = _make_user("noteyes@example.com")
    _play(user, 5)
    assert 'id="support-note-bar"' in _render_match_page(app, _login(app, user), user.id)


def test_postmatch_note_absent_before_the_match_floor(app):
    user = _make_user("noteno@example.com")
    _play(user, 1)
    assert 'id="support-note-bar"' not in _render_match_page(app, _login(app, user), user.id)


def test_postmatch_note_absent_after_optout(app):
    user = _make_user("noteout@example.com", support_prompt_optout_at=datetime.utcnow())
    _play(user, 9)
    assert 'id="support-note-bar"' not in _render_match_page(app, _login(app, user), user.id)
