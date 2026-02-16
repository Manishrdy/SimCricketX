"""
Scenario-Based Simulation Engine
================================
Steers matches toward dramatic finishes by:
  1. Free Play (overs 0-14): Light probability nudges to keep match on trajectory
  2. Convergence (overs 15-17): Stronger nudges to reach target state for finale
  3. Finale (overs 18-19): Scripted ball-by-ball sequences for maximum drama

Supported scenarios:
  - last_ball_six: Chasing team wins by hitting a 6 on the final ball
  - win_by_1_run: Defending team wins; chasing team falls 1 run short
  - super_over_thriller: Match ties after 20 overs, triggers super over
"""

import random
import logging

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
#  Scenario definitions: target corridors and finale parameters
# --------------------------------------------------------------------------- #

SCENARIO_CONFIG = {
    "last_ball_six": {
        "label": "Last-Ball Six",
        # Ideal state at start of over 18 (12 balls left)
        "convergence_target": {
            "runs_needed_range": (20, 28),
            "wickets_range": (3, 6),
        },
        "finale_last_ball": {"runs": 6, "type": "run"},
    },
    "win_by_1_run": {
        "label": "Win by 1 Run",
        "convergence_target": {
            "runs_needed_range": (18, 24),
            "wickets_range": (5, 8),
        },
        "finale_last_ball": {"runs": 0, "type": "run"},  # dot ball to lose
    },
    "super_over_thriller": {
        "label": "Super Over Thriller",
        "convergence_target": {
            "runs_needed_range": (20, 26),
            "wickets_range": (4, 7),
        },
        "finale_last_ball": None,  # needs exact tie — dynamic
    },
}

# --------------------------------------------------------------------------- #
#  Wicket type helpers (mirrors ball_outcome.py distributions)
# --------------------------------------------------------------------------- #

PACE_WICKET_TYPES = ["Caught", "Bowled", "LBW", "Caught", "Caught"]
SPIN_WICKET_TYPES = ["Caught", "Stumped", "Bowled", "LBW", "Caught"]


def _pick_wicket_type(bowler):
    """Pick a realistic wicket type based on bowling style."""
    btype = bowler.get("bowling_type", "Medium")
    if btype in ("Fast", "Medium"):
        return random.choice(PACE_WICKET_TYPES)
    return random.choice(SPIN_WICKET_TYPES)


def _pick_fielder(bowling_team, bowler_name):
    """Pick a random fielder (not the bowler) for caught dismissals."""
    candidates = [p["name"] for p in bowling_team if p["name"] != bowler_name]
    return random.choice(candidates) if candidates else bowler_name


# --------------------------------------------------------------------------- #
#  Outcome dict builders
# --------------------------------------------------------------------------- #

def _make_run_outcome(runs, batter, bowler):
    """Build a standard run outcome dict."""
    desc_map = {
        0: f"Dot ball. {bowler['name']} keeps it tight.",
        1: f"{batter['name']} nudges for a single.",
        2: f"Good running! {batter['name']} takes two.",
        3: f"Excellent running between the wickets — three!",
        4: f"FOUR! {batter['name']} finds the boundary!",
        6: f"SIX! {batter['name']} launches it into the stands!",
    }
    return {
        "type": "run",
        "runs": runs,
        "description": desc_map.get(runs, f"{batter['name']} scores {runs}."),
        "wicket_type": None,
        "is_extra": False,
        "batter_out": False,
    }


