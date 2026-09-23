"""Shared QA accounts for local testing (humans and AI agents).

Credentials live in ``.claude/test-accounts.json`` (gitignored, never
committed, never deployed). This script creates that file on first run and
upserts the five accounts into a LOCAL database, resetting them to a known
state (verified, unbanned, unmuted, ages as listed below).

    python scripts/dev_test_accounts.py seed              # repo cricket_sim.db
    python scripts/dev_test_accounts.py seed --db path.db
    python scripts/dev_test_accounts.py list [--show-passwords]

From Python (e.g. a smoke script), get a logged-in Flask test client without
typing a password anywhere:

    from scripts.dev_test_accounts import dev_app, logged_in_client
    app = dev_app()
    with app.app_context():
        alice = logged_in_client(app, "user1")
        alice.post("/api/community/posts", json={...})

See .claude/TEST_ACCOUNTS.md for the full guide. LOCAL / DEV ONLY — never
point this at the production database.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import sys
import uuid
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CREDENTIALS_PATH = os.path.join(ROOT, ".claude", "test-accounts.json")
DEFAULT_DB = os.path.join(ROOT, "cricket_sim.db")

# key -> spec. Emails use example.com (RFC 2606) so no mail is ever delivered.
ACCOUNTS = {
    "admin":  {"email": "qa.admin@example.com",  "display_name": "QA Admin",   "is_admin": True,
               "age_days": 120, "purpose": "Admin: moderation, official answers, announcements, admin pages."},
    "user1":  {"email": "qa.rohit@example.com",  "display_name": "Rohit QA",   "is_admin": False,
               "age_days": 90,  "purpose": "Established user: main author of posts, bugs, private posts."},
    "user2":  {"email": "qa.priya@example.com",  "display_name": "Priya QA",   "is_admin": False,
               "age_days": 60,  "purpose": "Established user: second voice — votes, comments, replies, reports."},
    "user3":  {"email": "qa.arjun@example.com",  "display_name": "Arjun QA",   "is_admin": False,
               "age_days": 45,  "purpose": "Established user: third voice; use for mute/ban/permission tests."},
    "newbie": {"email": "qa.newbie@example.com", "display_name": "Newbie QA",  "is_admin": False,
               "age_days": 0,   "purpose": "Account younger than 24h after every seed: read + vote only."},
}


def _password() -> str:
    # Satisfies the password policy: upper, lower, digit, length.
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "Qa-" + "".join(secrets.choice(alphabet) for _ in range(14))
        if any(c.isupper() for c in pw[3:]) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def load_credentials(create: bool = True) -> dict:
    """Read .claude/test-accounts.json, adding any missing account."""
    data = {}
    if os.path.exists(CREDENTIALS_PATH):
        with open(CREDENTIALS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    accounts = data.setdefault("accounts", {})
    changed = False
    for key, spec in ACCOUNTS.items():
        entry = accounts.get(key)
        if entry is None:
            entry = {"password": _password()}
            changed = True
        entry.update({"email": spec["email"], "display_name": spec["display_name"],
                      "is_admin": spec["is_admin"], "purpose": spec["purpose"]})
        accounts[key] = entry
    data["_note"] = ("Local/dev QA accounts for SimCricketX. Gitignored. Seed with "
                     "`python scripts/dev_test_accounts.py seed`. Never use against production.")
    if (changed or not os.path.exists(CREDENTIALS_PATH)) and create:
        os.makedirs(os.path.dirname(CREDENTIALS_PATH), exist_ok=True)
        with open(CREDENTIALS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.chmod(CREDENTIALS_PATH, 0o600)
    return data


def dev_app(db_path: str | None = None):
    """A create_app() bound to a local DB, with background workers and the
    migration precheck suppressed (run the dev server once to migrate)."""
    os.environ.setdefault("SIMCRICKETX_SKIP_GLOBAL_APP", "1")
    os.environ["SIMCRICKETX_PRECHECK_RUNNING"] = "1"
    os.environ["SIMCRICKETX_TEST_MODE"] = "1"
    os.environ["GITHUB_ISSUE_ON_EXCEPTION_ENABLED"] = "false"
    db_path = os.path.abspath(db_path or DEFAULT_DB)
    if db_path != DEFAULT_DB:
        os.environ["SIMCRICKETX_TEST_DB_URI"] = f"sqlite:///{db_path}"
    from app import create_app
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return app


def seed(app) -> list[str]:
    """Upsert every account and reset it to its known state. Call inside an
    app context."""
    from werkzeug.security import generate_password_hash
    from database import db
    from database.models import User

    creds = load_credentials()["accounts"]
    now = datetime.utcnow()
    done = []
    for key, spec in ACCOUNTS.items():
        user = db.session.get(User, spec["email"])
        if user is None:
            user = User(id=spec["email"], stable_id=str(uuid.uuid4()))
            db.session.add(user)
        user.password_hash = generate_password_hash(creds[key]["password"])
        user.display_name = spec["display_name"]
        user.is_admin = spec["is_admin"]
        user.email_verified = True
        user.is_banned = False
        user.banned_until = None
        user.ban_reason = None
        user.force_password_reset = False
        user.force_email_verify = False
        user.community_muted_until = None
        user.lockout_until = None
        user.lockout_count = 0
        user.created_at = now - timedelta(days=spec["age_days"])
        done.append(f"{key:7s} {spec['email']:24s} {'admin' if spec['is_admin'] else 'user':5s}  {spec['display_name']}")
    db.session.commit()
    return done


class _FreshUserClient:
    """Test client wrapper that drops flask-login's cached user before every
    request, so several logged-in clients can share one app context."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if name in ("get", "post", "patch", "put", "delete", "open"):
            def call(*args, **kwargs):
                from flask import g
                g.pop("_login_user", None)
                return attr(*args, **kwargs)
            return call
        return attr


def logged_in_client(app, key: str):
    """A Flask test client logged in as QA account ``key`` via a real
    ActiveSession row — no password is typed or sent."""
    from database import db
    from database.models import ActiveSession

    email = ACCOUNTS[key]["email"]
    token = f"qa-{key}-{secrets.token_hex(8)}"
    db.session.add(ActiveSession(session_token=token, user_id=email, ip_address="127.0.0.1",
                                 user_agent="qa-test-client", login_at=datetime.utcnow(),
                                 last_active=datetime.utcnow()))
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = email
        sess["_fresh"] = True
        sess["session_token"] = token
    return _FreshUserClient(client)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_seed = sub.add_parser("seed", help="create/reset the QA accounts in a local DB")
    p_seed.add_argument("--db", default=None, help="SQLite path (default: repo cricket_sim.db)")
    p_list = sub.add_parser("list", help="print the accounts")
    p_list.add_argument("--show-passwords", action="store_true")
    args = parser.parse_args()

    if args.cmd == "list":
        for key, entry in load_credentials()["accounts"].items():
            pw = entry["password"] if args.show_passwords else "********"
            print(f"{key:7s} {entry['email']:24s} {pw:20s} {entry['purpose']}")
        return

    app = dev_app(args.db)
    with app.app_context():
        for line in seed(app):
            print("[seeded]", line)
    print(f"Credentials: {os.path.relpath(CREDENTIALS_PATH, ROOT)}")


if __name__ == "__main__":
    main()
