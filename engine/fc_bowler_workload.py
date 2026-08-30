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
        # Spell state: overs in the current spell, overs rested since it
        # ended, and accumulated fatigue (0 = fresh).
        self._spell_overs: Dict[str, int] = {p["name"]: 0 for p in self._eligible_xi}
        self._rest_overs: Dict[str, int] = {p["name"]: 0 for p in self._eligible_xi}
        self._fatigue: Dict[str, float] = {p["name"]: 0.0 for p in self._eligible_xi}

    def reset(self, bowling_xi: list, carry_fraction: float = 0.0):
        """Re-initialise for a new innings this side bowls (mirrors
        BowlerManager.reset's call shape from _reset_innings_state).

        carry_fraction carries a share of the previous innings' workload
        into the new one instead of wiping it. A side that has just bowled
        130 overs does NOT walk out fresh, and that residual tiredness is
        the single biggest real-world reason a captain declines the
        follow-on — with a full reset, enforcing it cost nothing at all.
        0.0 keeps the original behaviour for the ordinary innings change
        after both sides have had a bat.
        """
        prior = dict(self._overs_this_innings) if carry_fraction > 0 else {}
        prior_fatigue = dict(self._fatigue) if carry_fraction > 0 else {}
        self._eligible_xi = [p for p in bowling_xi if p.get("will_bowl", False)]
        self._overs_this_innings = {
            p["name"]: int(round(prior.get(p["name"], 0) * carry_fraction))
            for p in self._eligible_xi
        }
        self._last_bowler = None
        self._prev_over_runs = {}
        # Everyone starts a fresh spell, but tiredness is what actually
        # carries across a follow-on.
        self._spell_overs = {p["name"]: 0 for p in self._eligible_xi}
        self._rest_overs = {p["name"]: 0 for p in self._eligible_xi}
        self._fatigue = {
            p["name"]: min(_MAX_FATIGUE,
                           prior_fatigue.get(p["name"], 0.0) * carry_fraction)
            for p in self._eligible_xi
        }

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

        def _sort_key(player):
            name = player["name"]
            # A bowler who has bowled out his spell drops behind everyone
            # else — the captain rotates rather than flogging him. Within
            # that, style preference for the surface, then the best bowler
            # available RIGHT NOW: rating discounted by current fatigue, so
            # a fresh second-change seamer can outrank a spent strike bowler.
            effective = (player.get("bowling_rating", 0)
                         * self.get_fatigue_mult(name,
                                                 player.get("stamina_rating", 50) or 50))
            return (1 if self.is_spell_spent(name) else 0, _bucket(player), -effective)

        return sorted(eligible, key=_sort_key)

    def record_over_completion(self, bowler_name: str, runs_this_over: int):
        """Book one over to a bowler, and advance everyone else's rest.

        This is the tick that drives the whole spell model: the bowler who
        just bowled tires and extends his spell, and every other bowler in
        the XI recovers a little. Without the second half there is no such
        thing as being rested, which is why the old model had a bowler
        decaying in a straight line from his first over to his fortieth.
        """
        self._overs_this_innings[bowler_name] = self._overs_this_innings.get(bowler_name, 0) + 1
        self._prev_over_runs[bowler_name] = runs_this_over
        self._last_bowler = bowler_name

        self._spell_overs[bowler_name] = self._spell_overs.get(bowler_name, 0) + 1
        self._rest_overs[bowler_name] = 0
        self._fatigue[bowler_name] = min(
            _MAX_FATIGUE,
            self._fatigue.get(bowler_name, 0.0) + self._over_cost(bowler_name),
        )

        for player in self._eligible_xi:
            name = player["name"]
            if name == bowler_name:
                continue
            self._rest_overs[name] = self._rest_overs.get(name, 0) + 1
            self._fatigue[name] = max(
                0.0, self._fatigue.get(name, 0.0) - self._recovery_rate(name))
            # A long enough breather ends the spell: he is available to be
            # brought back, and starts a fresh one when he is.
            if self._rest_overs[name] >= self._min_rest(name):
                self._spell_overs[name] = 0

    # ── Spell bookkeeping ────────────────────────────────────────────────

    def _player(self, bowler_name: str) -> dict:
        for p in self._eligible_xi:
            if p["name"] == bowler_name:
                return p
        return {}

    def _is_spin(self, bowler_name: str) -> bool:
        return (self._player(bowler_name).get("bowling_type") or "").strip() in _SPIN_TYPES

    def _stamina(self, bowler_name: str) -> int:
        return max(0, min(100, self._player(bowler_name).get("stamina_rating", 50) or 50))

    def max_spell_overs(self, bowler_name: str) -> int:
        """How long a spell this bowler can sustain before the captain takes
        him off. A quick bowls 5-8; a spinner can wheel away for 10-18."""
        if self._is_spin(bowler_name):
            base, bonus = _SPIN_SPELL_BASE, _SPIN_SPELL_STAMINA_BONUS
        else:
            base, bonus = _PACE_SPELL_BASE, _PACE_SPELL_STAMINA_BONUS
        return base + int(round(bonus * self._stamina(bowler_name) / 100.0))

    def _min_rest(self, bowler_name: str) -> int:
        return _SPIN_MIN_REST if self._is_spin(bowler_name) else _PACE_MIN_REST

    def _over_cost(self, bowler_name: str) -> float:
        base = _SPIN_OVER_COST if self._is_spin(bowler_name) else _PACE_OVER_COST
        # High stamina costs less per over; low stamina burns out fast.
        return base * (1.0 - self._stamina(bowler_name) / 200.0)

    def _recovery_rate(self, bowler_name: str) -> float:
        base = _SPIN_RECOVERY if self._is_spin(bowler_name) else _PACE_RECOVERY
        return base * (0.75 + self._stamina(bowler_name) / 200.0)

    def spell_overs(self, bowler_name: str) -> int:
        return self._spell_overs.get(bowler_name, 0)

    def is_spell_spent(self, bowler_name: str) -> bool:
        """True when this bowler has bowled out his spell and should be
        rested. A soft signal, not an eligibility rule — see
        rank_by_style_preference. Making it hard would deadlock a four-man
        attack."""
        return self._spell_overs.get(bowler_name, 0) >= self.max_spell_overs(bowler_name)

    def get_fatigue_mult(self, bowler_name: str, stamina_rating: int = 50) -> float:
        """
        Effectiveness multiplier from accumulated fatigue. Fatigue now rises
        while bowling and falls while resting (see record_over_completion),
        so a bowler comes back for a second spell refreshed rather than
        continuing a one-way slide from his first over.

        Floors at 0.55 (matching BowlerManager's most-worn T20/ListA value)
        so a workhorse never goes fully ineffective.

        stamina_rating is accepted for call-site compatibility; the manager
        reads stamina off the XI itself, and falls back to this value for a
        bowler it does not know about.
        """
        if bowler_name not in self._fatigue and bowler_name not in self._overs_this_innings:
            return 1.0
        if not self._player(bowler_name):
            # Unknown to this manager (a test double, or a bowler swapped in
            # mid-innings): fall back to the old flat per-over slope.
            done = self._overs_this_innings.get(bowler_name, 0)
            slope = max(0.0, 0.010 - 0.0001 * max(0, min(100, stamina_rating)))
            return max(_MIN_FATIGUE_MULT, 1.0 - slope * done)
        return max(_MIN_FATIGUE_MULT, 1.0 - self._fatigue.get(bowler_name, 0.0))

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
# ── Spell model ──────────────────────────────────────────────────────────
# A first-class bowler operates in spells, not in a single unbroken shift.
# A quick runs in for five to eight overs and is then taken off; a spinner
# can wheel away for two or three times that. Resting recovers him, which is
# what makes a second spell after tea — with the old ball reversing — a real
# thing rather than a continuation of the first.
_PACE_SPELL_BASE = 5
_PACE_SPELL_STAMINA_BONUS = 3     # -> 5-8 overs
_SPIN_SPELL_BASE = 10
_SPIN_SPELL_STAMINA_BONUS = 8     # -> 10-18 overs

_PACE_MIN_REST = 8                # overs off before a quick comes back
_SPIN_MIN_REST = 5

_PACE_OVER_COST = 0.045           # fatigue added per over bowled
_SPIN_OVER_COST = 0.022
_PACE_RECOVERY = 0.020            # fatigue shed per over rested
_SPIN_RECOVERY = 0.016

_MAX_FATIGUE = 0.45               # matches the 0.55 effectiveness floor
_MIN_FATIGUE_MULT = 0.55

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
