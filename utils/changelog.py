"""Parse changelog.txt into structured release blocks for the public page."""

import os
import re
import threading
from datetime import datetime

from markupsafe import Markup, escape

_HEADER_RE = re.compile(r"^\[(\d+)\.(\d+)\.(\d+)\]\s*(\d{4}-\d{2}-\d{2})?\s*$")
_CODE_RE = re.compile(r"`([^`]+)`")

_cache = {"mtime": None, "path": None, "releases": []}
_lock = threading.Lock()


def render_inline(text):
    """Escape text, then turn `backtick` spans into <code> elements."""
    return Markup(_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", str(escape(text))))


def _release_kind(major, minor, patch):
    if minor == 0 and patch == 0:
        return "major"
    if patch == 0:
        return "minor"
    return "patch"


def parse_changelog(text):
    """Return releases newest-first as dicts: version, date, kind, entries."""
    releases = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        header = _HEADER_RE.match(line)
        if header:
            major, minor, patch = (int(header.group(i)) for i in (1, 2, 3))
            date_str = header.group(4)
            date = datetime.strptime(date_str, "%Y-%m-%d") if date_str else None
            current = {
                "version": f"{major}.{minor}.{patch}",
                "series": f"{major}.{minor}",
                "date_iso": date_str,
                "date_label": f"{date.day} {date:%b %Y}" if date else "",
                "kind": _release_kind(major, minor, patch),
                "entries": [],
            }
            releases.append(current)
        elif current is not None:
            if line.startswith("-"):
                current["entries"].append(render_inline(line[1:].strip()))
            elif current["entries"]:
                # Wrapped continuation of the previous bullet.
                current["entries"][-1] = Markup(f"{current['entries'][-1]} {render_inline(line)}")
    return releases


def load_changelog(path):
    """Parse the changelog file, re-reading only when it changes on disk."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    with _lock:
        if _cache["path"] == path and _cache["mtime"] == mtime:
            return _cache["releases"]
        with open(path, encoding="utf-8") as f:
            releases = parse_changelog(f.read())
        _cache.update(path=path, mtime=mtime, releases=releases)
        return releases
