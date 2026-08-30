"""
engine/fc_declaration.py
==========================

Rule-based declaration and follow-on heuristics for First-Class (FC)
matches. Pure functions — mirrors engine/weather.py's "state in, outcome
out" shape — called automatically from Match._fc_next_ball() at an over
boundary, the same way _check_rain_events() runs automatically without
touching `pending_decision`. Per the agreed Phase 1 scope, the AI always
decides; there is no user-captained override yet.

Innings 1, 2, and a follow-on-free innings 3 are eligible to declare — a
side already chasing a set target (innings 4, or a follow-on team's
innings 3) never declares, since there's nothing to declare *to*. Innings 1
uses raw score as its "is this enough" metric (nothing to compare against
yet); innings 2 and innings 3 both use lead over the first completed
innings, since by that point there's an actual target the declaring side
is trying to set.

Thresholds are gathered here in one place (mirroring
`bowler_manager._FATIGUE_TABLE`'s "one place to tune" pattern) so they can
be adjusted after playtesting without touching control flow. Treat them as
a documented starting point, not a final tuning.

Declaring from innings 2/3 (setting an actual target) additionally supports
a Monte Carlo win-probability estimate (see estimate_lead_declaration_outcome
below) in place of the flat lead-vs-threshold check, when the caller
supplies the strength/overs-remaining inputs it needs. Innings 1 (raw
score, nothing to compare against yet, and up to 3 more innings still to
play out) keeps the simpler flat-threshold heuristic — modeling "will we
win the whole rest of the match" is a different, much larger problem than
"can we bowl this specific opposition innings out and defend/chase the
target," which is what the Monte Carlo model actually estimates.
"""

import random as _random

# Minimum overs into the innings before a declaration is even considered —
# a captain doesn't declare inside the first few overs regardless of score.
_MIN_OVERS_BEFORE_DECLARE = 20

# Overs threshold past which a "time-forcing" declaration becomes possible
# even without the last-pair trigger below.
_TIME_FORCING_OVERS_THRESHOLD = 60

# Base run thresholds (before pitch scaling via fmt.pitch_par_factors) for
# a "big enough" total/lead to justify a time-forcing declaration.
_INNINGS1_BASE_THRESHOLD = 300
_LEAD_BASE_THRESHOLD = 250

# Wickets-down floor that, combined with the overs floor, also counts as
# "big enough" even short of the run threshold — a long tail isn't worth
# grinding through once the score is already competitive.
_WICKETS_DOWN_FLOOR = 6
_OVERS_DOWN_FLOOR = 100

# Days-remaining ceiling — declaring makes sense only when there's still
# enough of the match left to bowl the opposition out twice (or once, in
# the follow-on-free innings-3 case).
_MAX_DAYS_REMAINING_FOR_DECLARE = 2
_MIN_DAYS_REMAINING_FOR_FOLLOW_ON = 2

# ── Per-innings time budget (independent of whole-match days-remaining) ─────
# The days_remaining <= 2 gate above only creates time-forcing pressure in
# the match's last 2 days — on Flat/Dead pitches, where the per-ball wicket
# probability is low enough that a side can realistically bat 150-300+
# overs without losing its 9th wicket, that gate never engages until an
# innings has already run away to 650-1200+ runs. This budget creates the
# same kind of pressure much earlier, scaled to whatever's actually left in
# the match at the moment THIS innings started — see
# compute_innings_time_budget_overs() and Match._fc_start_next_innings()/
# __init__, which capture it once per innings.

# Fraction of the overs remaining in the whole match (at the instant this
# innings starts) allotted to it before time-forcing pressure begins. 0.40
# of a fresh 5-day match's 450 overs is 180 overs (~2 days) — matching how
# long a first innings can realistically occupy before a real captain
# declares. A later-starting innings (2/3) automatically gets a tighter
# budget, since overs_remaining_in_match is whatever's actually left by
# then, not a fixed constant.
_INNINGS_TIME_BUDGET_FRACTION = 0.40

# Overs past the time budget over which the flat score/lead threshold
# linearly decays from 100% down to _INNINGS_TIME_BUDGET_DECAY_FLOOR of its
# pitch-scaled value — a marginal total becomes "good enough" the longer an
# innings grinds on past its budget, instead of requiring the full bar
# forever (a hard cutoff instead would either declare too early on a side
# fractionally short of a real total, or do nothing for a genuinely
# stalled low-scoring innings, depending on where the line is drawn).
_INNINGS_TIME_BUDGET_DECAY_OVERS = 60.0

