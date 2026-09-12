#!/usr/bin/env python3
r"""Import a large player-pool file through the admin API, in small batches.

Why this exists
---------------
`POST /admin/player-pool/import` sends the whole file in one request. A
multi-megabyte file (e.g. final_full_member_ratings_2026-08-29.json, ~3 MB /
5,347 players) is rejected with **HTTP 413 Request Entity Too Large** by the
reverse proxy in front of the app, before Flask ever sees it. Raising the proxy
cap is not in the app's hands, so this script splits the file locally and posts
a few hundred rows at a time to:

    POST /api/admin/player-pool/import/chunk

Each batch commits on its own, so an interrupted run keeps everything already
imported, and re-running is safe: existing names are skipped (or updated, with
--mode update).

Validation deliberately lives only on the server (utils of
routes/player_pool_routes.py) — this script does not re-implement the schema
rules, so the two can never drift apart.

Authentication
--------------
The login form is protected by Turnstile and a proof-of-work challenge, so this
script does not log in. Instead, sign in as an admin in a browser and copy the
`session` cookie:

    DevTools → Application → Cookies → <your site> → session

Then pass it with --cookie, --cookie-file, or $SIMCRICKETX_ADMIN_COOKIE. The
script fetches the CSRF token from the import page itself.

Examples
--------
    # Local dev server
    python3 scripts/import_player_pool_chunked.py \
        --file final_full_member_ratings_2026-08-29.json \
        --base-url http://127.0.0.1:7860 \
        --cookie "$SIMCRICKETX_ADMIN_COOKIE"

    # Production, refreshing ratings for names already in the pool
    python3 scripts/import_player_pool_chunked.py \
        --file final_full_member_ratings_2026-08-29.json \
        --base-url https://simcricketx.com \
        --cookie-file ~/.simcricketx-admin-cookie \
        --mode update --batch-size 200 --state /tmp/pool-import.json

    # Show the batch plan without sending anything
    python3 scripts/import_player_pool_chunked.py -f players.json --dry-run

Stdlib only — no virtualenv needed (the repo venv is Windows-only).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

IMPORT_PAGE_PATH = "/admin/player-pool/import"
LIMITS_PATH = "/api/admin/player-pool/import/limits"
CHUNK_PATH = "/api/admin/player-pool/import/chunk"

DEFAULT_BATCH_SIZE = 200
DEFAULT_MAX_BYTES = 512 * 1024          # keep each request well under any proxy cap
DEFAULT_RETRIES = 3
CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')


# ──────────────────────────────────────────────────────────────────────────────
#  HTTP
# ──────────────────────────────────────────────────────────────────────────────

class AdminClient:
    """Minimal session-cookie client for the admin import endpoints."""

    def __init__(self, base_url: str, cookie: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.cookie = _normalize_cookie(cookie)
        self.timeout = timeout
        self.csrf_token = ""
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()),
            _NoRedirect(),
        )

    def _request(self, path, data=None, headers=None):
        url = self.base_url + path
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        req.add_header("Cookie", self.cookie)
        req.add_header("Accept", "application/json, text/html")
        req.add_header("User-Agent", "simcricketx-pool-importer/1.0")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with self.opener.open(req, timeout=self.timeout) as res:
                return res.status, res.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:                      # 4xx / 5xx
            return exc.code, exc.read().decode("utf-8", "replace")

    def authenticate(self):
        """Confirm the cookie belongs to an admin and capture the CSRF token."""
        status, body = self._request(IMPORT_PAGE_PATH)
        if status in (301, 302, 303, 307, 308, 401) or "/login" in body[:400]:
            raise SystemExit(
                "Not signed in: the session cookie is missing, expired, or not an "
                "admin session. Re-copy the `session` cookie from a logged-in "
                "admin browser session."
            )
        if status == 403:
            raise SystemExit("That session is signed in but is not an admin account.")
        if status != 200:
            raise SystemExit(f"Unexpected HTTP {status} fetching {IMPORT_PAGE_PATH}.")
        match = CSRF_RE.search(body)
        if not match:
            raise SystemExit("Could not find a CSRF token on the import page.")
        self.csrf_token = match.group(1)

    def limits(self):
        status, body = self._request(LIMITS_PATH)
        if status != 200:
            return {}
        try:
            return json.loads(body)
        except ValueError:
            return {}

    def send_chunk(self, payload):
        data = json.dumps(payload).encode("utf-8")
        status, body = self._request(CHUNK_PATH, data=data, headers={
            "Content-Type": "application/json",
            "X-CSRFToken": self.csrf_token,
        })
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"error": (body[:200].strip() or f"HTTP {status} with no body")}
        return status, parsed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect to /login means "not authenticated" — surface it, don't follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _normalize_cookie(raw: str) -> str:
    cookie = (raw or "").strip().strip('"').strip("'")
    if not cookie:
        raise SystemExit(
            "No admin cookie supplied. Pass --cookie/--cookie-file or set "
            "$SIMCRICKETX_ADMIN_COOKIE (see --help)."
        )
    if "=" not in cookie:
        cookie = "session=" + cookie
    return cookie


# ──────────────────────────────────────────────────────────────────────────────
#  Batching
# ──────────────────────────────────────────────────────────────────────────────

def load_rows(path):
    with open(path, "r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, list) or not data:
        raise SystemExit(f"{path}: expected a non-empty JSON array of player objects.")
    return data


def plan_batches(rows, batch_size, max_bytes):
    """Split rows into batches capped by both row count and serialized size."""
    batches = []
    index = 0
    while index < len(rows):
        take = min(batch_size, len(rows) - index)
        while take > 1 and len(json.dumps(rows[index:index + take])) > max_bytes:
            take = -(-take // 2)           # ceil-halve
        batches.append((index, rows[index:index + take]))
        index += take
    return batches


# ──────────────────────────────────────────────────────────────────────────────
#  Progress meter
# ──────────────────────────────────────────────────────────────────────────────

class Meter:
    """Single-line progress meter on a TTY; one line per batch otherwise."""

    WIDTH = 28

    def __init__(self, total_rows, total_batches, stream=sys.stderr):
        self.total_rows = total_rows
        self.total_batches = total_batches
        self.stream = stream
        self.tty = stream.isatty()
        self.started = time.monotonic()

    def render(self, rows_done, batches_done, totals):
        elapsed = time.monotonic() - self.started
        frac = (rows_done / self.total_rows) if self.total_rows else 1.0
        filled = int(self.WIDTH * frac)
        bar = "█" * filled + "░" * (self.WIDTH - filled)
        eta = (elapsed / rows_done * (self.total_rows - rows_done)) if rows_done else 0.0
        line = (
            f"[{bar}] {frac * 100:5.1f}% | "
            f"batch {batches_done}/{self.total_batches} | "
            f"rows {rows_done:,}/{self.total_rows:,} | "
            f"+{totals['imported']:,} ~{totals['updated']:,} "
            f"={totals['skipped']:,} !{totals['failed']:,} | "
            f"{_clock(elapsed)} elapsed | ETA {_clock(eta)}"
        )
        if self.tty:
            self.stream.write("\r\033[K" + line)
        else:
            self.stream.write(line + "\n")
        self.stream.flush()

    def done(self):
        if self.tty:
            self.stream.write("\n")
            self.stream.flush()


def _clock(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# ──────────────────────────────────────────────────────────────────────────────
#  State (resume)
# ──────────────────────────────────────────────────────────────────────────────

def read_state(path, source_file):
    if not path or not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return 0
    if state.get("source") != os.path.abspath(source_file):
        return 0
    return int(state.get("rows_done", 0) or 0)


def write_state(path, source_file, rows_done, totals):
    if not path:
        return
    payload = {
        "source": os.path.abspath(source_file),
        "rows_done": rows_done,
        "totals": totals,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    except OSError as exc:
        print(f"\nwarning: could not write state file {path}: {exc}", file=sys.stderr)


# ──────────────────────────────────────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Import a large player-pool JSON file in small batches via the admin API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples\n--------\n", 1)[-1],
    )
    parser.add_argument("-f", "--file", required=True, help="JSON array of player objects.")
    parser.add_argument("--base-url", default=os.getenv("SIMCRICKETX_BASE_URL", "http://127.0.0.1:7860"),
                        help="App base URL (default: %(default)s).")
    parser.add_argument("--cookie", default=os.getenv("SIMCRICKETX_ADMIN_COOKIE", ""),
                        help="Admin session cookie ('session=...' or the bare value).")
    parser.add_argument("--cookie-file", help="File containing the cookie instead of --cookie.")
    parser.add_argument("--mode", choices=("skip", "update"), default="skip",
                        help="Existing names: skip them, or overwrite their ratings (default: %(default)s).")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help="Rows per request (default: %(default)s; clamped to the server's cap).")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help="Hard cap on each request body in bytes (default: %(default)s).")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help="Retries per batch on 429/5xx, with backoff (default: %(default)s).")
    parser.add_argument("--sleep", type=float, default=0.0,
                        help="Seconds to pause between batches (be kind to a small prod box).")
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-request timeout in seconds.")
    parser.add_argument("--start-row", type=int, default=0, help="Skip this many rows before importing.")
    parser.add_argument("--limit", type=int, default=0, help="Import at most this many rows (0 = all).")
    parser.add_argument("--state", help="State file for resuming an interrupted run.")
    parser.add_argument("--resume", action="store_true",
                        help="Continue from the row count recorded in --state.")
    parser.add_argument("--error-log", help="Append every rejected-row message to this file.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the batch plan and exit (no requests, no validation).")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    rows = load_rows(args.file)
    total_in_file = len(rows)

    start = max(0, args.start_row)
    if args.resume:
        start = max(start, read_state(args.state, args.file))
    if start:
        rows = rows[start:]
    if args.limit > 0:
        rows = rows[:args.limit]
    if not rows:
        print(f"Nothing to do: {total_in_file:,} rows in file, {start:,} already done.")
        return 0

    cookie = args.cookie
    if args.cookie_file:
        with open(os.path.expanduser(args.cookie_file), "r", encoding="utf-8") as handle:
            cookie = handle.read()

    batch_size = max(1, args.batch_size)
    if args.dry_run:
        batches = plan_batches(rows, batch_size, args.max_bytes)
        biggest = max(len(json.dumps(chunk)) for _, chunk in batches)
        print(f"File           : {args.file}")
        print(f"Rows in file   : {total_in_file:,}")
        print(f"Rows to send   : {len(rows):,} (starting at row {start + 1:,})")
        print(f"Batches        : {len(batches):,} of up to {batch_size} rows")
        print(f"Largest request: {biggest:,} bytes (cap {args.max_bytes:,})")
        print(f"Mode           : {args.mode}")
        print("Dry run — nothing sent.")
        return 0

    client = AdminClient(args.base_url, cookie, timeout=args.timeout)
    client.authenticate()

    limits = client.limits()
    server_cap = int(limits.get("max_chunk_rows") or 0)
    if server_cap and batch_size > server_cap:
        print(f"Batch size {batch_size} exceeds the server cap; using {server_cap}.", file=sys.stderr)
        batch_size = server_cap

    batches = plan_batches(rows, batch_size, args.max_bytes)
    totals = {"imported": 0, "updated": 0, "skipped": 0, "failed": 0}
    meter = Meter(len(rows), len(batches))
    error_log = open(args.error_log, "a", encoding="utf-8") if args.error_log else None

    print(
        f"Importing {len(rows):,} rows from {args.file} into {args.base_url} "
        f"in {len(batches):,} batches of up to {batch_size} (mode={args.mode}).",
        file=sys.stderr,
    )

    rows_done = 0
    exit_code = 0
    try:
        for batch_index, (offset, chunk) in enumerate(batches, start=1):
            payload = {"rows": chunk, "mode": args.mode, "offset": start + offset}
            status, body = _send_with_retry(client, payload, args.retries, meter)

            if status != 200 or not body.get("ok"):
                meter.done()
                print(
                    f"Batch {batch_index}/{len(batches)} failed (HTTP {status}): "
                    f"{body.get('error') or body.get('reason') or 'unknown error'}",
                    file=sys.stderr,
                )
                print(
                    f"{rows_done:,} rows were already imported. Re-run with "
                    f"--start-row {start + rows_done}"
                    + (f" (or --resume --state {args.state})" if args.state else "")
                    + " after fixing the cause.",
                    file=sys.stderr,
                )
                exit_code = 1
                break

            for key in ("imported", "updated", "skipped", "failed"):
                totals[key] += int(body.get(key) or 0)
            for message in body.get("errors") or []:
                if error_log:
                    error_log.write(message + "\n")
            rows_done += int(body.get("received") or len(chunk))

            meter.render(rows_done, batch_index, totals)
            write_state(args.state, args.file, start + rows_done, totals)
            if args.sleep:
                time.sleep(args.sleep)
    except KeyboardInterrupt:
        meter.done()
        print(
            f"Interrupted after {rows_done:,} rows. Re-run with --start-row "
            f"{start + rows_done} to continue.",
            file=sys.stderr,
        )
        exit_code = 130
    finally:
        if error_log:
            error_log.close()

    meter.done()
    print(
        f"imported={totals['imported']:,} updated={totals['updated']:,} "
        f"skipped={totals['skipped']:,} rejected={totals['failed']:,} "
        f"rows_sent={rows_done:,}/{len(rows):,}"
    )
    if totals["failed"] and args.error_log:
        print(f"Rejected-row messages appended to {args.error_log}.")
    elif totals["failed"]:
        print("Re-run with --error-log PATH to capture the rejected-row messages.")
    return exit_code


def _send_with_retry(client, payload, retries, meter):
    """Retry 429/5xx with backoff; halve the batch if the host returns 413."""
    attempt = 0
    while True:
        attempt += 1
        status, body = client.send_chunk(payload)

        if status == 413 and len(payload["rows"]) > 1:
            half = -(-len(payload["rows"]) // 2)
            meter.stream.write(
                f"\n413 from the host — splitting this batch into {half} + "
                f"{len(payload['rows']) - half} rows.\n"
            )
            first = {"rows": payload["rows"][:half], "mode": payload["mode"],
                     "offset": payload["offset"]}
            second = {"rows": payload["rows"][half:], "mode": payload["mode"],
                      "offset": payload["offset"] + half}
            status_a, body_a = _send_with_retry(client, first, retries, meter)
            if status_a != 200 or not body_a.get("ok"):
                return status_a, body_a
            status_b, body_b = _send_with_retry(client, second, retries, meter)
            if status_b != 200 or not body_b.get("ok"):
                return status_b, body_b
            merged = {"ok": True}
            for key in ("imported", "updated", "skipped", "failed", "received"):
                merged[key] = int(body_a.get(key) or 0) + int(body_b.get(key) or 0)
            merged["errors"] = (body_a.get("errors") or []) + (body_b.get("errors") or [])
            return 200, merged

        retryable = status == 429 or status >= 500
        if retryable and attempt <= retries:
            wait = 2 ** (attempt - 1)
            meter.stream.write(f"\nHTTP {status} — retrying in {wait}s ({attempt}/{retries}).\n")
            meter.stream.flush()
            time.sleep(wait)
            continue
        return status, body


if __name__ == "__main__":
    sys.exit(main())
