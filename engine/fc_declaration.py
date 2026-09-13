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
Monte Carlo win-probability estimates in place of the flat lead-vs-threshold
check, when the caller supplies the strength/overs-remaining inputs they
need. Innings 2 uses estimate_lead_declaration_outcome because both sides
may genuinely bat again. Innings 3 uses estimate_target_defence_outcome:
the opposition's fourth-innings chase ends the match, so the declaring side
can never receive another innings. Innings 1 (raw score, nothing to compare
against yet, and up to 3 more innings still to play out) keeps the simpler
flat-threshold heuristic.
"""

import logging
import random as _random

from engine import fc_captain
from engine.fc_captain import DEFAULT_RISK_APPETITE


logger = logging.getLogger(__name__)

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

# A captain does not declare at nine down merely because the score has
# passed "a good total". At nine wickets the only thing still on offer is a
# last-wicket stand worth twenty-odd runs, so closing the innings early is a
# decision about TIME — and with three days still to play there is no time
# worth buying. Captains who do declare nine down on day two are sitting on
# a total that is already overwhelming, not one that is merely competitive.
#
# So when the last-pair trigger is the ONLY thing that opened the window
# (the innings is still inside its time budget and the match is not in its
# closing days), the score/lead bar is raised by this multiplier.
#
# Left at 1.0 this fired on essentially EVERY first innings that reached
# 300 with nine down — in scripts/bench_fc.py 100% of innings-1
# declarations came at exactly 9 wickets — which capped the all-out
# first-innings total below the threshold by construction and made "bowled
# out for under 300" look like the norm on every surface. Measured effect
# of raising it: see the FC section of scripts/bench_fc.py's output.
_LAST_PAIR_NO_PRESSURE_MULTIPLIER = 1.55

# A very long innings is its own kind of pressure. Past this many overs a
# captain is asking "have we got enough?" every over, whatever the wicket
# count — so the window opens here too, and the score threshold below
# decides. Without it the window opened ONLY at nine down, at the innings
# time budget (180 overs of a fresh five-day match) or in the closing days,
# which left a side 700 for 5 after 140 overs on day three with no way to
# declare at all. That is where the 900-run first innings on Dead came from.
#
# Set deliberately high (a side has batted a day and a half by here). At 100
# it truncated the distribution visibly — every long innings that cleared
# the score bar declared, so Hard's biggest all-out total in 100 matches was
# 459 against a bar of 465, and every pitch's mean dropped 10-15 runs. The
# point is to catch the runaway innings, not to cap the good ones.
_LONG_INNINGS_WINDOW_OVERS = 135

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


# ── Monte Carlo declaration models (innings 2/3 only — see module docstring) ─

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
# Recalibrated against the actual ball engine: FC now produces ~3.2 RPO
# overall (see scripts/bench_fc.py). The old 1.9 base modelled the
# opposition scoring at ~2.1 an over while the engine was really scoring at
# 4.5, so every declaration verdict was computed against a game that wasn't
# being played — the captain systematically over-estimated how easily he
# could contain a chase.
_MC_SCORE_RATE_BASE = 3.00
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

# A declaration only means anything from in front. Closing an innings while
# still behind hands the opposition a lead for nothing — yet the old model
# did exactly that on seaming pitches, where the MC read every position as
# hopeless and "declare" scored no worse than batting on. Expressed as a
# multiple of a par-ish first-innings score so it scales with the surface.
_MIN_LEAD_TO_DECLARE = 60


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


def _classify_target_defence_trial(*, target, overs_remaining_in_match,
                                   dismiss_overs, scoring_rate):
    """Classify one fourth-innings chase from the declaring side's view."""
    available_overs = max(0.0, overs_remaining_in_match)
    overs_played = min(dismiss_overs, available_overs)
    projected_runs = overs_played * scoring_rate

    # The chase ends the instant the target is reached. Even if the sampled
    # aggregate projection says "325 all out", a side chasing 311 won at
    # 311; there is no later innings for the declaring side.
    if projected_runs >= target:
        return "loss"
    if dismiss_overs <= available_overs:
        return "win"
    return "draw"


