"""Onboarding guide: state rules, journey stages, API, page wiring, and that
the tour content still points at elements that exist."""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash

from database import db
from database.models import ActiveSession, Match, Team, User
from utils import guide_state as gs

ROOT = Path(__file__).resolve().parent.parent
TOURS_JS = ROOT / "static" / "js" / "scx_guide_tours.js"
CONFIG_RE = re.compile(r'<script id="scx-guide-config" type="application/json">(.*?)</script>', re.S)


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_user(email="guide@example.com", *, admin=False, guide_state=None):
    user = User(id=email, password_hash=generate_password_hash("Password123!"), display_name="Guide Tester",
                is_admin=admin, is_banned=False, force_password_reset=False, email_verified=True,
                created_at=datetime.utcnow(), guide_state=guide_state)
    db.session.add(user)
    db.session.commit()
    return user


class _FreshUserClient:
    """Drop flask-login's per-context user cache before each request (the app
    fixture keeps one app context open for the whole test)."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if name in ("get", "post"):
            def call(*args, **kwargs):
                from flask import g
                g.pop("_login_user", None)
                return attr(*args, **kwargs)
            return call
        return attr


def login(app, user, **extra_session):
    client = app.test_client()
    token = f"tok-{user.id}"
    db.session.add(ActiveSession(session_token=token, user_id=user.id, ip_address="127.0.0.1", user_agent="pytest",
                                 login_at=datetime.utcnow(), last_active=datetime.utcnow()))
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = user.id
        sess["_fresh"] = True
        sess["session_token"] = token
        sess.update(extra_session)
    return _FreshUserClient(client)


def add_team(user, code, *, draft=False, placeholder=False):
    team = Team(name=f"Team {code}", short_code=code, user_id=user.id, is_draft=draft, is_placeholder=placeholder)
    db.session.add(team)
    db.session.commit()
    return team


def add_match(user):
    db.session.add(Match(id=str(uuid.uuid4()), user_id=user.id))
    db.session.commit()


def guide_config(client, path="/"):
    res = client.get(path)
    assert res.status_code == 200, res.status_code
    found = CONFIG_RE.search(res.get_data(as_text=True))
    return json.loads(found.group(1)) if found else None


def stored(user):
    db.session.expire_all()
    return gs.load_state(db.session.get(User, user.id).guide_state)


# ── State rules (pure) ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [None, "", "not json", "[]", "42", '{"seen": "x"}'])
def test_load_state_tolerates_garbage(raw):
    assert gs.load_state(raw) == {"v": 1, "seen": {}}


def test_load_state_drops_invalid_entries():
    raw = json.dumps({"seen": {"home": "done", "Bad-Id": "done", "stats": "maybe", "x" * 60: "done"},
                      "journey": "flying"})
    assert gs.load_state(raw) == {"v": 1, "seen": {"home": "done"}}


def test_done_is_not_downgraded_by_a_later_skip():
    state = gs.mark_tour(gs.load_state(None), "home", "done")
    gs.mark_tour(state, "home", "skipped")
    assert state["seen"]["home"] == "done"
    gs.mark_tour(state, "stats", "skipped")
    gs.mark_tour(state, "stats", "done")
    assert state["seen"]["stats"] == "done"


@pytest.mark.parametrize("tour,status", [("home", "later"), ("../x", "done"), ("", "done"), (None, "done")])
def test_mark_tour_rejects_bad_input(tour, status):
    with pytest.raises(ValueError):
        gs.mark_tour(gs.load_state(None), tour, status)


def test_seen_is_capped():
    state = gs.load_state(None)
    for i in range(gs.MAX_SEEN):
        gs.mark_tour(state, f"t{i}", "done")
    with pytest.raises(ValueError):
        gs.mark_tour(state, "one_more", "done")
    gs.mark_tour(state, "t0", "skipped")  # updating an existing entry is fine


def test_finished_journey_cannot_be_restarted_by_a_stale_tab():
    for final in ("dismissed", "done"):
        state = gs.set_journey(gs.load_state(None), final)
        gs.set_journey(state, "active")
        assert state["journey"] == final


@pytest.mark.parametrize("published,matches,stage", [
    (0, 0, "team1"), (1, 0, "team2"), (2, 0, "match"), (5, 0, "match"), (2, 1, "complete"), (0, 3, "complete"),
])
def test_journey_stage(published, matches, stage):
    assert gs.journey_stage(published, matches) == stage


def test_journey_status_distinguishes_new_and_existing_users():
    empty = gs.load_state(None)
    assert gs.journey_status(empty, any_teams=0, matches=0) == "eligible"
    assert gs.journey_status(empty, any_teams=1, matches=0) == "none"
    assert gs.journey_status(empty, any_teams=0, matches=1) == "none"
    active = gs.set_journey(gs.load_state(None), "active")
    assert gs.journey_status(active, any_teams=3, matches=0) == "active"


# ── Page context ─────────────────────────────────────────────────────────────

def test_new_user_is_offered_the_journey(app):
    client = login(app, make_user())
    cfg = guide_config(client)
    assert cfg["page"] == "home"
    assert cfg["journey"] == {"status": "eligible", "stage": "team1", "published": 0, "drafts": 0}
    assert cfg["seen"] == {}
    assert cfg["readonly"] is False
    assert cfg["rules"]["min"] >= 11


def test_existing_user_gets_page_tours_only(app):
    user = make_user()
    add_team(user, "OLD")
    cfg = guide_config(login(app, user))
    assert cfg["journey"] == {"status": "none"}


def test_journey_stage_follows_real_data(app):
    user = make_user(guide_state=gs.dump_state(gs.set_journey(gs.load_state(None), "active")))
    client = login(app, user)

    add_team(user, "DRF", draft=True)
    add_team(user, "BYE", placeholder=True)
    journey = guide_config(client)["journey"]
    assert (journey["stage"], journey["published"], journey["drafts"]) == ("team1", 0, 1)

    add_team(user, "ONE")
    assert guide_config(client)["journey"]["stage"] == "team2"
    add_team(user, "TWO")
    assert guide_config(client)["journey"]["stage"] == "match"
    add_match(user)
    assert guide_config(client)["journey"]["stage"] == "complete"


def test_dismissed_journey_skips_the_counts(app):
    user = make_user(guide_state=gs.dump_state(gs.set_journey(gs.load_state(None), "dismissed")))
    assert guide_config(login(app, user))["journey"] == {"status": "dismissed"}


def test_guide_is_on_user_pages_and_standalone_manage_teams(app):
    client = login(app, make_user())
    assert guide_config(client, "/teams/manage")["page"] == "manage_teams"
    assert guide_config(client, "/community")["page"] == "community"


def test_guide_is_absent_on_unmapped_and_admin_pages(app):
    client = login(app, make_user())
    assert guide_config(client, "/about") is None
    admin = login(app, make_user("boss@example.com", admin=True))
    assert guide_config(admin, "/admin/dashboard") is None


def test_impersonation_is_read_only(app):
    user = make_user()
    client = login(app, user, impersonating_from="boss@example.com")
    assert guide_config(client)["readonly"] is True
    res = client.post("/api/guide/progress", json={"tour": "home", "status": "done"})
    assert res.status_code == 403
    assert stored(user)["seen"] == {}


# ── API ──────────────────────────────────────────────────────────────────────

def test_progress_saves_tours_and_journey(app):
    user = make_user()
    client = login(app, user)
    res = client.post("/api/guide/progress", json={"tour": "home", "status": "done"})
    assert res.status_code == 200 and res.get_json()["seen"] == {"home": "done"}
    client.post("/api/guide/progress", json={"journey": "active"})

    state = stored(user)
    assert state["seen"] == {"home": "done"}
    assert state["journey"] == "active" and "journey_started" in state
    assert guide_config(client)["seen"] == {"home": "done"}


@pytest.mark.parametrize("body", [
    {"tour": "home", "status": "meh"},
    {"tour": "<script>", "status": "done"},
    {"journey": "restart"},
    {},
    [],
])
def test_progress_rejects_bad_payloads(app, body):
    user = make_user()
    res = login(app, user).post("/api/guide/progress", json=body)
    assert res.status_code == 400
    assert stored(user) == {"v": 1, "seen": {}}


def test_progress_accepts_the_config_csrf_token(app):
    """The engine posts with X-CSRFToken taken from the config island; that
    must satisfy CSRFProtect, and a request without it must not."""
    app.config["WTF_CSRF_ENABLED"] = True
    user = make_user()
    client = login(app, user)
    token = guide_config(client)["csrf"]
    assert client.post("/api/guide/progress", json={"tour": "home", "status": "done"}).status_code == 400
    res = client.post("/api/guide/progress", json={"tour": "home", "status": "done"},
                      headers={"X-CSRFToken": token})
    assert res.status_code == 200
    assert stored(user)["seen"] == {"home": "done"}


def test_progress_requires_login(app):
    res = app.test_client().post("/api/guide/progress", json={"tour": "home", "status": "done"})
    assert res.status_code in (302, 401)


def test_reset_clears_everything(app):
    state = gs.set_journey(gs.mark_tour(gs.load_state(None), "home", "done"), "dismissed")
    user = make_user(guide_state=gs.dump_state(state))
    client = login(app, user)
    assert client.post("/api/guide/reset").status_code == 200
    assert stored(user) == {"v": 1, "seen": {}}
    # No teams, no matches → a reset brings the journey back.
    assert guide_config(client)["journey"]["status"] == "eligible"


# ── Migration ────────────────────────────────────────────────────────────────

def test_migration_adds_column_idempotently(app, tmp_path):
    from migrations.add_user_guide_state import run_migration

    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id VARCHAR(120) PRIMARY KEY, display_name VARCHAR(100))"))
        conn.execute(text("INSERT INTO users (id) VALUES ('old@example.com')"))

    class _DB:
        pass

    fake = _DB()
    fake.engine = engine
    run_migration(fake, app)
    run_migration(fake, app)
    with engine.connect() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(users)"))]
        assert cols.count("guide_state") == 1
        assert conn.execute(text("SELECT guide_state FROM users")).scalar() is None


def test_migration_is_registered():
    from migrations.precheck import MIGRATIONS
    assert "add_user_guide_state" in [name for name, _ in MIGRATIONS]


# ── Tour content still matches the templates ────────────────────────────────

def _template_text():
    return "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "templates").rglob("*.html"))


def test_tour_id_selectors_exist_in_templates():
    js = TOURS_JS.read_text(encoding="utf-8")
    templates = _template_text()
    targets = re.findall(r"(?:target|waitFor): (\[[^\]]*\]|'[^']*')", js)
    ids = set()
    for chunk in targets:
        for selector in re.findall(r"'([^']*)'", chunk):
            ids.update(re.findall(r"#([A-Za-z][\w-]*)", selector))
    assert ids, "no #id selectors found — did the tour file format change?"
    missing = sorted(i for i in ids if f'id="{i}"' not in templates and i != "scxg-journey-mount")
    assert not missing, f"tour selectors point at ids no template defines: {missing}"
    assert 'id="scxg-journey-mount"' in (ROOT / "templates" / "home.html").read_text(encoding="utf-8")


def test_home_card_guide_keys_match_home_links():
    js = TOURS_JS.read_text(encoding="utf-8")
    home = (ROOT / "templates" / "home.html").read_text(encoding="utf-8")
    hrefs = re.findall(r"\('(/[^']+)', '[^']+', '[^']+', '[^']+', '[^']+'\)", home)
    keys = {h.strip("/").replace("/", "-") for h in hrefs}
    used = set(re.findall(r'data-guide="([^"]+)"', js))
    assert used and used <= keys, f"unknown home cards: {sorted(used - keys)}"


def test_every_guide_page_has_a_main_tour():
    js = TOURS_JS.read_text(encoding="utf-8")
    pages_with_main = set(re.findall(r"page: '([a-z_]+)',\s*main: true", js))
    assert set(gs.GUIDE_PAGES.values()) == pages_with_main
