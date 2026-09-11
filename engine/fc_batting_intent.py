"""Deterministic first-class tactics. All tuning lives here; no RNG or state.

Pace estimates are tactical heuristics, not win probabilities. Probabilities
for actual deliveries remain the responsibility of ball_outcome.
"""

import math


def bounded(value, low=0.0, high=1.0):
    value = float(value)
    return max(low, min(high, value)) if math.isfinite(value) else low


def ramp(value, low, high):
    """Smoothstep: continuous values and slopes at both ends."""
    x = bounded((value - low) / (high - low))
    return x * x * (3 - 2 * x)


def ability(player):
    bat = player.get("batting_rating")
    bat = 50 if bat is None else bounded(bat, 0, 100)
    tech = player.get("technique_rating")
    return 0.7 * bat + 0.3 * (bat if tech is None else bounded(tech, 0, 100))


PACE = {"Green": 3.1, "Dry": 3.4, "Hard": 3.6, "Flat": 4.0, "Dead": 4.5}
MAX_SINGLE_REFUSAL = 0.55
MAX_SINGLE_SEEK = 0.07  # share of dots converted, not a guaranteed single


def batting_intent(state):
    innings = state.get("fc_innings", 1)
    wickets = bounded(state.get("wickets", 0), 0, 10)
    overs = max(0.0, state.get("overs_remaining", 0))
    needed = state.get("runs_needed")
    strength = state.get("remaining_strength", 50)
    pace = PACE.get(state.get("pitch"), 3.6)
    pace *= 1 + bounded((strength - 60) / 250, -0.15, 0.15)
    pace *= 1 - 0.15 * bounded(state.get("pitch_wear", 0))
    pace *= 1 - 0.20 * ramp(wickets, 4, 10)
    pace *= 1 - 0.03 * (1 - ramp(state.get("ball_age", 20), 0, 15))
    survival = attack = 0.0
    reason = "accumulate"
    if innings == 4 and needed is not None:
        if needed <= 0 or overs <= 0:
            return dict(
                survival=0.0,
                attack=0.0,
                neutral=1.0,
                stumps=0.0,
                reason="complete",
                achievable_pace=pace,
            )
        ratio = (needed / max(overs, 1 / 6)) / pace
        survival = ramp(ratio, 1.05, 1.65)
        attack = ramp(ratio, 0.55, 1.15) * (1 - survival)
        reason = "save match" if survival > 0.5 else "chase"
    elif innings in (1, 2, 3):
        lead = state.get("lead")
        if lead is not None and lead < 0:
            survival = ramp(-lead, 40, 220) * (1 - ramp(overs, 45, 180))
            if state.get("follow_on"):
                survival = max(survival, ramp(-lead, 0, 180) * 0.65)
        budget = state.get("innings_budget")
        urgency = (
            ramp(state.get("innings_overs", 0) / budget, 0.75, 1.05) if budget else 0
        )
        late_push = (1 - ramp(overs, 90, 180)) * ramp(
            state.get("innings_overs", 0), 50, 80
        )
        attack = max(urgency, late_push) * (1 - survival)
        if state.get("declared"):
            attack = 0
        reason = (
            "save match"
            if survival > 0.5
            else ("build declaration" if attack > 0.3 else reason)
        )
    # Stumps is a separate mild caution, fading smoothly over the last hour.
    stumps = 1 - ramp(state.get("overs_today", 90), 0, state.get("last_hour_overs", 15))
    if innings == 4 and needed is not None:
        stumps *= survival  # live chases remain live at close of play
    stumps *= 1 - survival
    return dict(
        survival=survival,
        attack=attack,
        neutral=1 - survival - attack,
        stumps=stumps,
        reason=reason,
        achievable_pace=pace,
    )


def tail_protection(state, intent):
    """Return a probability-mass transfer policy for singles and dots."""
    if state.get("runs_needed") is not None and state["runs_needed"] <= 1:
        return {"refuse_single": 0.0, "seek_single": 0.0}
    gap = state.get("striker_ability", 50) - state.get("partner_ability", 50)
    resources = ramp(state.get("wickets", 0), 6, 9)
    strength = ramp(abs(gap), 15, 50) * resources * (1 - intent["attack"])
    ball = state.get("ball_in_over", 0)  # 0..5 legal balls already completed
    settled = ramp(state.get("striker_balls_faced", 0), 0, 25)
    if gap > 0:
        return {
            "refuse_single": MAX_SINGLE_REFUSAL
            * strength
            * settled
            * (1 - ramp(ball, 2, 5)),
            "seek_single": MAX_SINGLE_SEEK * strength * ramp(ball, 3, 5),
        }
    # A weaker batter can look for rotation, but receives a smaller boost.
    return {
        "refuse_single": 0.0,
        "seek_single": MAX_SINGLE_SEEK * 0.4 * strength * (1 - ramp(ball, 3, 5)),
    }


def apply_tail_protection(weights, policy):
    """Preserve total mass, wickets, extras and boundaries exactly."""
    result = dict(weights)
    refused = result.get("Single", 0) * bounded(policy.get("refuse_single", 0))
    sought = result.get("Dot", 0) * bounded(policy.get("seek_single", 0))
    result["Single"] = result.get("Single", 0) - refused + sought
    result["Dot"] = result.get("Dot", 0) + refused - sought
    return result
