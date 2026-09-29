"""
Which code revision each running app process booted from — and the guard
that stops a destructive migration while an older process is still serving.

A "contract" migration (dropping a table or column the code used to read)
is only safe once no running process can still query the old schema. On
the OCI box the deploy is manual — git pull, run the migration, restart —
so the old gunicorn worker keeps serving the previous revision from memory
until the restart. That is how GitHub issue #195 happened: the support chat
code was pulled away on disk, `python -m migrations.community_board --apply`
dropped support_conversation, and the never-restarted worker (still running
the pre-community-board code) answered the admin sidebar's unread-count poll
with "no such table: support_conversation". The traceback had no source
lines because the .py files it was executing were already gone from disk.

The fix is ordering, enforced rather than documented:

  * create_app() calls record_boot(), which writes data/runtime/app-<pid>.json
    naming the git revision that process loaded.
  * assert_no_stale_holders() finds every other process that has the SQLite
    file open (psutil) and refuses unless each one booted from the revision
    that is checked out now. A process with no boot record — including any
    server started before this module existed — counts as stale.

So the only order that works is the safe one: deploy, restart, then drop.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
RUNTIME_DIR = os.path.join(PROJECT_ROOT, "data", "runtime")

# psutil reports create_time as a float; a pid reused by a new process gets
# a different start time, so a marker only counts if the two agree.
_CREATE_TIME_TOLERANCE_SECS = 1.0


class StaleServerError(RuntimeError):
    """A process holding the database is not running the checked-out code."""


def code_revision(root: str = PROJECT_ROOT) -> str | None:
    """The checked-out commit, or None when it cannot be determined."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _marker_path(pid: int) -> str:
    return os.path.join(RUNTIME_DIR, f"app-{pid}.json")


def _read_marker(pid: int) -> dict | None:
    try:
        with open(_marker_path(pid), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _prune_dead_markers(psutil) -> None:
    try:
        names = os.listdir(RUNTIME_DIR)
    except OSError:
        return
    for name in names:
        if not (name.startswith("app-") and name.endswith(".json")):
            continue
        try:
            pid = int(name[4:-5])
        except ValueError:
            continue
        if not psutil.pid_exists(pid):
            try:
                os.remove(os.path.join(RUNTIME_DIR, name))
            except OSError:
                pass


def record_boot(db_path: str | None) -> dict | None:
    """Record this process's code revision. Never raises: a failure here
    must not stop the app booting — it only means the guard will treat this
    process as stale and ask for a restart/stop before a destructive step."""
    try:
        import psutil

        os.makedirs(RUNTIME_DIR, exist_ok=True)
        _prune_dead_markers(psutil)
        pid = os.getpid()
        marker = {
            "pid": pid,
            "create_time": psutil.Process(pid).create_time(),
            "revision": code_revision(),
            "db_path": os.path.realpath(db_path) if db_path else None,
            "booted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp = _marker_path(pid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(marker, fh)
        os.replace(tmp, _marker_path(pid))
        return marker
    except Exception as exc:  # noqa: BLE001 - best effort by design
        print(f"[WARN] runtime_registry: could not record boot revision: {exc}")
        return None


def processes_holding(db_path: str) -> list:
    """Other processes that have db_path open (psutil.Process objects).

    Processes we are not allowed to inspect are skipped: in production the
    app and the operator running migrations are the same user."""
    import psutil

    target = os.path.realpath(db_path)
    me = os.getpid()
    holders = []
    for proc in psutil.process_iter(["pid"]):
        if proc.pid == me:
            continue
        try:
            if any(os.path.realpath(f.path) == target for f in proc.open_files()):
                holders.append(proc)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            continue
    return holders


def stale_holders(db_path: str) -> list[dict]:
    """Holders of db_path that did not boot from the checked-out revision."""
    import psutil

    current = code_revision()
    stale = []
    for proc in processes_holding(db_path):
        try:
            cmdline = " ".join(proc.cmdline()) or proc.name()
            create_time = proc.create_time()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        marker = _read_marker(proc.pid)
        if marker is None or abs(marker.get("create_time", 0) - create_time) > _CREATE_TIME_TOLERANCE_SECS:
            reason = "no boot record (started before the current code, or not an app process)"
        elif current is None:
            reason = "cannot read the checked-out git revision to compare"
        elif marker.get("revision") != current:
            reason = f"booted from {str(marker.get('revision'))[:10]}, checkout is {current[:10]}"
        else:
            continue
        stale.append({"pid": proc.pid, "cmdline": cmdline[:160], "reason": reason})
    return stale


def describe_stale(stale: list[dict], db_path: str, action: str) -> str:
    lines = [
        f"Refusing to {action}: {len(stale)} process(es) still have "
        f"{os.path.basename(db_path)} open and are not running the checked-out code.",
    ]
    for s in stale:
        lines.append(f"  pid {s['pid']}: {s['reason']}\n      {s['cmdline']}")
    lines.append(
        "A process running older code can still query what this step removes. "
        "Restart the app first (sudo systemctl restart simcricketx), or stop the "
        "listed processes, then re-run this command."
    )
    return "\n".join(lines)


def assert_no_stale_holders(db_path: str | None, action: str) -> None:
    """Raise StaleServerError if any other process holding db_path is stale.

    An in-memory database (db_path None / ':memory:') cannot be shared, so
    there is nothing to check."""
    if not db_path or db_path == ":memory:":
        return
    stale = stale_holders(db_path)
    if stale:
        raise StaleServerError(describe_stale(stale, db_path, action))
