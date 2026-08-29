"""
engine/fc_bowler_workload.py
=============================

Bowler selection and workload for First-Class (FC) matches — the
uncapped-spell counterpart to `engine/bowler_manager.py`'s over-quota
model. This is a deliberately SEPARATE class, not a branch inside
`BowlerManager`: FC has no per-bowler over cap and no "every will_bowl
player must bowl at least 1 over" forcing rule (a specialist batter in a
real Test XI may never bowl), so most of `BowlerManager`'s quota logic
simply doesn't apply.

Rules enforced
--------------
1. No-consecutive — MCC Law 17.2, universal (a bowler may not bowl two
   overs in a row). No quota exists to relax in FC, so this is the only
   hard eligibility rule.
2. Fatigue — a continuous per-innings effectiveness decay driven by a
   bowler's `stamina_rating` (0-100), not a fixed fatigue table keyed by
   an over count that doesn't exist for FC.
3. Day-stage bowling-style preference — a SOFT ranking (not a hard
   eligibility filter) that shifts from pace-first toward spin-first as
   the pitch wears, so the AI captain's bowling changes visibly track the
   pitch × bowling-style dynamic in ball_outcome.py/ground_config.py.
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class FCBowlerManager:
    """
    Manages bowler eligibility, workload/fatigue, and day-stage style
    preference for one innings of an FC match.

    Parameters
    ----------
    bowling_xi : list of player dicts from the bowling team XI.
    fmt        : MultiDayFormatConfig instance for the current match.
    """

    def __init__(self, bowling_xi: list, fmt):
        self.fmt = fmt
        self._eligible_xi: List[dict] = [
            p for p in bowling_xi if p.get("will_bowl", False)
        ]
        self._overs_this_innings: Dict[str, int] = {p["name"]: 0 for p in self._eligible_xi}
        self._last_bowler: Optional[str] = None
        self._prev_over_runs: Dict[str, int] = {}

    def reset(self, bowling_xi: list):
        """Re-initialise for a new innings this side bowls (mirrors
        BowlerManager.reset's call shape from _reset_innings_state)."""
        self._eligible_xi = [p for p in bowling_xi if p.get("will_bowl", False)]
        self._overs_this_innings = {p["name"]: 0 for p in self._eligible_xi}
        self._last_bowler = None
        self._prev_over_runs = {}

    # ------------------------------------------------------------------ #
    # Public query interface                                              #
    # ------------------------------------------------------------------ #

    def get_eligible_bowlers(self, current_over: int) -> List[dict]:
        """
        Return bowlers eligible to bowl the current over. Only Law 17.2
        applies — no quota, no forced-fresh-bowler rule (unlike
        BowlerManager, an FC specialist batter may simply never bowl).
        """
        eligible = [p for p in self._eligible_xi if p["name"] != self._last_bowler]
        if eligible:
            return eligible
        # Only one eligible bowler total (a tiny attack) — Law 17.2 has no
        # legal alternative; surface the same empty-list signal
        # BowlerManager's fallback-2 path uses so callers handle it the
        # same way.
        logger.warning(
            "FCBowlerManager: no non-consecutive bowler available at over %d "
            "(last bowler=%s)", current_over, self._last_bowler,
        )
        return []

    def rank_by_style_preference(self, eligible: List[dict], pitch_wear: float, fc_day: int) -> List[dict]:
        """
        Ranking (not filtering) of *eligible* bowlers by day-stage
        bowling-style preference — pace-first while the pitch is fresh,
        shifting to spin-first as it wears — with bowling_rating as the
        tiebreaker within each style bucket (so callers can simply take
        ranked[0] as "the best available bowler in the preferred style").
        """
        preferred = _fc_preferred_style(pitch_wear, fc_day)

        def _bucket(player):
            if preferred is None:
                return 0
            style = (player.get("bowling_type") or "").strip()
            is_spin = style in _SPIN_TYPES
            if preferred == "spin":
                return 0 if is_spin else 1
            return 0 if not is_spin else 1

        return sorted(eligible, key=lambda p: (_bucket(p), -p.get("bowling_rating", 0)))

    def record_over_completion(self, bowler_name: str, runs_this_over: int):
        self._overs_this_innings[bowler_name] = self._overs_this_innings.get(bowler_name, 0) + 1
        self._prev_over_runs[bowler_name] = runs_this_over
        self._last_bowler = bowler_name

    def get_fatigue_mult(self, bowler_name: str, stamina_rating: int = 50) -> float:
        """
        Continuous fatigue decay, keyed by a bowler's stamina rating
        (0-100) rather than a fixed table indexed by an over count that
        doesn't exist for FC's uncapped spells. Floors at 0.55 (matching
        BowlerManager's most-worn T20/ListA value) so a workhorse bowler
        never goes fully ineffective. The slope is clamped to >= 0 — at
        stamina_rating=100 a bowler simply doesn't fatigue within one
        innings; it must never go negative, which would make bowling MORE
        overs increase effectiveness past 1.0.
        """
        done = self._overs_this_innings.get(bowler_name, 0)
        slope = max(0.0, 0.010 - 0.0001 * max(0, min(100, stamina_rating)))
        return max(0.55, 1.0 - slope * done)

    def overs_bowled(self, bowler_name: str) -> int:
        return self._overs_this_innings.get(bowler_name, 0)

    def overs_remaining(self, bowler_name: str) -> Optional[int]:
        """No quota in FC, so there's no meaningful cap to count down from
        — always None. Present for polymorphic compatibility with
        BowlerManager.overs_remaining(), which callers may invoke without
        checking which manager type they hold."""
        return None

    def prev_over_runs(self, bowler_name: str) -> int:
        return self._prev_over_runs.get(bowler_name, -1)

    def last_bowler(self) -> Optional[str]:
        return self._last_bowler

    def is_consecutive(self, bowler_name: str) -> bool:
        return bowler_name == self._last_bowler


# ---------------------------------------------------------------------------
# Day-stage bowling-style preference
# ---------------------------------------------------------------------------

# Bowling-type values that count as "spin" for the purposes of the
# preference ranking — matches the keys used in wicket_factors_start/_end
# in config/ground_conditions_defaults.yaml's FC block.
_SPIN_TYPES = {"Off spin", "Leg spin", "Finger spin", "Wrist spin"}

# Wear threshold past which the preference shifts to spin-first.
_SPIN_PREFERENCE_WEAR_THRESHOLD = 0.5
_SPIN_PREFERENCE_DAY_THRESHOLD = 3


def _fc_preferred_style(pitch_wear: float, fc_day: int) -> Optional[str]:
    """
    Returns "pace", "spin", or None (no preference — let bowler quality
    alone decide) for the current match state. A pitch that increasingly
    favors spin only matters if the AI actually turns to its spinners;
    this is what makes that visible in the bowling changes rather than
    only in the per-ball wicket odds.
    """
    if pitch_wear >= _SPIN_PREFERENCE_WEAR_THRESHOLD or fc_day >= _SPIN_PREFERENCE_DAY_THRESHOLD:
        return "spin"
    if pitch_wear < 0.15 and fc_day <= 1:
        return "pace"
    return None
