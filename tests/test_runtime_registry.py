"""Destructive migrations must not run while an older app process still holds
the database (GitHub #195: support_conversation was dropped under a
never-restarted worker that was still serving the support API)."""

import json
import os
import sqlite3
import subprocess
import sys

import psutil
import pytest
from sqlalchemy import text

from database import db
from utils import runtime_registry as rr

HOLD_DB = (
    "import sqlite3, sys, time\n"
    "c = sqlite3.connect(sys.argv[1]); c.execute('SELECT 1').fetchall()\n"
    "print('ready', flush=True); time.sleep(120)\n"
)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(rr, "code_revision", lambda root=rr.PROJECT_ROOT: "new-rev")
    return tmp_path / "runtime"


@pytest.fixture
def holder(tmp_path):
    """A separate process with a SQLite file open, like a gunicorn worker."""
    path = tmp_path / "held.db"
    sqlite3.connect(path).close()
    proc = subprocess.Popen([sys.executable, "-c", HOLD_DB, str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        yield proc, str(path)
    finally:
        proc.kill()
        proc.wait()


def write_marker(runtime_dir, pid, revision, create_time=None):
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if create_time is None:
        create_time = psutil.Process(pid).create_time()
    (runtime_dir / f"app-{pid}.json").write_text(json.dumps(
        {"pid": pid, "create_time": create_time, "revision": revision}))


def test_holder_without_boot_record_is_stale(registry, holder):
    proc, path = holder
    stale = rr.stale_holders(path)
    assert [s["pid"] for s in stale] == [proc.pid]
    assert "no boot record" in stale[0]["reason"]
    with pytest.raises(rr.StaleServerError, match="systemctl restart"):
        rr.assert_no_stale_holders(path, "drop things")


def test_holder_on_older_revision_is_stale(registry, holder):
    proc, path = holder
    write_marker(registry, proc.pid, "old-rev")
    assert "booted from old-rev" in rr.stale_holders(path)[0]["reason"]


def test_holder_on_current_revision_is_allowed(registry, holder):
    proc, path = holder
    write_marker(registry, proc.pid, "new-rev")
    assert rr.stale_holders(path) == []
    rr.assert_no_stale_holders(path, "drop things")


def test_reused_pid_does_not_inherit_a_dead_process_marker(registry, holder):
    proc, path = holder
    write_marker(registry, proc.pid, "new-rev", create_time=psutil.Process(proc.pid).create_time() - 3600)
    assert rr.stale_holders(path)[0]["pid"] == proc.pid


def test_unknown_checkout_revision_is_treated_as_stale(registry, holder, monkeypatch):
    proc, path = holder
    write_marker(registry, proc.pid, "new-rev")
    monkeypatch.setattr(rr, "code_revision", lambda root=rr.PROJECT_ROOT: None)
    assert "cannot read" in rr.stale_holders(path)[0]["reason"]


def test_own_process_and_unheld_files_are_ignored(registry, tmp_path):
    path = tmp_path / "mine.db"
    conn = sqlite3.connect(path)
    conn.execute("SELECT 1")
    try:
        assert rr.stale_holders(str(path)) == []
    finally:
        conn.close()
    rr.assert_no_stale_holders(None, "x")
    rr.assert_no_stale_holders(":memory:", "x")


def test_record_boot_writes_marker_and_prunes_dead_ones(registry):
    registry.mkdir(parents=True)
    dead = registry / "app-999999999.json"
    dead.write_text("{}")
    marker = rr.record_boot("/tmp/some.db")
    assert marker["revision"] == "new-rev" and marker["pid"] == os.getpid()
    assert json.loads((registry / f"app-{os.getpid()}.json").read_text())["revision"] == "new-rev"
    assert not dead.exists()


def test_community_drop_refuses_under_a_stale_server_and_changes_nothing(app, registry, monkeypatch, capsys):
    from migrations import community_board

    with db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE support_conversation (id INTEGER PRIMARY KEY)"))
    fake = [{"pid": 4242, "cmdline": "gunicorn: worker [app:create_app()]", "reason": "no boot record"}]
    monkeypatch.setattr(community_board, "stale_holders", lambda path: fake)
    monkeypatch.setattr(rr, "stale_holders", lambda path: fake)

    community_board.run_migration(db, app)  # dry run surfaces the problem early
    assert "will be refused" in capsys.readouterr().out

    with pytest.raises(rr.StaleServerError, match="pid 4242"):
        community_board.run_migration(db, app, apply=True)
    with db.engine.connect() as conn:
        assert conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE name='support_conversation'")).fetchone()

    monkeypatch.setattr(rr, "stale_holders", lambda path: [])
    community_board.run_migration(db, app, apply=True)
    with db.engine.connect() as conn:
        assert not conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE name='support_conversation'")).fetchone()


def test_admin_audit_drop_refuses_under_a_stale_server(registry, holder):
    from migrations.drop_admin_audit_log import migrate

    _, path = holder
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE admin_audit_log (id INTEGER PRIMARY KEY)")
    with pytest.raises(rr.StaleServerError):
        migrate(path, apply=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='admin_audit_log'").fetchone()
    assert migrate(path, apply=False) == 0  # dry run is read-only and never blocked
