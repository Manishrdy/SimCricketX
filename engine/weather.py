"""
engine/weather.py
=================

Rain forecast, hidden weather scripts, and interruption resolution.

Design (see also engine/dls.py):

* The user picks one of four forecast tiers at match setup. At match
  creation a single roll produces a hidden "weather script": zero or more
  rain events, each pinned to a global over of the match (a wall-clock
  proxy) with a number of overs lost. The script is stored in the match
  JSON, so a resumed match replays identically.

* Severity is expressed as a fraction of the scheduled innings, so the same
  tier feels equivalent in T20 (20 overs) and List A (50 overs).

* All interruptions land at over boundaries. `resolve_interruption` is a
  pure function that turns (game situation, overs lost) into one of:
  resume-with-fewer-overs, innings terminated, or match abandoned.

Simplified ICC rules implemented here:
  - Rain during innings 1 at `u` completed overs losing L overs revises
    BOTH innings to N' = max(u, scheduled - L) (equal allocations).
  - Rain during innings 2 revises only the chase: N2' = max(u, N2 - L).
  - A result requires the minimum overs per side (T20: 5, List A: 20).
    An innings-2 termination after the minimum is decided on DLS par;
    anything shorter is a No Result.
"""

import random
from math import ceil
from typing import Optional


# ── Forecast tiers (the four-tier setup selector) ─────────────────────────────

FORECAST_TIERS = {
    "clear": {
        "label": "Clear skies",
        "rain_chance": 0.0,
        "max_events": 0,
        "loss_fraction": (0.0, 0.0),
    },
    "passing_showers": {
        "label": "Passing showers",
        "rain_chance": 0.25,
        "max_events": 1,
        "loss_fraction": (0.10, 0.25),
    },
    "rain_around": {
        "label": "Rain around",
        "rain_chance": 0.50,
        "max_events": 2,          # capped to 1 for T20 below
        "loss_fraction": (0.20, 0.45),
    },
    "storm_warning": {
        "label": "Storm warning",
        "rain_chance": 0.75,
        "max_events": 2,          # capped to 1 for T20 below
        "loss_fraction": (0.35, 0.80),
    },
}

DEFAULT_FORECAST = "clear"

# Minimum overs the side batting second must face for a result.
MIN_OVERS_FOR_RESULT = {"T20": 5, "ListA": 20}


def forecast_label(forecast: str) -> str:
    return FORECAST_TIERS.get(forecast, FORECAST_TIERS[DEFAULT_FORECAST])["label"]


def min_overs_for_result(format_name: str) -> int:
    return MIN_OVERS_FOR_RESULT.get(format_name, 5)


def revised_max_bowler_overs(innings_overs: int) -> int:
    """ICC-style quota for a (possibly shortened) innings: ceil(overs / 5).

    Reproduces the full-length quotas exactly (20 -> 4, 50 -> 10)."""
    return max(1, ceil(innings_overs / 5))


# ── Weather script generation (one roll at match creation) ────────────────────

def generate_weather_script(forecast: str, scheduled_overs: int,
                            format_name: str, rng: Optional[random.Random] = None) -> dict:
    """
    Roll the hidden weather script for a match.

    Returns a JSON-serializable dict:
      {"forecast": ..., "events": [{"at_global_over": int, "overs_lost": int}, ...]}

    `at_global_over` counts completed overs across the whole match
    (innings 1 then innings 2), so an event can land in either innings —
    or exactly at the innings break, which plays as a pre-chase reduction.
    Events are consumed in order by the match engine.
    """
    rng = rng or random.Random()
    tier = FORECAST_TIERS.get(forecast, FORECAST_TIERS[DEFAULT_FORECAST])

    script = {"forecast": forecast, "events": []}
    if tier["rain_chance"] <= 0 or rng.random() >= tier["rain_chance"]:
        return script

    max_events = tier["max_events"]
    if format_name == "T20":
        max_events = min(max_events, 1)   # one clean stoppage max in T20
    n_events = 1 if max_events <= 1 else rng.choice([1, 1, 2])   # 2nd event is the rarity

    total_match_overs = scheduled_overs * 2
    lo, hi = tier["loss_fraction"]

    events = []
    for _ in range(n_events):
        at_over = rng.randint(1, total_match_overs - 1)
        overs_lost = max(1, round(scheduled_overs * rng.uniform(lo, hi)))
        events.append({"at_global_over": at_over, "overs_lost": overs_lost})

    # Order chronologically and keep events at least 4 overs apart.
    events.sort(key=lambda e: e["at_global_over"])
    filtered = []
    for ev in events:
        if not filtered or ev["at_global_over"] - filtered[-1]["at_global_over"] >= 4:
            filtered.append(ev)
    script["events"] = filtered
    return script


# ── Interruption resolution (pure) ────────────────────────────────────────────

# Outcome types returned by resolve_interruption
RESUME = "resume"                       # innings continues with fewer overs
INNINGS_TERMINATED = "innings_terminated"   # current innings is over, match continues
CHASE_TERMINATED = "chase_terminated"   # innings 2 over: decide on DLS par
NO_RESULT = "no_result"                 # match abandoned


def resolve_interruption(innings: int, overs_completed: int, innings_overs: int,
                         overs_lost: int, format_name: str) -> dict:
    """
    Decide what a rain stoppage does to the current innings.

    innings         : 1 or 2
    overs_completed : completed overs in the CURRENT innings at the stoppage
    innings_overs   : the innings' current allocation (may already be revised)
    overs_lost      : severity of this event
    format_name     : "T20" | "ListA"

    Returns {"type": <outcome>, "revised_overs": int} where revised_overs is
    the new allocation for the affected innings (both innings when rain hits
    innings 1; only the chase when it hits innings 2).
    """
    minimum = min_overs_for_result(format_name)
    revised = max(overs_completed, innings_overs - overs_lost)

    if innings == 1:
        if revised < minimum:
            return {"type": NO_RESULT, "revised_overs": revised}
        if revised <= overs_completed:
            return {"type": INNINGS_TERMINATED, "revised_overs": overs_completed}
        return {"type": RESUME, "revised_overs": revised}

    # Innings 2 — the chase.
    if revised <= overs_completed:
        # Cannot resume: decided on par if the minimum was reached.
        if overs_completed >= minimum:
            return {"type": CHASE_TERMINATED, "revised_overs": overs_completed}
        return {"type": NO_RESULT, "revised_overs": overs_completed}
    if revised < minimum:
        # Resuming would still leave the chase short of a valid result.
        if overs_completed >= minimum:
            return {"type": CHASE_TERMINATED, "revised_overs": overs_completed}
        return {"type": NO_RESULT, "revised_overs": revised}
    return {"type": RESUME, "revised_overs": revised}