def estimate_target_defence_outcome(*, lead, overs_remaining_in_match,
                                    own_bowling_strength,
                                    opp_batting_strength, pitch_wear=0.5,
                                    trials=_MC_TRIALS, rng=None):
    """Estimate (win, draw, loss) after an innings-three declaration.

    The current lead becomes the opposition's fourth-innings target
    (lead + 1). Each aggregate trial runs only until the target is reached,
    ten wickets fall, or the available overs expire. The declaring side's
    batting strength is intentionally absent: it cannot bat again.
    """
    rng = rng or _random
    wins = draws = losses = 0
    target = lead + 1

    for _ in range(trials):
        dismiss_overs = _mc_sample_dismiss_overs(
            own_bowling_strength, pitch_wear, rng,
        )
        scoring_rate = _mc_sample_score_rate(opp_batting_strength, rng)
        result = _classify_target_defence_trial(
            target=target,
            overs_remaining_in_match=overs_remaining_in_match,
            dismiss_overs=dismiss_overs,
            scoring_rate=scoring_rate,
        )
        if result == "win":
            wins += 1
        elif result == "loss":
            losses += 1
        else:
            draws += 1

    return wins / trials, draws / trials, losses / trials


def _time_pressure(overs_bowled_this_innings, days_remaining,
                   innings_time_budget_overs) -> bool:
    """
    True when something other than the exposed last pair is pushing the
    captain to close the innings: this innings has used up its time budget,
    or the match itself is into its closing days with a long innings already
    behind it. These are exactly the two non-last-pair branches of
    declaration_window_open(), so `window open and not _time_pressure(...)`
    means "the only reason we are even asking is that we are nine down".
    """
    if (innings_time_budget_overs is not None
            and overs_bowled_this_innings >= innings_time_budget_overs):
        return True
    return (overs_bowled_this_innings >= _TIME_FORCING_OVERS_THRESHOLD
            and days_remaining <= _MAX_DAYS_REMAINING_FOR_DECLARE)