# Floor the decayed threshold can't fall below (fraction of the full
# pitch-scaled threshold) — an overrun innings still needs a genuinely
# competitive score/lead, not literally anything.
_INNINGS_TIME_BUDGET_DECAY_FLOOR = 0.55

# Once an innings has overrun its time budget by this multiple, even a
# Monte Carlo verdict of "don't declare, no realistic time to force a
# result" is overridden as long as there's any lead at all. Without this, a
# genuinely bad matchup/time situation lets an innings 2/3 bat indefinitely
# once the budget-based window opens early — the MC model is legitimately
# telling the truth about win probability, but real captains still declare
# eventually (over-rate/fatigue/sportsmanship) rather than batting to a
# dead stop once forcing a win is off the table.
_MC_OVERRUN_CEILING_MULTIPLIER = 1.5


def compute_innings_time_budget_overs(overs_remaining_in_match) -> float:
    """
    The per-innings time budget in overs (see declaration_window_open's
    innings_time_budget_overs branch and should_declare's threshold decay).
    Callers (Match.__init__ for innings 1, Match._fc_start_next_innings()
    for innings 2-4) compute this exactly once, at the moment the innings
    starts, from however many overs are left in the WHOLE match at that
    instant — so an innings that starts late (because an earlier one ran
    long) automatically gets a correspondingly tighter budget, without any
    extra bookkeeping here.
    """
    return _INNINGS_TIME_BUDGET_FRACTION * overs_remaining_in_match


# ── Monte Carlo declaration model (innings 2/3 only — see module docstring) ─

_MC_TRIALS = 400

# Overs to bowl the opposition out ten times over, sampled from a normal
# distribution around a bowling-strength-derived mean (bowling_rating is
# 0-100, same scale as every other rating in this codebase). A weak attack
# (rating ~30) averages ~161 overs to take 10 wickets; a strong one
# (rating ~90) averages ~83. Reduced by pitch wear (more assistance late)
# and floored so even a very strong attack can't bowl a side out instantly.
_MC_DISMISS_OVERS_INTERCEPT = 200.0
_MC_DISMISS_OVERS_SLOPE = 1.3
_MC_DISMISS_OVERS_MIN = 30.0
_MC_DISMISS_OVERS_WEAR_DISCOUNT = 0.25   # up to 25% fewer overs on a fully worn pitch
_MC_DISMISS_OVERS_STD_FRAC = 0.22        # sample stddev as a fraction of the mean

# Scoring rate (runs/over) while an opposition batter survives — FC is
# dot/single-dominated (see format_config.py), so this stays well below
# T20/ListA rates regardless of batting strength.
_MC_SCORE_RATE_BASE = 1.9
_MC_SCORE_RATE_STRENGTH_SCALE = 100.0    # +1 run/over per +100 batting_rating above 50
_MC_SCORE_RATE_STD = 0.5

# A chase needs a minimum window to be a real chase at all, not a token
# faster/slower opposition dismissal is a draw (survived, but too late to
# do anything meaningful with the extra overs).
_MC_MIN_CHASE_OVERS = 5.0

# Required run rate past which pursuing the target risks losing wickets in
# a rush rather than settling for the draw — collapse_chance ramps linearly
# above this and is capped so even a very steep chase isn't a certain loss.
_MC_RECKLESS_CHASE_RATE = 5.0
_MC_COLLAPSE_CHANCE_PER_RATE = 0.15
_MC_COLLAPSE_CHANCE_CAP = 0.6

# Declare only when the simulated outcome clearly favors it — a coin-flip
# or worse doesn't justify giving up batting time.
_MC_MIN_WIN_PROB = 0.35
_MC_MIN_WIN_EDGE_OVER_LOSS = 0.15


def _mc_sample_dismiss_overs(bowling_strength, pitch_wear, rng):
    mean = max(
        _MC_DISMISS_OVERS_MIN,
        _MC_DISMISS_OVERS_INTERCEPT - bowling_strength * _MC_DISMISS_OVERS_SLOPE,
    )
    mean *= 1.0 - _MC_DISMISS_OVERS_WEAR_DISCOUNT * max(0.0, min(1.0, pitch_wear))
    return max(_MC_DISMISS_OVERS_MIN, rng.gauss(mean, mean * _MC_DISMISS_OVERS_STD_FRAC))


def _mc_sample_score_rate(batting_strength, rng):
    mean = _MC_SCORE_RATE_BASE + (batting_strength - 50) / _MC_SCORE_RATE_STRENGTH_SCALE
    return max(0.5, rng.gauss(mean, _MC_SCORE_RATE_STD))


