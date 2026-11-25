"""Open-result low-score survival, version 1.

All decisions are derived from live cricket state: no script, private RNG, or
hidden checkpoint history. Existing match snapshots therefore preserve the
inputs exactly. Corridors describe intent, never prescribe runs or wickets.
"""
import logging

from engine.game_state_engine import get_par_score

logger = logging.getLogger(__name__)
MODE = "adaptive_v1"


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


class MatchDirector:
    steers_first_innings = True

    def __init__(self, match):
        self.match = match
        self.scenario_type = MODE
        logger.info("[Director] Low-score survival available; result remains open")

    def on_innings_transition(self):
        logger.info("[Director] Reassessing low-score survival for chase")

    def get_phase(self):
        # Do not invoke the legacy convergence phase's collapse protection.
        return "adaptive" if self.match.innings in (1, 2) else "inactive"

    def get_override_outcome(self, batter, bowler):
        return None

    def _resources(self):
        m = self.match
        pair = [p for p in (m.current_striker, m.current_non_striker) if p]
        remaining = [m.batting_team[i] for i in m.remaining_batter_indices]
        depth = sum(clamp((p.get("batting_rating", 50) - 35) / 45)
                    for p in remaining)
        batting = sum(p.get("batting_rating", 50) for p in pair) / max(1, len(pair))
        bowlers = [(p, m.bowler_manager.overs_remaining(p["name"]))
                   for p in m.bowling_team if p.get("will_bowl")]
        capacity = sum(left for _, left in bowlers)
        bowling = (sum(p.get("bowling_rating", 50) * left for p, left in bowlers)
                   / capacity if capacity else 50)
        return depth, clamp((batting - bowling) / 50, -1, 1)

    def get_scenario_bias(self, match_state):
        m = self.match
        if getattr(m, "is_fc", False) or m.innings not in (1, 2):
            return {}
        bpo = m.fmt.balls_per_over
        total = m.fmt.overs * bpo
        played = m.current_over * bpo + m.current_ball
        left = total - played
        if left <= 0 or m.wickets >= 10:
            return {}
        progress = played / total
        par = max(1.0, get_par_score(m.fmt.overs, m.fmt, m.pitch))
        neutral = max(1.0, get_par_score(m.fmt.overs, m.fmt, "Hard"))
        # Pitch is shared; venue/weather and team skills remain in normal odds.
        # Observed scoring additionally captures their combined effect.
        pitch_support = clamp((1 - par / neutral) / 0.2)
        observed = m.score / max(1, played) * total
        evidence = clamp((progress - 0.15) / 0.20)
        low_score_support = clamp((0.95 - observed / par) / 0.25) * evidence
        support = max(pitch_support * (1 - evidence), low_score_support)
        depth, matchup = self._resources()
        wicket_pressure = clamp((m.wickets - (2 + 4 * progress)) / 3)
        tail_pressure = clamp((2 - depth) / 2)
        # Rebuilding buys survival at the cost of boundaries, never immunity.
        survival = max(wicket_pressure, tail_pressure) * (1 - progress)
        if m.innings == 1:
            # Flexible final-total corridor: 70–95% of condition-adjusted par.
            # Acceleration only late; do not boost an implausible recovery.
            urgency = clamp((progress - 0.65) / 0.35)
            intent = urgency * (0.5 + 0.25 * matchup) - survival
            if evidence and observed > par * 1.15:
                return self._fallback("first innings outside low-score corridor")
        else:
            if not m.target or m.score >= m.target:
                return {}
            support = clamp((0.95 - (m.target - 1) / par) / 0.25)
            needed = m.target - m.score
            rate = needed / left
            capacity = par / total * (1 + 0.20 * matchup)
            # Easy and implausible chases are released, never held back/rescued.
            if needed <= 4 or (10 - m.wickets >= 4 and rate < capacity * 0.35):
                return self._fallback("comfortable chase")
            if rate > capacity * 2.5:
                return self._fallback("chase beyond plausible recovery")
            urgency = clamp((rate / max(0.1, capacity) - 0.65) / 0.85)
            intent = urgency - survival * (1 - urgency)
        if support <= 0:
            return self._fallback("conditions/score do not support low-score survival")
        # Smooth activation with legal-ball progress; extras do not advance it.
        strength = 0.12 * support * clamp(played / max(bpo * 2, total * 0.1))
        intent = clamp(intent, -1, 1)
        factors = {
            "Four": 1 + strength * intent,
            "Six": 1 + strength * intent,
            "Wicket": 1 + strength * intent * 0.6,
            "Single": 1 + strength * (1 - max(0, intent)) * 0.5,
            "Double": 1 + strength * (1 - max(0, intent)) * 0.25,
        }
        logger.debug("[Director] innings=%s support=%.3f intent=%.3f factors=%s",
                     m.innings, support, intent, factors)
        return {"match_director": factors}

    @staticmethod
    def _fallback(reason):
        logger.debug("[Director] Ordinary simulation: %s", reason)
        return {}


def apply_director_weights(weights, factors, baseline):
    """Bound incremental influence and refuse to amplify extreme stacked odds.

    Preserve total non-extra mass so this feature cannot manufacture extras.
    Existing skill/pressure effects are not clipped or undone. When their ratio
    to pre-psychology weights is already extreme, decline additional steering
    in that direction. Final per-category probability changes are limited to 12%.
    """
    result = dict(weights)
    keys = ("Dot", "Single", "Double", "Three", "Four", "Six", "Wicket")
    before = sum(weights.get(k, 0) for k in keys)
    for key in keys:
        value = weights.get(key, 0)
        factor = clamp(factors.get(key, 1), 0.88, 1.12)
        base = baseline.get(key, 0)
        if base > 0 and ((value / base >= 2 and factor > 1)
                         or (value / base <= 0.5 and factor < 1)):
            factor = 1
        if key in result:
            result[key] = value * factor
    after = sum(result.get(k, 0) for k in keys)
    if after > 0:
        for key in keys:
            if key in result:
                result[key] *= before / after
    # Rescaling can move an unchanged category. If that would amplify an
    # already extreme psychological effect, spend no steering budget here.
    for key in keys:
        old, new, base = weights.get(key, 0), result.get(key, 0), baseline.get(key, 0)
        if base > 0 and ((old / base >= 2 and new > old + 1e-12)
                         or (old / base <= 0.5 and new < old - 1e-12)):
            return dict(weights)
    blend = 1.0
    for key in keys:
        old = weights.get(key, 0)
        delta = abs(result.get(key, 0) - old)
        if delta > 0:
            blend = min(blend, 0.12 * old / delta)
    return {key: value + blend * (result[key] - value)
            for key, value in weights.items()}