def declaration_window_open(*, fc_innings, wickets, overs_bowled_this_innings,
                             days_remaining, innings_time_budget_overs=None,
                             model_driven=False) -> bool:
    """
    True once the STRUCTURAL conditions for even considering a declaration
    are met: minimum overs faced, and either the tail is exposed
    (wickets==9, "protect the last pair"), the innings has simply gone on a
    very long time (_LONG_INNINGS_WINDOW_OVERS, whatever the wicket count),
    or enough time has passed to start "time-forcing" with a realistic
    amount of match still left. Does
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
    if model_driven:
        # With a value function behind the decision the window is purely a
        # legality/plausibility constraint: a captain does not declare in
        # the first few overs, and that is the only structural rule left.
        # The remaining clauses below exist because the flat-threshold rule
        # could not be trusted to be asked often — they kept it quiet until
        # the tail was exposed or the clock was nearly gone, by which time
        # the first innings had already run to 600. Asking a model that
        # prices batting on is the entire point, so ask it early and often.
        return True
    if wickets == 9:
        return True
    if (innings_time_budget_overs is not None
            and overs_bowled_this_innings >= innings_time_budget_overs):
        return True
    if overs_bowled_this_innings >= _LONG_INNINGS_WINDOW_OVERS:
        return True
    if overs_bowled_this_innings < _TIME_FORCING_OVERS_THRESHOLD:
        return False
    return days_remaining <= _MAX_DAYS_REMAINING_FOR_DECLARE


def should_declare(*, fc_innings, wickets, overs_bowled_this_innings,
                    score, lead, days_remaining, pitch_par_factor=1.0,
                    innings_time_budget_overs=None,
                    overs_remaining_in_match=None, own_bowling_strength=None,
                    own_batting_strength=None, opp_batting_strength=None,
                    pitch_wear=0.5, rng=None,
                    rain_risk=None, projected_final_wear=None,
                    attack_freshness=None,
                    pitch=None, own_strengths=None, opposition_strengths=None,
                    risk_appetite=None) -> bool:
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
    rain_risk                 : 0-1 share of the remaining match the forecast
                                 threatens. Time lost argues for declaring
                                 sooner — you may not get the overs back.
    projected_final_wear      : 0-1 pitch wear expected by the fourth
                                 innings. A surface that will be unplayable
                                 is a reason to declare and bowl on it.
    attack_freshness          : 0-1, 1.0 = a rested attack. There is no point
                                 declaring into a set of spent bowlers.
    innings_time_budget_overs : see compute_innings_time_budget_overs() /
                                 declaration_window_open(). Opens the window
                                 early (independent of days_remaining) once
                                 this innings has run past its budget, and —
                                 for the flat-threshold path below — linearly
                                 eases the score/lead bar the further past
                                 budget the innings runs. None (the default)
                                 preserves the exact prior behavior for any
                                 caller that doesn't supply it.

    Forward-model inputs (preferred, innings 2/3)
    ---------------------------------------------
    pitch, own_strengths, opposition_strengths, risk_appetite
        Supplying a pitch and both sides' strengths-by-wickets maps (see
        fc_captain.strengths_by_wickets) selects the fitted forward model,
        which compares declaring now against batting on for a set of
        horizons. risk_appetite is the captain's temperament; None uses the
        neutral default. Missing inputs fall through to the sampled
        estimator and then to the flat thresholds, unchanged.

    Monte Carlo inputs (innings 2/3 only, legacy fallback)
    ------------------------------------------------------
    overs_remaining_in_match, own_bowling_strength, own_batting_strength,
    opp_batting_strength, pitch_wear, rng
        Innings 2 requires all four strength/overs inputs and retains the
        bowl-then-possibly-chase model. Innings 3 requires overs remaining,
        own bowling strength, and opposition batting strength, and models
        only the final target defence; own batting strength is ignored.
        Missing inputs preserve the original flat-threshold fallback.
    """
    model_driven = (fc_innings in (1, 2, 3) and pitch is not None
                    and bool(own_strengths) and bool(opposition_strengths)
                    and overs_remaining_in_match is not None)

    if not declaration_window_open(
        fc_innings=fc_innings, wickets=wickets,
        overs_bowled_this_innings=overs_bowled_this_innings,
        days_remaining=days_remaining,
        innings_time_budget_overs=innings_time_budget_overs,
        model_driven=model_driven,
    ):
        return False

    # Innings 2/3 only: you declare from in front. Being behind and closing
    # the innings gives the opposition a lead for nothing. This is a rule of
    # the game rather than a judgement, so it binds the model too.
    if fc_innings in (2, 3) and lead < _MIN_LEAD_TO_DECLARE:
        return False

    if not model_driven and not (
            wickets >= _WICKETS_DOWN_FLOOR
            or overs_bowled_this_innings >= _OVERS_DOWN_FLOOR):
        return False

    innings_two_inputs = (
        overs_remaining_in_match, own_bowling_strength,
        own_batting_strength, opp_batting_strength,
    )
    innings_three_inputs = (
        overs_remaining_in_match, own_bowling_strength,
        opp_batting_strength,
    )
    # Preferred path: the fitted forward model (engine/fc_forecast.py) via
    # the value function in engine/fc_captain.py. It is chosen over the
    # sampled estimator below whenever the caller supplies squad strengths
    # and a pitch, because it prices the OVERS batting on would cost as well
    # as the runs it would gain. The sampled estimator could only ask
    # whether the lead was already big enough, so its one way to grow a lead
    # was to keep batting — which is how targets of 500+ were set.
    if model_driven:
        # The single most expensive thing the FC engine does: two measured
        # production calls took 52.6 s and 45.6 s. It is deliberately a plain
        # call here — should_declare stays pure so bench scripts and tests can
        # use it directly. Keeping it OFF the gevent hub is the Match layer's
        # job, which is also the only layer that knows whether the answer can
        # be deferred; see Match._fc_start_declaration_decision and
        # utils/cpu_offload.
        plan = fc_captain.evaluate_declaration(
            pitch=pitch, fc_innings=fc_innings,
            # Innings 1 has no lead to speak of yet: the runs on the board
            # ARE the position, and three innings still have to be played
            # out against them.
            lead=score if fc_innings == 1 else lead,
            overs_remaining=overs_remaining_in_match,
            own_strengths=own_strengths,
            opposition_strengths=opposition_strengths,
            wickets_in_hand=max(1, 10 - wickets),
            wear_start=pitch_wear,
            wear_end=projected_final_wear if projected_final_wear is not None
            else min(1.0, pitch_wear + 0.3),
            risk_appetite=(DEFAULT_RISK_APPETITE if risk_appetite is None
                           else risk_appetite),
        )
        if plan is not None:
            best, now = plan["best"], plan["now"]
            logger.debug(
                "FC declaration innings=%d lead=%d best_horizon=%+d value=%.3f "
                "(now: W%.2f D%.2f L%.2f) -> %s",
                fc_innings, lead, best["horizon"], best["value"],
                (now or best)["win"], (now or best)["draw"], (now or best)["loss"],
                "DECLARE" if plan["declare_now"] else "bat on")
            return plan["declare_now"]

    forecast_ready = (
        (fc_innings == 2 and all(v is not None for v in innings_two_inputs))
        or (fc_innings == 3 and all(v is not None for v in innings_three_inputs))
    )
    if forecast_ready:
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
        if fc_innings == 2:
            forecast_name = "innings_two_bowl_then_chase"
            win_prob, draw_prob, loss_prob = estimate_lead_declaration_outcome(
                lead=lead, overs_remaining_in_match=overs_remaining_in_match,
                own_bowling_strength=own_bowling_strength,
                own_batting_strength=own_batting_strength,
                opp_batting_strength=opp_batting_strength,
                pitch_wear=pitch_wear, rng=rng,
            )
        else:
            forecast_name = "innings_three_target_defence"
            win_prob, draw_prob, loss_prob = estimate_target_defence_outcome(
                lead=lead, overs_remaining_in_match=overs_remaining_in_match,
                own_bowling_strength=own_bowling_strength,
                opp_batting_strength=opp_batting_strength,
                pitch_wear=pitch_wear, rng=rng,
            )
        logger.debug(
            "FC declaration forecast=%s innings=%d win=%.3f draw=%.3f loss=%.3f",
            forecast_name, fc_innings, win_prob, draw_prob, loss_prob,
        )
        return (win_prob >= _MC_MIN_WIN_PROB
                and (win_prob - loss_prob) >= _MC_MIN_WIN_EDGE_OVER_LOSS)

    target_metric = score if fc_innings == 1 else lead
    base_threshold = _INNINGS1_BASE_THRESHOLD if fc_innings == 1 else _LEAD_BASE_THRESHOLD
    threshold = base_threshold * pitch_par_factor

    # Nine down, but nothing forcing the issue — see
    # _LAST_PAIR_NO_PRESSURE_MULTIPLIER. If the last pair is the only reason
    # this window is open, the innings still has its time budget in hand and
    # the match is not in its closing days, so the total has to be genuinely
    # overwhelming rather than merely good before the last wicket is thrown
    # away for a few extra overs.
    if not _time_pressure(overs_bowled_this_innings, days_remaining,
                          innings_time_budget_overs):
        threshold *= _LAST_PAIR_NO_PRESSURE_MULTIPLIER

    # A first-innings declaration is not a number being reached. A captain
    # declares on 250 at tea on day two if it is seaming and he fancies a
    # bowl, and bats past 550 on a road — the same score means different
    # things in different conditions. These read the conditions he can
    # actually see; each is optional, and omitting them all preserves the
    # original flat-threshold behaviour for existing callers.
    threshold *= _conditions_threshold_multiplier(
        rain_risk=rain_risk,
        projected_final_wear=projected_final_wear,
        attack_freshness=attack_freshness,
    )

    if innings_time_budget_overs is not None:
        overrun = overs_bowled_this_innings - innings_time_budget_overs
        if overrun > 0:
            decay_frac = min(1.0, overrun / _INNINGS_TIME_BUDGET_DECAY_OVERS)
            threshold *= 1.0 - decay_frac * (1.0 - _INNINGS_TIME_BUDGET_DECAY_FLOOR)
    return target_metric >= threshold


