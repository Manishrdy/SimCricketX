"""
engine/dls.py
=============

Duckworth-Lewis (Standard Edition) target revision for rain-affected matches.

Pure math module: no Match/engine imports, no side effects beyond loading the
embedded resource table once. Everything here operates on three inputs —
overs remaining, wickets lost, and runs — so it is trivially unit-testable.

Resource model
--------------
The embedded table (engine/data/dls_resource_table.json) is the published
D/L Standard Edition over-by-over reference table: for every combination of
whole overs remaining (0-50) and wickets lost (0-9) it gives the percentage
of a full 50-over innings' scoring resources that remain. A T20 innings is
handled by the same table, exactly as in real DLS: it simply *starts* at
R(20 overs, 0 wickets) = 56.6% and target maths uses resource ratios, so the
scale cancels.

Fractional overs (balls within an over) are linearly interpolated between
adjacent whole-over rows. The official ball-by-ball table differs from
linear interpolation by at most ~0.1 percentage points, which is well under
one run in any realistic scenario — this is the documented simplification.

Target rules (Standard Edition)
-------------------------------
Let S be Team 1's score, R1/R2 each team's total resource percentages:

  R2 == R1 : target = S + 1
  R2 <  R1 : target = floor(S * R2/R1) + 1
  R2 >  R1 : target = floor(S + G50 * (R2 - R1)/100) + 1

where G50 is the expected score for a *full 50-over* innings (100% of
resources). Callers working in T20 terms must scale their expected total up
to the 100%-resource equivalent (see `g50_from_expected_total`).

The "par score" at any point of Team 2's innings is
  par = floor(S * (resources Team 2 has consumed so far) / R1)
Team 2 is ahead if score > par; score == par at abandonment is a tie.
"""

import json
import math
import os
from typing import List, Optional

_TABLE_PATH = os.path.join(os.path.dirname(__file__), "data", "dls_resource_table.json")

# resources[overs_remaining][wickets_lost] -> percentage (0.0 - 100.0)
with open(_TABLE_PATH, "r") as _f:
    _RESOURCES: List[List[float]] = json.load(_f)["resources"]

MAX_OVERS = len(_RESOURCES) - 1   # 50


def resources_remaining(overs_remaining: float, wickets_lost: int) -> float:
    """
    Percentage of a full 50-over innings' resources remaining, given
    (possibly fractional) overs remaining and wickets lost.

    Fractional overs are interpolated linearly between whole-over rows.
    Out-of-range inputs are clamped (10 wickets lost -> 0 resources).
    """
    if wickets_lost >= 10:
        return 0.0
    wickets_lost = max(0, wickets_lost)
    overs_remaining = max(0.0, min(float(overs_remaining), float(MAX_OVERS)))

    lower = int(math.floor(overs_remaining))
    upper = min(lower + 1, MAX_OVERS)
    frac = overs_remaining - lower

    lo = _RESOURCES[lower][wickets_lost]
    hi = _RESOURCES[upper][wickets_lost]
    return lo + (hi - lo) * frac


def overs_from_balls(balls_remaining: int) -> float:
    """Convert a balls-remaining count to (fractional) overs remaining."""
    return max(0, balls_remaining) / 6.0


def g50_from_expected_total(expected_total: float, scheduled_overs: int) -> float:
    """
    Convert a format-scale expected innings total (e.g. the sim's pitch
    adjusted par of ~165 for a Hard-pitch T20) into its 100%-resource
    (G50) equivalent, as required by the R2 > R1 branch of the target
    formula. For a 50-over innings this is the identity.
    """
    start = resources_remaining(scheduled_overs, 0)
    if start <= 0:
        return expected_total
    return expected_total * 100.0 / start


class ResourceLedger:
    """
    Resource accounting for one innings.

    Starts with R(scheduled_overs, 0) and subtracts what each rain
    interruption steals: stopping with `u` overs remaining and `w` wickets
    lost, then resuming with only `u'` overs remaining, costs
    R(u, w) - R(u', w). An innings terminated outright loses R(u, w).
    """

    def __init__(self, scheduled_overs: int):
        self.scheduled_overs = scheduled_overs
        self.starting_resources = resources_remaining(scheduled_overs, 0)
        self.lost = 0.0
        self.interruptions = []   # audit log of dicts, serializable

    def record_interruption(self, overs_remaining_at_stop: float, wickets_lost: int,
                            overs_remaining_at_resume: float) -> float:
        """Record a stoppage that resumes with fewer overs. Returns resources lost."""
        at_stop = resources_remaining(overs_remaining_at_stop, wickets_lost)
        at_resume = resources_remaining(overs_remaining_at_resume, wickets_lost)
        lost = max(0.0, at_stop - at_resume)
        self.lost += lost
        self.interruptions.append({
            "overs_remaining_at_stop": overs_remaining_at_stop,
            "wickets_lost": wickets_lost,
            "overs_remaining_at_resume": overs_remaining_at_resume,
            "resources_lost": round(lost, 2),
        })
        return lost

    def record_termination(self, overs_remaining_at_stop: float, wickets_lost: int) -> float:
        """Record an innings ended outright by rain. Returns resources lost."""
        return self.record_interruption(overs_remaining_at_stop, wickets_lost, 0.0)

    def available(self) -> float:
        """Total resource percentage this innings had/has after all deductions."""
        return max(0.0, self.starting_resources - self.lost)

    # ── persistence ────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "scheduled_overs": self.scheduled_overs,
            "lost": self.lost,
            "interruptions": self.interruptions,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ResourceLedger":
        ledger = cls(data["scheduled_overs"])
        ledger.lost = data.get("lost", 0.0)
        ledger.interruptions = data.get("interruptions", [])
        return ledger


def compute_target(team1_score: int, r1: float, r2: float,
                   g50: Optional[float] = None) -> int:
    """
    DLS Standard Edition revised target for Team 2.

    team1_score : Team 1's final total
    r1, r2      : total resource percentages available to each team
    g50         : expected 50-over (100% resource) innings total; required
                  only when r2 > r1 (Team 1's innings was cut short).
    """
    if r1 <= 0:
        return team1_score + 1
    if abs(r2 - r1) < 1e-9:
        return team1_score + 1
    if r2 < r1:
        return int(math.floor(team1_score * r2 / r1)) + 1
    if g50 is None:
        raise ValueError("g50 is required when Team 2 has more resources than Team 1")
    return int(math.floor(team1_score + g50 * (r2 - r1) / 100.0)) + 1


def par_score(team1_score: int, r1: float, team2_resources_available: float,
              overs_remaining_now: float, wickets_lost_now: int) -> int:
    """
    DLS par score for Team 2 at the current point of its innings: the score
    at which the match would be tied were it abandoned right now.

    team2_resources_available : Team 2's total resources for the innings
                                (its ledger's available()), i.e. already net
                                of any completed interruptions.
    """
    if r1 <= 0:
        return 0
    remaining_now = resources_remaining(overs_remaining_now, wickets_lost_now)
    used = max(0.0, team2_resources_available - remaining_now)
    return int(math.floor(team1_score * used / r1))
