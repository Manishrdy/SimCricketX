"""Disposable Git/SQLite fixtures; never call real systemctl, sudo or pip."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest


spec = importlib.util.spec_from_file_location(
    "deploy_offline", Path(__file__).resolve().parents[1] / "scripts/deploy_offline.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


@pytest.fixture
def checkout(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(app), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.name", "Deployment test")
    git("config", "user.email", "deployment-test@example.com")
    (app / ".gitignore").write_text("data/\nvenv/\n*.db*\n.env\n")
    for name in ("migrate.py", "requirements.txt"):
        (app / name).write_text("")
    (app / "version.txt").write_text("3.0.3\n")
    git("add", ".")
    git("commit", "-qm", "previous release")
    previous = git("rev-parse", "HEAD")
    (app / "version.txt").write_text("3.0.4\n")
    git("commit", "-qam", "target release")
    target = git("rev-parse", "HEAD")
    git("reset", "--hard", previous)
    venv = app / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").touch()
    with sqlite3.connect(app / "cricket_sim.db") as conn:
        conn.execute("CREATE TABLE marker(value TEXT)")
        conn.execute("INSERT INTO marker VALUES ('preserve me')")
    (app / "data").mkdir()
    (app / "data/match.json").write_text('{"match_id": "existing"}')
    return app, venv, previous, target


@pytest.fixture
def system(checkout, monkeypatch):
    app, _, _, _ = checkout
    original = deploy.run
    calls = []
    options = {"fail": None, "health": "200", "active": "inactive"}
    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "git":
            return original(*args, **kwargs)
        if options["fail"] and options["fail"] in args:
            raise subprocess.CalledProcessError(1, args)
        output = ""
        if "--property=WorkingDirectory" in args:
            output = str(app)
        elif "--property=ActiveState" in args:
            output = options["active"]
        elif "--property=MainPID" in args:
            output = "0"
        elif args[0] == "curl":
            output = options["health"]
        return subprocess.CompletedProcess(args, 0, output)
    monkeypatch.setattr(deploy, "run", fake)
    monkeypatch.setattr(deploy.time, "sleep", lambda seconds: None)
    return calls, options


def test_default_is_read_only(checkout, system, tmp_path, capsys):
    app, venv, previous, target = checkout
    assert deploy.main(["--app-dir", str(app), "--commit", target]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    calls, _ = system
    assert not any(args[0] in {"sudo", "curl", str(venv / "bin/python")} for args, _ in calls)
    assert deploy.git(app, "rev-parse", "HEAD") == previous
    assert not (tmp_path / "app-rollout-backups").exists()
    assert not (app / ".git/offline-deploy.lock").exists()


@pytest.mark.parametrize("kind", ["tracked", "untracked", "bad_commit"])
def test_refuses_unreviewed_state_before_stopping(checkout, system, kind):
    app, venv, _, target = checkout
    if kind == "tracked":
        (app / "version.txt").write_text("local edit")
    elif kind == "untracked":
        (app / "local_notes.txt").write_text("preserve")
    else:
        target = "main"
    assert deploy.main(["--app-dir", str(app), "--commit", target, "--apply"]) == 1
    assert not any(args[0] == "sudo" for args, _ in system[0])


def test_snapshot_includes_wal_commits_and_private_data(checkout, tmp_path):
    app, _, _, _ = checkout
    # Keep the connection open: the committed row is still in WAL.
    source = sqlite3.connect(app / "cricket_sim.db")
    try:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute("INSERT INTO marker VALUES ('WAL commit')")
        source.commit()
        (app / ".env").write_text("TEST_PLACEHOLDER=value\n")
        backup = tmp_path / "backup"
        backup.mkdir(mode=0o700)
        deploy.snapshot(app, backup, {"previous_commit": "old"})
        with sqlite3.connect(backup / "cricket_sim.db") as conn:
            assert conn.execute("SELECT value FROM marker").fetchall() == [("preserve me",), ("WAL commit",)]
        assert (backup / "data/match.json").read_bytes() == (app / "data/match.json").read_bytes()
        assert (backup / ".env").read_bytes() == (app / ".env").read_bytes()
        assert json.loads((backup / "manifest.json").read_text())["backup_complete"] is True
    finally:
        source.close()


def test_success_stops_before_backup_and_update(checkout, system, tmp_path, monkeypatch):
    app, venv, previous, target = checkout
    calls, _ = system
    original_snapshot = deploy.snapshot
    def snapshot(*args):
        assert any(call == ("sudo", "-n", "systemctl", "stop", "simcricketx") for call, _ in calls)
        assert deploy.git(app, "rev-parse", "HEAD") == previous
        original_snapshot(*args)
    monkeypatch.setattr(deploy, "snapshot", snapshot)
    deploy.apply(app, target, "simcricketx", venv, tmp_path / "backups")
    assert deploy.git(app, "rev-parse", "HEAD") == target
    backup, = (tmp_path / "backups").iterdir()
    assert backup.stat().st_mode & 0o777 == 0o700
    assert (backup / "cricket_sim.db").exists()
    commands = [args for args, _ in calls]
    migration = next(i for i, args in enumerate(commands) if "migrate.py" in args)
    start = commands.index(("sudo", "-n", "systemctl", "start", "simcricketx"))
    assert migration < start
    env = calls[migration][1]["env"]
    assert env["SIMCRICKETX_TEST_DB_URI"] == ""
    assert env["SIMCRICKETX_PRECHECK_RUNNING"] == "0"
    assert env["SIMCRICKETX_SKIP_GLOBAL_APP"] == "1"
    assert env["SIMCRICKETX_TEST_MODE"] == "1"


@pytest.mark.parametrize("fail", ["install", "migrate.py", "start", "health"])
def test_failure_stops_service_retains_backup_and_never_restores_data(checkout, system, tmp_path, fail):
    app, venv, _, target = checkout
    calls, options = system
    if fail == "health":
        options["health"] = "503"
    else:
        options["fail"] = fail
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        deploy.apply(app, target, "simcricketx", venv, tmp_path / "backups")
    assert calls[-1][0] == ("sudo", "-n", "systemctl", "stop", "simcricketx")
    backup, = (tmp_path / "backups").iterdir()
    assert json.loads((backup / "manifest.json").read_text())["backup_complete"]
    assert deploy.git(app, "rev-parse", "HEAD") == target
    if fail in {"install", "migrate.py"}:
        assert not any("start" in args for args, _ in calls)


def test_unstopped_service_prevents_backup_and_code_update(checkout, system, tmp_path):
    app, venv, previous, target = checkout
    system[1]["active"] = "active"
    with pytest.raises(RuntimeError, match="did not fully stop"):
        deploy.apply(app, target, "simcricketx", venv, tmp_path / "backups")
    assert deploy.git(app, "rev-parse", "HEAD") == previous
    backup, = (tmp_path / "backups").iterdir()
    assert not (backup / "cricket_sim.db").exists()


def test_backup_failure_never_updates_or_starts(checkout, system, tmp_path, monkeypatch):
    app, venv, previous, target = checkout
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(deploy, "snapshot", fail)
    with pytest.raises(OSError, match="disk full"):
        deploy.apply(app, target, "simcricketx", venv, tmp_path / "backups")
    assert deploy.git(app, "rev-parse", "HEAD") == previous
    assert not any("start" in args for args, _ in system[0])


def test_rejects_nested_backups_and_data_symlinks(checkout, system, tmp_path):
    app, venv, _, target = checkout
    with pytest.raises(ValueError, match="outside"):
        deploy.apply(app, target, "simcricketx", venv, app / "backups")
    (app / "data/link").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="Symlinks"):
        deploy.preflight(app, target, "simcricketx", venv)
    assert not any(args[0] == "sudo" for args, _ in system[0])


def test_parallel_offline_deployments_are_blocked(checkout):
    app, _, _, _ = checkout
    with deploy.deployment_lock(app):
        with pytest.raises(RuntimeError, match="Another offline deployment"):
            with deploy.deployment_lock(app):
                pytest.fail("second deployment acquired lock")