# How far the conditions can move the bar a captain declares at. Kept modest
# and clamped: this is a captain weighing what he sees, not a different
# heuristic.
_COND_MIN_MULTIPLIER = 0.70
_COND_MAX_MULTIPLIER = 1.25
# Rain about means overs are the scarce resource — get them in NOW.
_COND_RAIN_DISCOUNT = 0.22
# A pitch that will be unplayable by the fourth innings is a reason to
# declare early and let THEM bat last on it.
_COND_WEAR_DISCOUNT = 0.15
_COND_WEAR_THRESHOLD = 0.60
# A spent attack is a reason to bat on: there is no point setting up a
# declaration your bowlers cannot enforce.
_COND_TIRED_ATTACK_PREMIUM = 0.18


def _conditions_threshold_multiplier(*, rain_risk=None, projected_final_wear=None,
                                      attack_freshness=None) -> float:
    """Scale the score/lead a captain declares at, by what he can see.

    rain_risk            : 0-1, share of remaining play the forecast threatens
    projected_final_wear : 0-1 pitch wear expected by the fourth innings
    attack_freshness     : 0-1, 1.0 = a fully rested attack
    """
    mult = 1.0
    if rain_risk is not None and rain_risk > 0:
        mult -= _COND_RAIN_DISCOUNT * min(1.0, rain_risk)
    if projected_final_wear is not None and projected_final_wear >= _COND_WEAR_THRESHOLD:
        # Scaled by how far past the threshold it is.
        over = (projected_final_wear - _COND_WEAR_THRESHOLD) / (1.0 - _COND_WEAR_THRESHOLD)
        mult -= _COND_WEAR_DISCOUNT * min(1.0, max(0.0, over))
    if attack_freshness is not None and attack_freshness < 1.0:
        mult += _COND_TIRED_ATTACK_PREMIUM * (1.0 - min(1.0, max(0.0, attack_freshness)))
    return max(_COND_MIN_MULTIPLIER, min(_COND_MAX_MULTIPLIER, mult))


