#!/usr/bin/env python3
"""Explicit, offline rollout for the existing single-host/systemd deployment.

Dry run by default. This deploys an already implemented and tested commit; it
does not implement the match-store integration. See docs/operations/offline-rollout.md.
Uses the standard library only and never automatically restores production data.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time


def run(*args, cwd=None, capture=False, env=None):
    return subprocess.run(args, cwd=cwd, env=env, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None)


def git(app, *args):
    return run("git", "-C", str(app), *args, capture=True).stdout.strip()


def has_local_changes(app):
    # SQLite sidecars can legitimately exist while the app is running. Never
    # ask an operator to delete them to satisfy the clean-worktree check.
    sidecars = {"?? cricket_sim.db" + suffix for suffix in ("-wal", "-shm", "-journal")}
    return any(line not in sidecars for line in
               git(app, "status", "--porcelain", "--untracked-files=all").splitlines())


def preflight(app, target, service, venv):
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", target):
        raise ValueError("--commit must be a full, locally fetched commit SHA")
    if not re.fullmatch(r"[a-zA-Z0-9_.@-]+", service) or service.startswith("-"):
        raise ValueError("Invalid systemd service name")
    if Path(git(app, "rev-parse", "--show-toplevel")).resolve() != app:
        raise ValueError("--app-dir must be the repository root")
    if has_local_changes(app):
        raise ValueError("Working tree is not clean; preserve local changes before deployment")
    target = git(app, "rev-parse", "--verify", target + "^{commit}")
    for filename in ("migrate.py", "requirements.txt", "version.txt"):
        git(app, "cat-file", "-e", target + ":" + filename)
    if not (venv / "bin/python").is_file():
        raise ValueError("Virtualenv Python missing; set --venv explicitly")
    if not (app / "cricket_sim.db").is_file():
        raise ValueError("Existing cricket_sim.db required; this is not a fresh-install script")
    workdir = run("systemctl", "show", service, "--property=WorkingDirectory", "--value",
                  capture=True).stdout.strip()
    if not workdir or Path(workdir).resolve() != app:
        raise ValueError("Service WorkingDirectory does not match --app-dir")
    # Avoid claiming to back up external files through data-directory symlinks.
    data = app / "data"
    if data.is_symlink() or (data.exists() and not data.is_dir()):
        raise ValueError("data must be a normal directory")
    if data.exists():
        for root, dirs, files in os.walk(data):
            if any((Path(root) / name).is_symlink() for name in dirs + files):
                raise ValueError("Symlinks under data need a deployment-specific backup procedure")
    return {"previous_commit": git(app, "rev-parse", "HEAD"), "target_commit": target,
            "target_version": git(app, "show", target + ":version.txt"),
            "app_dir": str(app), "service": service}


def snapshot(app, backup, metadata):
    """Include committed WAL data using SQLite backup, never raw live DB copy."""
    manifest = backup / "manifest.json"
    manifest.write_text(json.dumps({**metadata, "backup_complete": False}, indent=2) + "\n")
    source_uri = (app / "cricket_sim.db").as_uri() + "?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=30)
    destination = sqlite3.connect(str(backup / "cricket_sim.db"))
    try:
        source.backup(destination)
        if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("Database backup failed integrity check")
    finally:
        destination.close()
        source.close()
    if (app / "data").exists():
        shutil.copytree(app / "data", backup / "data")
    for name in (".env", "config", "requirements.txt", "version.txt"):
        path = app / name
        if path.is_dir():
            shutil.copytree(path, backup / name)
        elif path.is_file():
            shutil.copy2(path, backup / name)
    manifest.write_text(json.dumps({**metadata, "backup_complete": True}, indent=2) + "\n")


@contextmanager
def deployment_lock(app):
    # A stable git-local lock survives checkout/reset; dry runs create no lock.
    directory = Path(git(app, "rev-parse", "--absolute-git-dir"))
    with (directory / "offline-deploy.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another offline deployment is running") from None
        yield


def apply(app, target, service, venv, backup_root):
    with deployment_lock(app):
        metadata = preflight(app, target, service, venv)
        if backup_root == app or app in backup_root.parents:
            raise ValueError("Backups must be outside the application checkout")
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
        backup = Path(tempfile.mkdtemp(prefix=stamp, dir=backup_root))  # mode 0700
        print("Private backup directory: " + str(backup), flush=True)
        # Obtain sudo credentials before stopping; no prompt during failed recovery.
        run("sudo", "-v")
        try:
            run("sudo", "-n", "systemctl", "stop", service)
            state = run("systemctl", "show", service, "--property=ActiveState", "--value",
                        capture=True).stdout.strip()
            pid = run("systemctl", "show", service, "--property=MainPID", "--value",
                      capture=True).stdout.strip()
            if state not in {"inactive", "failed"} or pid != "0":
                raise RuntimeError("Service did not fully stop; code has not been updated")
            print("Service stopped; backing up database, match files, archives and configuration.", flush=True)
            snapshot(app, backup, metadata)
            frozen = run(str(venv / "bin/python"), "-m", "pip", "freeze", capture=True).stdout
            (backup / "pip-freeze.txt").write_text(frozen)
            # Recheck after stopping/backing up: background writers or another
            # deployment must not make this reset discard unseen edits.
            if has_local_changes(app):
                raise RuntimeError("Working tree changed during backup; refusing code update")
            if git(app, "rev-parse", "HEAD") != metadata["previous_commit"]:
                raise RuntimeError("HEAD changed during backup; another deployment may be running")
            run("git", "-C", str(app), "reset", "--hard", metadata["target_commit"])
            run(str(venv / "bin/python"), "-m", "pip", "install", "-r", str(app / "requirements.txt"))
            migration_env = dict(os.environ)
            # Suppress app workers in this one-off process, while keeping
            # production DB selection and the complete schema precheck enabled.
            # These overrides never enter the systemd service environment.
            migration_env.update(SIMCRICKETX_SKIP_GLOBAL_APP="1", SIMCRICKETX_TEST_MODE="1",
                                 SIMCRICKETX_TEST_DB_URI="", SIMCRICKETX_PRECHECK_RUNNING="0",
                                 GITHUB_ISSUE_ON_EXCEPTION_ENABLED="false")
            migration_env.pop("PYTEST_CURRENT_TEST", None)
            migration_env.pop("PYTEST_VERSION", None)
            run(str(venv / "bin/python"), "migrate.py", "--non-interactive", cwd=app, env=migration_env)
            run("sudo", "-n", "systemctl", "start", service)
            for attempt in range(5):
                try:
                    run("systemctl", "is-active", "--quiet", service)
                    # Direct Gunicorn check; avoid mistaking a proxy error page
                    # or redirect for application readiness.
                    status = run("curl", "--silent", "--show-error", "--max-time", "10",
                                 "--output", "/dev/null", "--write-out", "%{http_code}",
                                 "http://127.0.0.1:5000/", capture=True).stdout.strip()
                    if status == "200":
                        print("Deployment healthy: " + metadata["target_commit"], flush=True)
                        print("Backup retained at " + str(backup), flush=True)
                        return
                except subprocess.CalledProcessError:
                    pass
                if attempt < 4:
                    time.sleep(4)
            raise RuntimeError("Application health check failed")
        except BaseException:
            # Do not restore the old DB automatically: once startup has run,
            # users or jobs may have written new data. Keep evidence/backups.
            try:
                run("sudo", "-n", "systemctl", "stop", service)
                print("Deployment failed; service stopped for recovery.", file=sys.stderr)
            except BaseException:
                print("Could not stop service; check its state immediately.", file=sys.stderr)
            print("No automatic data rollback. Backup: " + str(backup), file=sys.stderr)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, default=Path("/home/ubuntu/SimCricketX"))
    parser.add_argument("--service", default="simcricketx")
    parser.add_argument("--venv", type=Path, help="Default: APP_DIR/venv")
    parser.add_argument("--backup-root", type=Path, help="Default: a sibling SimCricketX-rollout-backups directory")
    parser.add_argument("--commit", required=True, help="Full SHA of the tested release, already fetched locally")
    parser.add_argument("--apply", action="store_true", help="Stop, back up, deploy, migrate, start and health-check")
    args = parser.parse_args(argv)
    app = args.app_dir.resolve()
    venv = (args.venv or app / "venv").resolve()
    backup_root = (args.backup_root or app.parent / (app.name + "-rollout-backups")).resolve()
    try:
        if args.apply:
            apply(app, args.commit, args.service, venv, backup_root)
        else:
            metadata = preflight(app, args.commit, args.service, venv)
            print(json.dumps(metadata, indent=2))
            print("DRY RUN: no service, code, dependency or database changes made.")
            print("Apply will stop → back up → update → migrate → start → check health.")
            print("Backup destination: " + str(backup_root))
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
