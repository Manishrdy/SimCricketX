import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
import os
import sys

import pytest
import yaml
from werkzeug.security import generate_password_hash

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app, db
from auth.user_auth import update_user_email, validate_password_policy
from database.models import ActiveSession, AdminAuditLog, BlockedIP, FailedLoginAttempt, User


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "app": {"maintenance_mode": False, "secret_key": "test-secret-key-1234567890"},
                "rate_limits": {
                    "max_requests": 30,
                    "window_seconds": 10,
                    "admin_multiplier": 3,
                    "login_limit": "10 per minute",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SIMCRICKETX_CONFIG_PATH", str(cfg_path))
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
    return app, cfg_path


def _create_user(email: str, is_admin: bool = False, display_name: str = "Test User"):
    user = db.session.get(User, email)
    if user is None:
        user = User(
            id=email,
            password_hash=generate_password_hash("TestPass123"),
            display_name=display_name,
            is_admin=is_admin,
            created_at=datetime.now(timezone.utc),
            last_login=datetime.now(timezone.utc),
        )
        db.session.add(user)
    else:
        user.is_admin = is_admin
        user.display_name = display_name
        user.password_hash = generate_password_hash("TestPass123")
    db.session.commit()
    return user


def _login_with_token(client, user_email: str, token: str):
    with client.session_transaction() as sess:
        sess["_user_id"] = user_email
        sess["_fresh"] = True
        sess["session_token"] = token


def test_admin_files_rejects_path_traversal(app_env):
    app, _ = app_env
    admin_email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    token = secrets.token_hex(32)

    with app.app_context():
        _create_user(admin_email, is_admin=True, display_name="Admin")
        db.session.add(
            ActiveSession(
                session_token=token,
                user_id=admin_email,
                ip_address="127.0.0.1",
                user_agent="pytest",
            )
        )
        db.session.commit()

    with app.test_client() as client:
        _login_with_token(client, admin_email, token)
        resp = client.get("/admin/api/files", query_string={"path": "..\\SimCricketX_evil"})
        assert resp.status_code == 403


def test_session_revocation_enforced(app_env):
    app, _ = app_env
    user_email = f"user-{uuid.uuid4().hex[:8]}@example.com"
    token = secrets.token_hex(32)

    with app.app_context():
        _create_user(user_email, is_admin=False, display_name="Player")
        db.session.add(
            ActiveSession(
                session_token=token,
                user_id=user_email,
                ip_address="127.0.0.1",
                user_agent="pytest",
            )
        )
        db.session.commit()

    with app.test_client() as client:
        _login_with_token(client, user_email, token)
        ok_resp = client.get("/", follow_redirects=False)
        assert ok_resp.status_code in (200, 302)

        with app.app_context():
            ActiveSession.query.filter_by(session_token=token).delete()
            db.session.commit()

        revoked_resp = client.get("/", follow_redirects=False)
        assert revoked_resp.status_code == 302
        assert "/login" in revoked_resp.headers.get("Location", "")


def test_admin_block_ip_validation_and_self_lockout_guard(app_env):
    app, _ = app_env
    admin_email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    token = secrets.token_hex(32)

    with app.app_context():
        _create_user(admin_email, is_admin=True, display_name="Admin")
        db.session.add(
            ActiveSession(
                session_token=token,
                user_id=admin_email,
                ip_address="127.0.0.1",
                user_agent="pytest",
            )
        )
        db.session.commit()

    with app.test_client() as client:
        _login_with_token(client, admin_email, token)
        invalid = client.post(
            "/admin/ip-blocklist/add",
            data={"ip_address": "not-an-ip", "reason": "test"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert invalid.status_code == 400

        self_block = client.post(
            "/admin/ip-blocklist/add",
            data={"ip_address": "127.0.0.1", "reason": "test"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert self_block.status_code == 400


def test_admin_config_update_allowlist(app_env):
    app, cfg_path = app_env
    admin_email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    token = secrets.token_hex(32)

    with app.app_context():
        _create_user(admin_email, is_admin=True, display_name="Admin")
        db.session.add(
            ActiveSession(
                session_token=token,
                user_id=admin_email,
                ip_address="127.0.0.1",
                user_agent="pytest",
            )
        )
        db.session.commit()

    with app.test_client() as client:
        _login_with_token(client, admin_email, token)

        forbidden_secret = client.post(
            "/admin/config/update",
            data={"section": "app", "key": "secret_key", "value": "new-secret"},
        )
        assert forbidden_secret.status_code == 403

        unknown = client.post(
            "/admin/config/update",
            data={"section": "app", "key": "unknown_field", "value": "x"},
        )
        assert unknown.status_code == 400

        valid = client.post(
            "/admin/config/update",
            data={"section": "rate_limits", "key": "max_requests", "value": "45"},
        )
        assert valid.status_code == 200

    written = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert written["rate_limits"]["max_requests"] == 45


def test_admin_file_delete_writes_audit_log(app_env):
    app, _ = app_env
    admin_email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    token = secrets.token_hex(32)
    rel_path = f"data/admin_test_delete_{uuid.uuid4().hex[:8]}.txt"

    with app.app_context():
        _create_user(admin_email, is_admin=True, display_name="Admin")
        db.session.add(
            ActiveSession(
                session_token=token,
                user_id=admin_email,
                ip_address="127.0.0.1",
                user_agent="pytest",
            )
        )
        db.session.commit()

    abs_path = app.root_path + "/" + rel_path.replace("\\", "/")
    path_obj = Path(abs_path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    path_obj.write_text("delete me", encoding="utf-8")

    with app.test_client() as client:
        _login_with_token(client, admin_email, token)
        resp = client.delete("/admin/api/files", query_string={"path": rel_path})
        assert resp.status_code == 200

    with app.app_context():
        entry = (
            AdminAuditLog.query.filter_by(admin_email=admin_email, action="delete_file", target=rel_path)
            .order_by(AdminAuditLog.id.desc())
            .first()
        )
        assert entry is not None


def test_password_policy_and_user_email_reference_updates(app_env):
    app, _ = app_env

    ok, _ = validate_password_policy("GoodPass123")
    assert ok
    bad, _ = validate_password_policy("short")
    assert not bad

    old_email = f"user-{uuid.uuid4().hex[:8]}@example.com"
    new_email = f"user-{uuid.uuid4().hex[:8]}@example.com"
    session_token = secrets.token_hex(32)
    block_ip = f"198.51.100.{int(uuid.uuid4().hex[:2], 16)}"

    with app.app_context():
        _create_user(old_email, is_admin=False, display_name="Mover")
        db.session.add(
            ActiveSession(
                session_token=session_token,
                user_id=old_email,
                ip_address="198.51.100.1",
                user_agent="pytest",
            )
        )
        db.session.add(BlockedIP(ip_address=block_ip, reason="test", blocked_by=old_email))
        db.session.add(FailedLoginAttempt(email=old_email, ip_address="203.0.113.10", user_agent="pytest"))
        db.session.commit()

        success, _ = update_user_email(old_email, new_email, admin_email="audit-admin@example.com")
        assert success
        assert db.session.get(User, new_email) is not None
        assert ActiveSession.query.filter_by(user_id=new_email).count() >= 1
        assert BlockedIP.query.filter_by(blocked_by=new_email).count() >= 1
        assert FailedLoginAttempt.query.filter_by(email=new_email).count() >= 1
