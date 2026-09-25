"""Onboarding guide state: page tours seen + the new-user "first match" journey.

Two flows share one store (``users.guide_state``, JSON):

* **Page tours** — every user-facing page has a short spotlight tour that runs
  once, the first time the user opens that page. ``seen`` records each tour id
  the user finished (``done``) or dismissed (``skipped``); either stops it
  auto-starting again. The "?" button replays a tour on demand.

* **Journey** — for a brand-new account (no teams, no matches) the guide walks
  the user through: create a team → create a second → simulate a match. The
  *stage* is never stored: it is derived from real data on every request
  (published teams, archived matches), so it cannot drift out of step with
  what the user has actually done — deleting a team moves them back a stage,
  and building teams by some other route moves them forward.

Only ``journey`` status is stored:
  absent     → never decided. Offered iff the account has no teams and no
               matches ("eligible"); otherwise the user is an existing user and
               gets page tours only.
  "active"   → the user has begun it (recorded client-side on first display).
  "dismissed"→ the user chose "Skip getting started".
  "done"     → first match archived and the celebration was shown.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

STATE_VERSION = 1

# Flask endpoint → guide page id. Only these pages carry the guide (the "?"
# button, auto-start tours, the journey card). Admin pages are deliberately
# absent: the guide is for user-level pages only.
GUIDE_PAGES = {
    "home": "home",
    "create_team": "team_create",
    "manage_teams": "manage_teams",
    "team_squad": "team_squad",
    "match_setup": "match_setup",
    "match_detail": "match_detail",
    "tournaments": "tournaments",
    "create_tournament_route": "tournament_create",
    "tournament_dashboard": "tournament_dashboard",
    "create_tour_route": "tour_create",
    "player_pool": "player_pool",
    "ground_conditions": "ground_conditions",
    "my_matches": "my_matches",
    "statistics": "statistics",
    "community_index": "community",
}

JOURNEY_STATUSES = ("active", "dismissed", "done")
SEEN_STATUSES = ("done", "skipped")
# Stages, in order. "complete" = first match archived, celebration pending.
JOURNEY_STAGES = ("team1", "team2", "match", "complete")

_TOUR_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
MAX_SEEN = 100


def _empty() -> dict:
    return {"v": STATE_VERSION, "seen": {}}


def load_state(raw) -> dict:
    """Parse the stored JSON defensively. Anything malformed is dropped rather
    than raised: a corrupt blob must never break page rendering, and the worst
    outcome of discarding it is that a tour shows once more."""
    state = _empty()
    if not raw:
        return state
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return state
    if not isinstance(data, dict):
        return state

    seen = data.get("seen")
    if isinstance(seen, dict):
        for tour_id, status in list(seen.items())[:MAX_SEEN]:
            if is_valid_tour_id(tour_id) and status in SEEN_STATUSES:
                state["seen"][tour_id] = status

    journey = data.get("journey")
    if journey in JOURNEY_STATUSES:
        state["journey"] = journey
    for key in ("journey_started", "journey_finished"):
        if isinstance(data.get(key), str):
            state[key] = data[key][:32]
    return state


def dump_state(state: dict) -> str:
    return json.dumps(state, separators=(",", ":"), sort_keys=True)


def is_valid_tour_id(tour_id) -> bool:
    return isinstance(tour_id, str) and bool(_TOUR_ID_RE.match(tour_id))


def mark_tour(state: dict, tour_id: str, status: str) -> dict:
    """Record a tour as done/skipped. ``done`` wins over ``skipped`` so a later
    replay that is skipped part-way doesn't downgrade a finished tour."""
    if not is_valid_tour_id(tour_id):
        raise ValueError("invalid tour id")
    if status not in SEEN_STATUSES:
        raise ValueError("invalid tour status")
    seen = state.setdefault("seen", {})
    if tour_id not in seen and len(seen) >= MAX_SEEN:
        raise ValueError("too many tours")
    if seen.get(tour_id) == "done" and status == "skipped":
        return state
    seen[tour_id] = status
    return state


def set_journey(state: dict, status: str, *, now: datetime | None = None) -> dict:
    if status not in JOURNEY_STATUSES:
        raise ValueError("invalid journey status")
    stamp = (now or datetime.utcnow()).isoformat(timespec="seconds")
    current = state.get("journey")
    # A finished or dismissed journey can't be silently restarted by a stale
    # tab posting "active"; only reset_state() brings it back.
    if status == "active" and current in ("dismissed", "done"):
        return state
    state["journey"] = status
    if status == "active":
        state.setdefault("journey_started", stamp)
    else:
        state["journey_finished"] = stamp
    return state


def reset_state() -> dict:
    return _empty()


def journey_stage(published_teams: int, matches: int) -> str:
    if matches > 0:
        return "complete"
    if published_teams < 1:
        return "team1"
    if published_teams < 2:
        return "team2"
    return "match"


def journey_status(state: dict, *, any_teams: int, matches: int) -> str:
    """Effective status: stored value, else "eligible" for a brand-new account
    (nothing built yet), else "none" (existing user → page tours only)."""
    stored = state.get("journey")
    if stored:
        return stored
    return "eligible" if any_teams == 0 and matches == 0 else "none"