def estimate_lead_declaration_outcome(*, lead, overs_remaining_in_match,
                                       own_bowling_strength, own_batting_strength,
                                       opp_batting_strength, pitch_wear=0.5,
                                       trials=_MC_TRIALS, rng=None):
    """
    Monte Carlo estimate of (win_prob, draw_prob, loss_prob) if the batting
    side declares RIGHT NOW with the given lead.

    Each trial samples how many overs it takes to bowl the opposition's
    last innings out and how many runs they score doing it, then — if they
    passed our lead — samples our own run-chase for the target they've set
    in whatever overs remain. This is deliberately an aggregate statistical
    model (sampled overs/runs, not a nested ball-by-ball simulation): a full
    Match rollout per trial would be far too slow to run at a live over
    boundary, and the aggregate quantities that actually drive the
    decision — "will there be time to bowl them out, and can we defend or
    chase what's left" — don't need per-ball fidelity to estimate well.

    *_strength params are 0-100 (bowling_rating/batting_rating scale).
    """
    rng = rng or _random
    wins = draws = losses = 0
    for _ in range(trials):
        dismiss_overs = _mc_sample_dismiss_overs(own_bowling_strength, pitch_wear, rng)
        if dismiss_overs >= overs_remaining_in_match:
            draws += 1  # ran out of time to even take the 10th wicket
            continue

        opp_total = dismiss_overs * _mc_sample_score_rate(opp_batting_strength, rng)
        if opp_total < lead:
            wins += 1  # dismissed for less than our lead -> innings/runs win
            continue

        overs_for_chase = overs_remaining_in_match - dismiss_overs
        target = opp_total - lead + 1
        if overs_for_chase < _MC_MIN_CHASE_OVERS:
            draws += 1  # they passed our lead, but no realistic time to chase
            continue

        required_rate = target / overs_for_chase
        if required_rate > _MC_RECKLESS_CHASE_RATE:
            collapse_chance = min(
                _MC_COLLAPSE_CHANCE_CAP,
                (required_rate - _MC_RECKLESS_CHASE_RATE) * _MC_COLLAPSE_CHANCE_PER_RATE,
            )
            if rng.random() < collapse_chance:
                losses += 1
            else:
                draws += 1
            continue

        our_total = overs_for_chase * _mc_sample_score_rate(own_batting_strength, rng)
        if our_total >= target:
            wins += 1
        else:
            draws += 1  # fell short but time (not wickets) ran out first

    return wins / trials, draws / trials, losses / trials


def declaration_window_open(*, fc_innings, wickets, overs_bowled_this_innings,
                             days_remaining, innings_time_budget_overs=None) -> bool:
    """
    True once the STRUCTURAL conditions for even considering a declaration
    are met: minimum overs faced, and either the tail is exposed
    (wickets==9, "protect the last pair") or enough time has passed to
    start "time-forcing" with a realistic amount of match still left. Does
    NOT judge whether the score/lead itself is worth declaring on — that's
    should_declare()'s (AI mode) or the user-captained UI's job once this
    window is open.

    Shared by should_declare() and Match's user-captained "should we ask
    the captain about declaring right now" trigger, so the two can never
    drift out of sync about *when* a declaration becomes a live decision —
    only *who* (AI heuristic vs a human) makes the actual call once it is.

    innings_time_budget_overs, when supplied, also opens the window once
    overs_bowled_this_innings reaches it — independent of days_remaining,
    so a runaway innings on a low-wicket-probability pitch doesn't have to
    wait for the whole match's last 2 days before time-forcing pressure can
    even be considered. None (the default) preserves the exact prior
    behavior for any caller that doesn't know about this parameter.
    """
    if fc_innings not in (1, 2, 3):
        return False
    if overs_bowled_this_innings < _MIN_OVERS_BEFORE_DECLARE:
        return False
    if wickets == 9:
        return True
    if (innings_time_budget_overs is not None
            and overs_bowled_this_innings >= innings_time_budget_overs):
        return True
    if overs_bowled_this_innings < _TIME_FORCING_OVERS_THRESHOLD:
        return False
    return days_remaining <= _MAX_DAYS_REMAINING_FOR_DECLARE