def _make_wicket_outcome(batter, bowler, bowling_team, runs=0):
    """Build a wicket outcome dict."""
    wtype = _pick_wicket_type(bowler)
    if wtype == "Run Out":
        runs = 1
    fielder = ""
    if wtype == "Caught":
        fielder = _pick_fielder(bowling_team, bowler["name"])
        desc = f"OUT! {batter['name']} caught by {fielder} off {bowler['name']}!"
    elif wtype == "Bowled":
        desc = f"BOWLED! {bowler['name']} cleans up {batter['name']}!"
    elif wtype == "LBW":
        desc = f"LBW! {bowler['name']} traps {batter['name']} in front!"
    elif wtype == "Stumped":
        fielder = _pick_fielder(bowling_team, bowler["name"])
        desc = f"STUMPED! {batter['name']} stranded out of the crease!"
    elif wtype == "Run Out":
        fielder = _pick_fielder(bowling_team, bowler["name"])
        desc = f"RUN OUT! {batter['name']} is short of the crease!"
    else:
        desc = f"OUT! {batter['name']} is dismissed!"

    return {
        "type": "wicket",
        "runs": runs,
        "description": desc,
        "wicket_type": wtype,
        "is_extra": False,
        "batter_out": True,
        "fielder_out": fielder,
    }


# --------------------------------------------------------------------------- #
#  Finale script generators
# --------------------------------------------------------------------------- #

VALID_RUNS = (0, 1, 2, 3, 4, 6)  # Only legal run values in cricket


def _snap_to_valid(r):
    """Snap a run value to the nearest legal cricket outcome."""
    if r in VALID_RUNS:
        return r
    if r <= 0:
        return 0
    if r >= 6:
        return 6
    # 5 → 6 or 4 (prefer 4 to avoid inflation); other odd gaps similarly
    best = min(VALID_RUNS, key=lambda v: (abs(v - r), -v))
    return best