# Follow-on judgement (beyond the Law's bare eligibility test).
#
# Overs the attack has already bowled in the match past which a captain
# starts seriously weighing whether his bowlers can go again straight away.
_FO_TIRED_ATTACK_OVERS = 130.0
# ...and the point past which he'd rather bat again than ask them to.
_FO_EXHAUSTED_ATTACK_OVERS = 190.0
# Fourth-innings pitch wear past which batting last is genuinely unpleasant.
# Enforcing the follow-on means YOU bat last, so a pitch that will be
# breaking up is an argument against enforcing — the modern captain's
# reasoning, and the reason the follow-on has become rarer.
_FO_NASTY_LAST_INNINGS_WEAR = 0.72
# A deficit so large the opposition is unlikely to make you bat again at
# all; overrides the caution above.
_FO_OVERWHELMING_DEFICIT_MULT = 2.0


def should_enforce_follow_on(*, deficit, follow_on_margin, days_remaining,
                              attack_overs_bowled=None, projected_final_wear=None,
                              rain_risk=None, pitch=None, own_strengths=None,
                              opposition_strengths=None, pitch_wear=0.5,
                              overs_remaining_in_match=None,
                              attack_freshness=None, risk_appetite=None) -> bool:
    """
    Returns True if the side that batted first should enforce the
    follow-on, given the second side's deficit after being dismissed.

    The first two checks are the Law's eligibility test. Everything after
    is the judgement a captain actually makes, and every one of those
    inputs is optional — omitting them preserves the original
    enforce-whenever-legal behaviour.

    Parameters
    ----------
    deficit              : how many runs behind the second side finished
                            (innings1_total - innings2_total); must be >= 0
                            to even be considered
    follow_on_margin     : fmt.follow_on_margin (150 for 4-day, 200 for 5-day)
    days_remaining       : full match days left including today
    attack_overs_bowled  : overs this attack has bowled in the match so far.
                            A tired attack is the main reason to decline.
    projected_final_wear : pitch wear expected by the fourth innings (0-1).
                            Enforcing means batting last on it.
    rain_risk            : 0-1 chance of losing significant time to weather.
                            Time lost argues FOR enforcing — you may not get
                            another chance to bowl them out.
    """
    if deficit < follow_on_margin:
        return False
    if days_remaining < _MIN_DAYS_REMAINING_FOR_FOLLOW_ON:
        return False

    # Preferred path: weigh enforcing against batting again under the same
    # value function every other decision uses. The thresholds below remain
    # as the fallback for callers that cannot supply the model's inputs.
    if (pitch is not None and own_strengths and opposition_strengths
            and overs_remaining_in_match is not None):
        plan = fc_captain.evaluate_follow_on(
            pitch=pitch, deficit=deficit,
            overs_remaining=overs_remaining_in_match,
            own_strengths=own_strengths,
            opposition_strengths=opposition_strengths,
            wear_start=pitch_wear,
            wear_end=projected_final_wear if projected_final_wear is not None
            else min(1.0, pitch_wear + 0.3),
            risk_appetite=(DEFAULT_RISK_APPETITE if risk_appetite is None
                           else risk_appetite),
            attack_freshness=1.0 if attack_freshness is None else attack_freshness,
        )
        if plan is not None:
            logger.debug(
                "FC follow-on deficit=%d enforce_value=%.3f decline_value=%.3f "
                "fatigue_penalty=%.1f -> %s", deficit,
                plan["options"]["enforce"]["value"],
                plan["options"]["decline"]["value"], plan["fatigue_penalty"],
                "ENFORCE" if plan["enforce"] else "bat again")
            return plan["enforce"]

    # Crushing lead: make them follow on and be done with it.
    if deficit >= follow_on_margin * _FO_OVERWHELMING_DEFICIT_MULT:
        return True

    # Rain about means time is the scarce resource, not bowlers' legs.
    if rain_risk is not None and rain_risk >= 0.4:
        return True

    if attack_overs_bowled is not None:
        if attack_overs_bowled >= _FO_EXHAUSTED_ATTACK_OVERS:
            return False
        if (attack_overs_bowled >= _FO_TIRED_ATTACK_OVERS
                and projected_final_wear is not None
                and projected_final_wear >= _FO_NASTY_LAST_INNINGS_WEAR):
            # Tired attack AND we'd be batting last on a worn pitch — the
            # textbook modern decline.
            return False

    if (projected_final_wear is not None
            and projected_final_wear >= _FO_NASTY_LAST_INNINGS_WEAR
            and days_remaining >= 3):
        # Plenty of time left, so there's no need to take on a fourth-innings
        # chase on a breaking pitch; bat again and bury them instead.
        return False

    return True
