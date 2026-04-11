"""Bulk-close GitHub issues that were auto-filed by the test suite.

Background
----------
Before the conftest safety net was added, running pytest with a real `.env`
loaded would fire real `POST /repos/{repo}/issues` calls for every exception
raised inside any test. This produced ~80 false-positive issues on the
configured repository (label `auto-exception`).

This script closes those issues in bulk. It does NOT delete them — GitHub's
REST API does not support issue deletion (only GraphQL with admin scope), and
closing is reversible if any of them turn out to be real.

Usage
-----
    python scripts/cleanup_test_github_issues.py --dry-run
    python scripts/cleanup_test_github_issues.py
    python scripts/cleanup_test_github_issues.py --label auto-exception --limit 10

Reads `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, and (optionally)
`GITHUB_ISSUE_LABELS` from the environment / `.env` file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


CLOSE_COMMENT = (
    "Closed automatically: this issue was filed by the SimCricketX test suite "
    "before the conftest safety net was added. It is a false positive from a "
    "pytest run, not a real production exception. Reopen if you believe "
    "otherwise."
)


def _request(method: str, path: str, *, body: dict | None = None, query: dict | None = None) -> tuple[int | None, object]:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        return None, {"error": "GITHUB_TOKEN not set"}

    url = f"https://api.github.com{path}"
    if query:
        url = f"{url}?{urlparse.urlencode(query)}"

    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SimCricketX-Cleanup",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            return resp.getcode(), parsed
    except urlerror.HTTPError as e:
        try:
            parsed = json.loads(e.read().decode("utf-8"))
        except Exception:
            parsed = {"error": str(e)}
        return e.code, parsed
    except (urlerror.URLError, TimeoutError, ValueError) as e:
        return None, {"error": str(e)}


def list_open_issues_with_label(repo: str, label: str) -> list[dict]:
    """Page through every open issue carrying the given label."""
    collected: list[dict] = []
    page = 1
    while True:
        status, parsed = _request(
            "GET",
            f"/repos/{repo}/issues",
            query={
                "state": "open",
                "labels": label,
                "per_page": 100,
                "page": page,
            },
        )
        if status is None or status >= 300 or not isinstance(parsed, list):
            print(f"  ! list page {page} failed: status={status} body={parsed}", file=sys.stderr)
            break
        if not parsed:
            break
        # Pull requests show up under /issues; filter them out.
        for item in parsed:
            if "pull_request" in item:
                continue
            collected.append(item)
        if len(parsed) < 100:
            break
        page += 1
    return collected


def close_issue(repo: str, number: int) -> tuple[bool, str]:
    # Post comment first so the trail is preserved even if close fails.
    c_status, c_body = _request(
        "POST",
        f"/repos/{repo}/issues/{number}/comments",
        body={"body": CLOSE_COMMENT},
    )
    if c_status is None or c_status >= 300:
        return False, f"comment failed (status={c_status}): {c_body}"

    s_status, s_body = _request(
        "PATCH",
        f"/repos/{repo}/issues/{number}",
        body={"state": "closed", "state_reason": "not_planned"},
    )
    if s_status is None or s_status >= 300:
        return False, f"close failed (status={s_status}): {s_body}"
    return True, "closed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--label",
        default=os.getenv("GITHUB_ISSUE_LABELS", "auto-exception").split(",")[0].strip() or "auto-exception",
        help="Label to filter on (default: first entry in GITHUB_ISSUE_LABELS)",
    )
    parser.add_argument(
        "--repo",
        default=os.getenv("GITHUB_REPOSITORY", ""),
        help="Repository in owner/repo format (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument("--dry-run", action="store_true", help="List issues without closing them")
    parser.add_argument("--limit", type=int, default=0, help="Stop after N closes (0 = no limit)")
    args = parser.parse_args()

    if not args.repo:
        print("error: GITHUB_REPOSITORY not set and --repo not provided", file=sys.stderr)
        return 2
    if not os.getenv("GITHUB_TOKEN"):
        print("error: GITHUB_TOKEN not set", file=sys.stderr)
        return 2

    print(f"Repository : {args.repo}")
    print(f"Label      : {args.label}")
    print(f"Dry run    : {args.dry_run}")
    print()

    print(f"Fetching open issues with label '{args.label}'...")
    issues = list_open_issues_with_label(args.repo, args.label)
    print(f"Found {len(issues)} matching issue(s).\n")

    if not issues:
        return 0

    for i, issue in enumerate(issues, start=1):
        number = issue.get("number")
        title = (issue.get("title") or "").strip()[:80]
        url = issue.get("html_url", "")
        print(f"  [{i:>3}] #{number:<5} {title}")
        print(f"        {url}")

    if args.dry_run:
        print("\nDry run — nothing closed. Re-run without --dry-run to close them.")
        return 0

    print()
    confirm = input(f"About to close {len(issues)} issue(s). Type 'yes' to proceed: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        return 1

    print()
    closed = 0
    failed = 0
    for i, issue in enumerate(issues, start=1):
        if args.limit and closed >= args.limit:
            print(f"\nReached --limit {args.limit}; stopping.")
            break
        number = issue.get("number")
        ok, msg = close_issue(args.repo, number)
        if ok:
            closed += 1
            print(f"  [{i:>3}] #{number} -> {msg}")
        else:
            failed += 1
            print(f"  [{i:>3}] #{number} -> ERROR: {msg}", file=sys.stderr)

    print()
    print(f"Done. Closed: {closed}   Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