def _distribute_runs(target_runs, num_balls, include_wicket=True, include_boundary=True):
    """
    Generate a sequence of run values that sum to target_runs over num_balls.
    Every value is a legal cricket outcome: 0, 1, 2, 3, 4, or 6.
    Returns a list of (runs, is_wicket) tuples.
    """
    if num_balls <= 0 or target_runs < 0:
        return []

    sequence = []
    remaining = target_runs
    balls_left = num_balls

    # Place dramatic moments first
    wicket_pos = None
    boundary_pos = None

    if include_wicket and balls_left >= 4 and remaining >= 4:
        wicket_pos = random.randint(1, min(balls_left - 2, balls_left // 2 + 1))

    if include_boundary and balls_left >= 3 and remaining >= 8:
        candidates = [i for i in range(balls_left) if i != wicket_pos]
        if candidates:
            boundary_pos = random.choice(candidates[:len(candidates) // 2 + 1])

    for i in range(balls_left):
        balls_after = balls_left - i - 1

        if i == wicket_pos:
            # Run out with 1 run scored
            sequence.append((1, True))
            remaining -= 1
        elif i == boundary_pos and remaining >= 4:
            sequence.append((4, False))
            remaining -= 4
        elif remaining <= 0:
            sequence.append((0, False))
        else:
            # Pick a valid run value based on average needed
            avg_needed = remaining / max(1, balls_after + 1)
            if avg_needed <= 0.5:
                r = 0
            elif avg_needed <= 1.5:
                r = random.choice([0, 1, 1])
            elif avg_needed <= 2.5:
                r = random.choice([1, 1, 2])
            elif avg_needed <= 4:
                r = random.choice([1, 2, 2, 4])
            else:
                r = random.choice([2, 4, 4, 6])
            # Never exceed remaining, and snap to valid
            r = _snap_to_valid(min(r, remaining))
            sequence.append((r, False))
            remaining -= r

    # Final adjustment pass: fix any deficit/surplus using ONLY valid swaps
    # Try multiple passes to converge
    for _ in range(10):
        if remaining == 0:
            break
        adjusted = False
        # Iterate backwards for deficit (need more runs), forwards for surplus
        indices = range(len(sequence) - 1, -1, -1) if remaining > 0 else range(len(sequence))
        for i in indices:
            runs_val, is_wkt = sequence[i]
            if is_wkt:
                continue  # Don't touch wicket balls
            # Try each valid value that moves us closer to target
            for candidate in sorted(VALID_RUNS, reverse=(remaining > 0)):
                diff = candidate - runs_val
                if remaining > 0 and diff > 0 and diff <= remaining:
                    sequence[i] = (candidate, False)
                    remaining -= diff
                    adjusted = True
                    break
                elif remaining < 0 and diff < 0 and diff >= remaining:
                    sequence[i] = (candidate, False)
                    remaining -= diff
                    adjusted = True
                    break
            if remaining == 0:
                break
        if not adjusted:
            break

    # Validation: ensure every value is legal
    for i in range(len(sequence)):
        runs_val, is_wkt = sequence[i]
        if runs_val not in VALID_RUNS:
            sequence[i] = (_snap_to_valid(runs_val), is_wkt)

    return sequence


def _generate_last_ball_six_script(runs_needed, wickets_remaining, balls_left):
    """
    Script for 'last ball six': batter hits 6 on the final ball to win.
    Builds a sequence where all runs except 6 are scored in balls 1..(N-1),
    then a 6 on ball N.
    """
    if balls_left <= 1:
        # Only 1 ball left, just hit the six
        return [{"runs": 6, "is_wicket": False}]

    runs_before_last = runs_needed - 6
    if runs_before_last < 0:
        runs_before_last = 0

    include_wicket = wickets_remaining >= 4 and balls_left >= 6
    include_boundary = balls_left >= 5

    pre_sequence = _distribute_runs(
        runs_before_last, balls_left - 1,
        include_wicket=include_wicket,
        include_boundary=include_boundary
    )

    script = []
    for runs_val, is_wkt in pre_sequence:
        script.append({"runs": runs_val, "is_wicket": is_wkt})

    # The climactic final ball
    script.append({"runs": 6, "is_wicket": False})
    return script


def _generate_win_by_1_run_script(runs_needed, wickets_remaining, balls_left):
    """
    Script for 'win by 1 run': chasing team falls 1 run short.
    Score (runs_needed - 2) in balls 1..(N-1), then a dot on the last ball.
    They needed `runs_needed` but only scored `runs_needed - 1` total, losing by 1.
    """
    if balls_left <= 1:
        return [{"runs": 0, "is_wicket": False}]

    # They need to score runs_needed to win. We want them to score runs_needed - 1.
    # So in balls before last: score (runs_needed - 1), last ball: dot.
    # Wait — they need to NOT reach target. Target = runs_needed.
    # Total scored in finale = runs_needed - 1 means they fall 1 short.
    runs_before_last = runs_needed - 1
    if runs_before_last < 0:
        runs_before_last = 0

    include_wicket = wickets_remaining >= 5 and balls_left >= 6
    include_boundary = balls_left >= 5

    pre_sequence = _distribute_runs(
        runs_before_last, balls_left - 1,
        include_wicket=include_wicket,
        include_boundary=include_boundary
    )

    script = []
    for runs_val, is_wkt in pre_sequence:
        script.append({"runs": runs_val, "is_wicket": is_wkt})

    # Last ball: dot — agonizing defeat
    script.append({"runs": 0, "is_wicket": False})
    return script


def _generate_super_over_script(runs_needed, wickets_remaining, balls_left):
    """
    Script for 'super over thriller': match ties exactly.
    Score exactly (runs_needed - 1) across all balls so score == target - 1.
    """
    total_to_score = runs_needed - 1  # tie means score == target - 1
    if total_to_score < 0:
        total_to_score = 0

    include_wicket = wickets_remaining >= 5 and balls_left >= 6

    sequence = _distribute_runs(
        total_to_score, balls_left,
        include_wicket=include_wicket,
        include_boundary=(balls_left >= 5)
    )

    script = []
    for runs_val, is_wkt in sequence:
        script.append({"runs": runs_val, "is_wicket": is_wkt})
    return script


# --------------------------------------------------------------------------- #
#  Main ScenarioEngine class
# --------------------------------------------------------------------------- #

class ScenarioEngine:
    """
    Manages scenario-based match steering.
    Attached to a Match instance when scenario_mode is set.
    """

    def __init__(self, scenario_type, match):
        self.scenario_type = scenario_type
        self.match = match
        self.config = SCENARIO_CONFIG.get(scenario_type, {})
        self.finale_script = None
        self.finale_ball_index = 0
        self.active = True
        self._convergence_logged = False

        logger.info(f"[Scenario] Initialized: {scenario_type}")

    def on_innings_transition(self):
        """Called when innings transitions from 1st to 2nd."""
        self.finale_script = None
        self.finale_ball_index = 0
        self._convergence_logged = False
        logger.info(f"[Scenario] Innings transition — ready for 2nd innings steering")

    # ------------------------------------------------------------------ #
    #  Phase detection
    # ------------------------------------------------------------------ #

    def get_phase(self):
        """Return the current scenario phase."""
        if self.match.innings == 1:
            return "first_innings"
        if not self.active:
            return "inactive"

        over = self.match.current_over
        if over < 15:
            return "free_play"
        elif over < 18:
            return "convergence"
        else:
            return "finale"

    # ------------------------------------------------------------------ #
    #  Bias for free-play and convergence phases
    # ------------------------------------------------------------------ #

    def get_scenario_bias(self, match_state):
        """
        Return pressure_effects modifications for the current phase.
        Returns a dict of multiplicative modifiers to merge into pressure_effects.
        Only active during 2nd innings, free_play and convergence phases.
        """
        phase = self.get_phase()

        if phase in ("first_innings", "inactive", "finale"):
            return {}

        if self.match.innings != 2 or self.match.target is None:
            return {}

        runs_needed = self.match.target - self.match.score
        balls_remaining = (20 - self.match.current_over) * 6 - self.match.current_ball
        wickets_remaining = 10 - self.match.wickets

        if balls_remaining <= 0:
            return {}

        target = self.config.get("convergence_target", {})
        ideal_rn_low, ideal_rn_high = target.get("runs_needed_range", (20, 28))
        ideal_wk_low, ideal_wk_high = target.get("wickets_range", (3, 6))

        # Calculate how many balls until finale (over 18)
        balls_to_finale = max(1, (18 - self.match.current_over) * 6 - self.match.current_ball)

        # Ideal runs needed at start of finale
        ideal_runs_needed = (ideal_rn_low + ideal_rn_high) / 2
        ideal_wickets_in_hand = 10 - (ideal_wk_low + ideal_wk_high) / 2

        # Current trajectory: where will we be at over 18 at current rate?
        if balls_remaining > balls_to_finale:
            current_rr = self.match.score / max(1, self.match.current_over * 6 + self.match.current_ball)
            projected_score_at_18 = self.match.score + current_rr * balls_to_finale
            projected_rn_at_18 = self.match.target - projected_score_at_18
        else:
            projected_rn_at_18 = runs_needed

        bias = {}

        if phase == "free_play":
            # Light nudges: ±15%
            strength = 0.15
        else:
            # Convergence: ±30%
            strength = 0.30
            if not self._convergence_logged:
                logger.info(f"[Scenario] Convergence started: need={runs_needed}, wkts={wickets_remaining}, target_rn=({ideal_rn_low}-{ideal_rn_high})")
                self._convergence_logged = True

        # Scoring bias: if team is too far ahead, slow them; if behind, speed up
        if projected_rn_at_18 < ideal_rn_low:
            # Team scoring too fast — reduce boundaries, boost dots
            bias["boundary_modifier"] = 1 - strength
            bias["dot_bonus"] = 0.03 * (strength / 0.15)
        elif projected_rn_at_18 > ideal_rn_high:
            # Team scoring too slow — boost boundaries, reduce dots
            bias["boundary_modifier"] = 1 + strength
            bias["dot_bonus"] = -0.02 * (strength / 0.15)

        # Wicket bias
        current_wickets = self.match.wickets
        if current_wickets < ideal_wk_low and phase == "convergence":
            # Not enough wickets — slightly boost
            bias["wicket_modifier"] = 1 + strength * 0.5
        elif current_wickets > ideal_wk_high:
            # Too many wickets — reduce
            bias["wicket_modifier"] = 1 - strength * 0.7

        return bias

    # ------------------------------------------------------------------ #
    #  Finale: scripted outcome overrides
    # ------------------------------------------------------------------ #

    def get_override_outcome(self, batter, bowler):
        """
        During finale phase, return a scripted outcome dict.
        Returns None if not in finale phase or script is exhausted.
        """
        if self.get_phase() != "finale":
            return None

        # Generate script on first call
        if self.finale_script is None:
            self._generate_finale_script()
            if self.finale_script is None:
                # Couldn't generate — fall back to normal sim
                self.active = False
                logger.warning("[Scenario] Could not generate finale script, falling back to normal")
                return None

        # Check if we have balls left in the script
        if self.finale_ball_index >= len(self.finale_script):
            return None

        ball_spec = self.finale_script[self.finale_ball_index]
        self.finale_ball_index += 1

        # Build the outcome dict
        if ball_spec.get("is_wicket"):
            outcome = _make_wicket_outcome(batter, bowler, self.match.bowling_team, runs=1)
        else:
            outcome = _make_run_outcome(ball_spec["runs"], batter, bowler)

        logger.debug(f"[Scenario] Finale ball {self.finale_ball_index}: {outcome['runs']}r {'W' if outcome.get('batter_out') else ''}")
        return outcome

    def _generate_finale_script(self):
        """Build the finale ball sequence based on current match state."""
        runs_needed = self.match.target - self.match.score
        wickets_remaining = 10 - self.match.wickets
        balls_left = (20 - self.match.current_over) * 6 - self.match.current_ball

        logger.info(f"[Scenario] Generating finale: type={self.scenario_type}, "
                     f"runs_needed={runs_needed}, wickets={wickets_remaining}, balls={balls_left}")

        # Sanity checks
        if balls_left <= 0 or wickets_remaining <= 0:
            self.finale_script = None
            return

        if self.scenario_type == "last_ball_six":
            self._generate_last_ball_six(runs_needed, wickets_remaining, balls_left)
        elif self.scenario_type == "win_by_1_run":
            self._generate_win_by_1_run(runs_needed, wickets_remaining, balls_left)
        elif self.scenario_type == "super_over_thriller":
            self._generate_super_over(runs_needed, wickets_remaining, balls_left)
        else:
            self.finale_script = None

    def _generate_last_ball_six(self, runs_needed, wickets_remaining, balls_left):
        """Last-Ball Six: hit 6 on the very last ball to win."""
        # We need runs_needed >= 6 for this to work
        if runs_needed < 6:
            # Team is too close — still do a big finish, just adjust
            # Make them need exactly 6: add dots to burn balls
            dots_needed = balls_left - 1
            script = [{"runs": 0, "is_wicket": False}] * dots_needed
            # If they already passed target minus 6, we need a wicket or two
            # to make it tense. But for simplicity, let them cruise and hit a 6
            script.append({"runs": min(runs_needed, 6), "is_wicket": False})
            self.finale_script = script
            return

        if runs_needed > balls_left * 5:
            # Too far behind — can't realistically script this
            logger.warning(f"[Scenario] Last-ball six impossible: need {runs_needed} off {balls_left}")
            self.finale_script = None
            return

        self.finale_script = _generate_last_ball_six_script(
            runs_needed, wickets_remaining, balls_left
        )

    def _generate_win_by_1_run(self, runs_needed, wickets_remaining, balls_left):
        """Win by 1 run: chasing team falls 1 run short."""
        if runs_needed <= 0:
            # Already won — can't do this scenario
            self.finale_script = None
            return

        if runs_needed > balls_left * 5:
            logger.warning(f"[Scenario] Win-by-1-run impossible: need {runs_needed} off {balls_left}")
            self.finale_script = None
            return

        self.finale_script = _generate_win_by_1_run_script(
            runs_needed, wickets_remaining, balls_left
        )

    def _generate_super_over(self, runs_needed, wickets_remaining, balls_left):
        """Super Over Thriller: match ties exactly."""
        if runs_needed <= 0:
            self.finale_script = None
            return

        if runs_needed - 1 > balls_left * 5:
            logger.warning(f"[Scenario] Super-over tie impossible: need {runs_needed} off {balls_left}")
            self.finale_script = None
            return

        self.finale_script = _generate_super_over_script(
            runs_needed, wickets_remaining, balls_left
        )