def should_declare(*, fc_innings, wickets, overs_bowled_this_innings,
                    score, lead, days_remaining, pitch_par_factor=1.0,
                    innings_time_budget_overs=None,
                    overs_remaining_in_match=None, own_bowling_strength=None,
                    own_batting_strength=None, opp_batting_strength=None,
                    pitch_wear=0.5, rng=None) -> bool:
    """
    Returns True if the AI captain should declare the current innings
    closed right now (called at an over boundary only).

    Parameters
    ----------
    fc_innings                : 1-4, current innings number
    wickets                   : wickets down in the current innings
    overs_bowled_this_innings : overs completed in the current innings
    score                     : current innings score (relevant for
                                 innings 1, where there's no "lead" yet)
    lead                      : batting side's lead over the opposition's
                                 completed innings total (relevant for
                                 innings 2, and innings 3 in the
                                 no-follow-on case)
    days_remaining            : full match days left including today
    pitch_par_factor          : fmt.pitch_par_factors[pitch] — scales the
                                 run thresholds so "a good total" means
                                 something different on a Green seamer
                                 than a Dead belter
    innings_time_budget_overs : see compute_innings_time_budget_overs() /
                                 declaration_window_open(). Opens the window
                                 early (independent of days_remaining) once
                                 this innings has run past its budget, and —
                                 for the flat-threshold path below — linearly
                                 eases the score/lead bar the further past
                                 budget the innings runs. None (the default)
                                 preserves the exact prior behavior for any
                                 caller that doesn't supply it.

    Monte Carlo inputs (innings 2/3 only — see estimate_lead_declaration_outcome)
    ----------------------------------------------------------------------
    overs_remaining_in_match, own_bowling_strength, own_batting_strength,
    opp_batting_strength, pitch_wear, rng
        When all four strength/overs inputs are supplied, the lead-vs-flat-
        threshold check below is replaced by a simulated win/draw/loss
        estimate — a genuine model of "is there time to bowl them out and
        defend/chase this lead," rather than a static run number. Any
        caller that omits them (e.g. existing unit tests, or innings 1,
        which never uses this path at all) gets the original flat-threshold
        behavior unchanged.
    """
    if not declaration_window_open(
        fc_innings=fc_innings, wickets=wickets,
        overs_bowled_this_innings=overs_bowled_this_innings,
        days_remaining=days_remaining,
        innings_time_budget_overs=innings_time_budget_overs,
    ):
        return False

    if not (wickets >= _WICKETS_DOWN_FLOOR or overs_bowled_this_innings >= _OVERS_DOWN_FLOOR):
        return False

    _mc_inputs = (overs_remaining_in_match, own_bowling_strength,
                  own_batting_strength, opp_batting_strength)
    if fc_innings != 1 and all(v is not None for v in _mc_inputs):
        # overs_remaining_in_match keeps shrinking the longer this innings
        # overruns its budget, which LOWERS the Monte Carlo model's win
        # probability (less time = harder to force a result) — the opposite
        # of the pressure we want. Without this ceiling, a genuinely bad
        # matchup/time situation would let an innings bat indefinitely once
        # the budget-based window is open early. Cheaper than the 400-trial
        # simulation too, so it's checked first.
        if (innings_time_budget_overs is not None
                and overs_bowled_this_innings >= innings_time_budget_overs * _MC_OVERRUN_CEILING_MULTIPLIER
                and lead > 0):
            return True
        win_prob, _draw_prob, loss_prob = estimate_lead_declaration_outcome(
            lead=lead, overs_remaining_in_match=overs_remaining_in_match,
            own_bowling_strength=own_bowling_strength,
            own_batting_strength=own_batting_strength,
            opp_batting_strength=opp_batting_strength,
            pitch_wear=pitch_wear, rng=rng,
        )
        return (win_prob >= _MC_MIN_WIN_PROB
                and (win_prob - loss_prob) >= _MC_MIN_WIN_EDGE_OVER_LOSS)

    target_metric = score if fc_innings == 1 else lead
    base_threshold = _INNINGS1_BASE_THRESHOLD if fc_innings == 1 else _LEAD_BASE_THRESHOLD
    threshold = base_threshold * pitch_par_factor
    if innings_time_budget_overs is not None:
        overrun = overs_bowled_this_innings - innings_time_budget_overs
        if overrun > 0:
            decay_frac = min(1.0, overrun / _INNINGS_TIME_BUDGET_DECAY_OVERS)
            threshold *= 1.0 - decay_frac * (1.0 - _INNINGS_TIME_BUDGET_DECAY_FLOOR)
    return target_metric >= threshold


def should_enforce_follow_on(*, deficit, follow_on_margin, days_remaining) -> bool:
    """
    Returns True if the side that batted first should enforce the
    follow-on, given the second side's deficit after being dismissed.

    Parameters
    ----------
    deficit          : how many runs behind the second side finished
                        (innings1_total - innings2_total); must be >= 0
                        to even be considered
    follow_on_margin : fmt.follow_on_margin (150 for 4-day, 200 for 5-day)
    days_remaining   : full match days left including today
    """
    if deficit < follow_on_margin:
        return False
    return days_remaining >= _MIN_DAYS_REMAINING_FOR_FOLLOW_ON
