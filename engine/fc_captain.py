"""Match-level decisions from the forward model.

One value function, consulted by every decision:

    V = P(win) + draw_value * P(draw)

A draw is worth something, but distinctly less than a win — the ratio is
roughly the county championship's own (a draw pays about 5 against a win's
16). Scoring a draw at zero instead makes a captain strictly prefer a
certain draw to any gamble at all, so it bats on until the game is dead;
that is precisely the behaviour that produced 700-run first innings.

Because win + draw + loss = 1, this is equivalent to penalising defeat at
draw_value / (1 - draw_value) — losses are priced, they are simply not
priced so highly that trying to win is never worth it.

`risk_appetite` is the captain's temperament, scaling what a draw is worth:
below 1 it backs itself and plays for the win, above 1 it takes the draw. It
replaces the fixed `_MC_MIN_WIN_PROB` / `_MC_MIN_WIN_EDGE_OVER_LOSS` pair,
under which every captain on every surface had identical nerve.

Everything here is deterministic and consumes no RNG. Declaring is evaluated
against the alternative of batting on, so the cost of the overs those extra
runs consume is priced — which is the thing the old lead-versus-threshold
check could not see.
"""
import functools
import logging

import numpy as np

from engine.fc_decision_budget import check_budget
from engine.fc_batting_intent import ability
from engine.fc_forecast import chase_curves, innings_forecast

logger = logging.getLogger(__name__)

DEFAULT_RISK_APPETITE = 1.0

# What a draw is worth relative to a win, before the captain's temperament
# scales it. 0.30 is the county championship's draw-to-win points ratio.
DRAW_VALUE = 0.30

# Aggression levels considered when a side picks how to play an innings.
# Coarse on purpose: the frontier is smooth, and the decision is about which
# region to sit in rather than a third decimal place.
TEMPOS = (-1.0, -0.5, 0.0, 0.5, 1.0)

# How far ahead a declaring captain looks, in overs. 0 is "declare now".
# Fixed short horizons give precision near the decision; the fractions of
# whatever is left in the match supply the long options, including batting
# out most of the remaining time. Without those long options the captain
# cannot represent "kill the game", every candidate looks worse than the
# next one along, and it bats to the end of the list every time.
_SHORT_HORIZONS = (0, 10, 20, 30, 45, 60)
_HORIZON_FRACTIONS = (0.35, 0.5, 0.7, 0.9)


def horizons_for(overs_remaining):
    overs_remaining = max(0, int(overs_remaining))
    candidates = set(_SHORT_HORIZONS)
    candidates.update(int(overs_remaining * f) for f in _HORIZON_FRACTIONS)
    return tuple(sorted(h for h in candidates if h < overs_remaining))

# Chase forecasts are cached on overs rounded to this, which bounds how many
# distinct dynamic programmes one decision can trigger.
OVERS_GRANULARITY = 10

# Value given up per over of batting on. Small, but it is what breaks a flat
# value surface toward declaring.
#
# On a road every option can evaluate to the same near-certain draw, and with
# nothing to separate them the captain occupies the crease for another eighty
# overs for no gain — which is how a first innings reaches 846. Real captains
# are not indifferent there: over-rates, tiring an attack that has to bowl
# next, weather risk and plain sportsmanship all argue for getting on with
# it. Before the forward model this job was done by fc_declaration's
# _LONG_INNINGS_WINDOW_OVERS gate, which the model path no longer consults.
#
# Size it against the value differences it competes with, which are usually
# 0.02-0.05. At 0.0006 it was worth 0.12 over a 200-over horizon, swamped
# them, and captains declared in 83% of first innings on Flat — the draw
# rate fell to 5%. At 0.00025 it still separates a genuinely flat surface
# but no longer outvotes a real chance of winning.
TIME_PREFERENCE = 0.00025


def value(win, loss, risk_appetite=DEFAULT_RISK_APPETITE):
    draw = max(0.0, 1.0 - win - loss)
    return win + DRAW_VALUE * risk_appetite * draw


def strengths_by_wickets(batting_order, bowling_attack):
    """Batting-minus-bowling strength for every wickets-in-hand state.

    With w wickets in hand the last w+1 batters remain, so a side eight down
    is modelled as its tail rather than as its team average.
    """
    attack = [p for p in bowling_attack if p.get("will_bowl")] or list(bowling_attack)
    bowling = (sum(p.get("bowling_rating", 50) for p in attack) / len(attack)
               if attack else 50.0)
    order = list(batting_order)
    total = len(order)
    out = {}
    for w in range(1, 11):
        remaining = order[max(0, total - (w + 1)):] or order
        out[w] = sum(ability(p) for p in remaining) / len(remaining) - bowling
    return out


def remaining_strengths_by_wickets(current_batters, unbatted, bowling_attack):
    """Continuation only: retain both survivors, including a set opener.

    Future dismissals are unknown. Average over which of the two current
    batters survives each dismissal rather than pretending batting-order
    position identifies the dismissed player. Unused batters enter in order.
    Full-XI strengths remain separate for a possible later innings.
    """
    attack = [p for p in bowling_attack if p.get("will_bowl")] or list(bowling_attack)
    bowling = (sum(p.get("bowling_rating", 50) for p in attack) / len(attack)
               if attack else 50.0)
    pair = list(current_batters)
    waiting = list(unbatted)
    if not pair:
        return strengths_by_wickets(waiting, bowling_attack)
    pair_mean = sum(ability(p) for p in pair) / len(pair)
    wickets = min(10, len(waiting) + len(pair) - 1)
    out = {}
    for w in range(wickets, 0, -1):
        remaining = [ability(p) for p in waiting]
        out[w] = (2 * pair_mean + sum(remaining)) / (2 + len(remaining)) - bowling
        if waiting:
            pair_mean = (pair_mean + ability(waiting.pop(0))) / 2
    return out


def _key(strengths):
    return tuple(round(strengths.get(w, 0.0), 1) for w in range(1, 11))


@functools.lru_cache(maxsize=2048)
def _forecast(pitch, overs, strengths_key, wear_start, wear_end, aggression,
              bucket, track, wickets_in_hand=10):
    strengths = {w: strengths_key[w - 1] for w in range(1, 11)}
    return innings_forecast(
        pitch=pitch, overs_available=overs, wickets_in_hand=wickets_in_hand,
        strength_by_wickets=strengths, aggression=aggression,
        wear_start=wear_start, wear_end=wear_end, bucket=bucket,
        track_absorption=track)


def _rounded(overs):
    return int(max(0, round(overs / OVERS_GRANULARITY) * OVERS_GRANULARITY))


@functools.lru_cache(maxsize=1024)
def _best_response_curves(pitch, overs, strengths_key, wear_start, wear_end,
                          risk_appetite, bucket):
    """Chasing side's best tempo for every target at once.

    Returns (win, loss, draw) arrays indexed by target bucket, each already
    resolved to the tempo that maximises the chasing side's own value. The
    best tempo genuinely varies with the target — go hard at a gettable one,
    shut up shop against an impossible one — so the choice is made per
    target rather than once for the innings.
    """
    stacked = []
    for tempo in TEMPOS:
        check_budget()
        forecast = _forecast(pitch, overs, strengths_key, wear_start,
                             wear_end, tempo, bucket, False)
        win, loss, draw = chase_curves(forecast)
        stacked.append((win, loss, draw,
                        win + DRAW_VALUE * risk_appetite * draw))
    scores = np.stack([s[3] for s in stacked])
    choice = scores.argmax(axis=0)
    index = np.arange(scores.shape[1])
    return (np.stack([s[0] for s in stacked])[choice, index],
            np.stack([s[1] for s in stacked])[choice, index],
            np.stack([s[2] for s in stacked])[choice, index])


def _target_index(targets, bucket, size):
    return np.clip(np.ceil(np.asarray(targets) / bucket).astype(int), 0, size)


def best_response(pitch, overs, strengths, wear_start, wear_end, target,
                  risk_appetite=DEFAULT_RISK_APPETITE, bucket=5):
    """How a side chasing `target` in `overs` would choose to play, and what
    that gets them. Returns (win, loss, draw)."""
    win, loss, draw = _best_response_curves(
        pitch, _rounded(overs), _key(strengths), round(wear_start, 2),
        round(wear_end, 2), risk_appetite, bucket)
    index = int(_target_index([target], bucket, len(win) - 1)[0])
    return float(win[index]), float(loss[index]), float(draw[index])


# A first-innings declaration is judged three innings ahead, and after the
# opposition's reply our "lead" may well be a deficit. The lead axis is
# therefore offset so it can represent both.
LEAD_FLOOR = 400
# At five runs per bucket, cover positions from -400 through +2000.
_MIN_POSITION_BUCKETS = 480


def _lead_index(leads, bucket, size):
    position = (np.asarray(leads, dtype=float) + LEAD_FLOOR) / bucket
    return np.clip(np.rint(position).astype(int), 0, size)


def _normalise_outcomes(outcome):
    """Remove floating-point dust and conserve probability for each position."""
    values = np.maximum(0.0, np.asarray(outcome, dtype=float))
    total = values.sum(axis=0)
    values /= np.where(total > 0, total, 1.0)
    values[1] = np.where(total > 0, values[1], 1.0)
    return tuple(values)


def _wear_after(start, end, elapsed, available):
    if elapsed <= 0:
        return round(start, 2)
    # Future wear is only a projection. A tenth-wide grid bounds nested
    # forecast caches; the observed starting wear remains exact.
    projected = round(start + (end - start) * min(1.0, elapsed / max(1, available)), 1)
    return round(max(min(start, end), min(max(start, end), projected)), 2)


def _ending_groups(forecast, horizon):
    """Joint runs/time distribution, grouped on the model's time grid.

    Surviving mass declares at the horizon; all-out mass ends at its own
    over. Grouping bounds the number of downstream forecasts, while keeping
    the full score distribution instead of substituting its mean.
    """
    groups = {}
    absorption = forecast["absorption"]
    if absorption is not None:
        for over, mass in enumerate(absorption):
            if mass.sum() > 0:
                elapsed = min(horizon, max(1, _rounded(over + 1)))
                groups[elapsed] = groups.get(elapsed, 0) + mass
    groups[horizon] = groups.get(horizon, 0) + forecast["survived_pmf"]
    return groups


def _defence_integral(leads, runs, mass, curves, bucket):
    their_win, their_loss, draw = curves
    index = _target_index(leads[:, None] + runs[None, :] + 1,
                          bucket, len(their_win) - 1)
    return tuple(values[index] @ mass for values in (their_loss, draw, their_win))


@functools.lru_cache(maxsize=512)
def _third_innings_curves(pitch, overs, own_key, opp_key, wear_start, wear_end,
                          risk_appetite, bucket, size):
    """Our best outcomes for every lead, including early all-out endings."""
    lead_axis = np.arange(size + 1) * bucket - LEAD_FLOOR
    best_value = np.full(size + 1, -np.inf)
    best = [np.zeros(size + 1) for _ in range(3)]
    for horizon in horizons_for(overs):
        check_budget()
        outcome = [np.zeros(size + 1) for _ in range(3)]
        if horizon:
            continuation = _forecast(pitch, horizon, own_key, wear_start,
                                    _wear_after(wear_start, wear_end, horizon, overs),
                                    0.5, bucket, True)
            groups = _ending_groups(continuation, horizon)
            runs = continuation["runs_axis"]
            spent = continuation["expected_overs"]
        else:
            groups, runs, spent = {0: np.ones(1)}, np.zeros(1), 0
        for elapsed, mass in groups.items():
            check_budget()
            if mass.sum() <= 0:
                continue
            curves = _best_response_curves(
                pitch, _rounded(overs - elapsed), opp_key,
                _wear_after(wear_start, wear_end, elapsed, overs), wear_end,
                risk_appetite, bucket)
            for slot, values in zip(outcome, _defence_integral(
                    lead_axis, runs, mass, curves, bucket)):
                slot += values
        win, drawn, loss = outcome
        score = win + DRAW_VALUE * risk_appetite * drawn - TIME_PREFERENCE * spent
        take = score > best_value
        best_value = np.where(take, score, best_value)
        for slot, values in zip(best, outcome):
            slot[:] = np.where(take, values, slot)
    return _normalise_outcomes(best)


@functools.lru_cache(maxsize=512)
def _lead_declaration_curves(pitch, overs, own_key, opp_key, wear_start,
                             wear_end, risk_appetite, opponent_risk, bucket, size):
    """Bowl, then possibly chase, for every lead on the same score grid."""
    leads = np.arange(size + 1) * bucket - LEAD_FLOOR
    best_score = np.full(size + 1, -np.inf)
    best = [np.zeros(size + 1) for _ in range(3)]
    for tempo in TEMPOS:
        check_budget()
        forecast = _forecast(pitch, overs, opp_key, wear_start, wear_end,
                             tempo, bucket, True)
        axis = forecast["runs_axis"]
        short = axis[None, :] < leads[:, None]
        cheaply = short @ forecast["all_out_pmf"]
        win = cheaply.copy()
        draw = np.full(size + 1, forecast["survived_pmf"].sum())
        loss = np.zeros(size + 1)
        for elapsed, mass in _ending_groups(
                dict(forecast, survived_pmf=np.zeros_like(axis)), overs).items():
            check_budget()
            if mass.sum() <= 0:
                continue
            remaining = _rounded(overs - elapsed)
            if remaining <= 0:
                draw += (~short) @ mass
                continue
            curves = _best_response_curves(
                pitch, remaining, own_key,
                _wear_after(wear_start, wear_end, elapsed, overs), wear_end,
                risk_appetite, bucket)
            index = _target_index(axis[None, :] - leads[:, None] + 1,
                                  bucket, len(curves[0]) - 1)
            for slot, curve in zip((win, loss, draw), curves):
                slot += (curve[index] * ~short) @ mass
        # Preserve the opponent's existing objective: avoid an innings defeat.
        take = -cheaply > best_score
        best_score = np.where(take, -cheaply, best_score)
        for slot, values in zip(best, (win, draw, loss)):
            slot[:] = np.where(take, values, slot)
    return _normalise_outcomes(best)


@functools.lru_cache(maxsize=512)
def _first_innings_curves(pitch, overs, own_key, opp_key, wear_start, wear_end,
                         risk_appetite, bucket, size, follow_on_margin):
    scores = np.arange(size + 1) * bucket - LEAD_FLOOR
    reply = _forecast(pitch, overs, opp_key, wear_start, wear_end, 0.0, bucket, True)
    axis = reply["runs_axis"]
    result = [np.zeros(size + 1),
              np.full(size + 1, reply["survived_pmf"].sum()), np.zeros(size + 1)]
    leads = scores[:, None] - axis[None, :]
    index = _lead_index(leads, bucket, size)
    for elapsed, mass in _ending_groups(
            dict(reply, survived_pmf=np.zeros_like(axis)), overs).items():
        check_budget()
        if mass.sum() <= 0:
            continue
        remaining = _rounded(overs - elapsed)
        if remaining <= 0:
            result[1] += mass.sum()
            continue
        wear = _wear_after(wear_start, wear_end, elapsed, overs)
        decline = _third_innings_curves(pitch, remaining, own_key, opp_key,
                                        wear, wear_end, risk_appetite, bucket, size)
        chosen = [curve[index] for curve in decline]
        if follow_on_margin is not None:
            # Project a spell's workload before asking the same bowl-then-
            # chase model used by the live follow-on decision. A full innings
            # of work cannot be treated as a completely fresh attack.
            freshness = round(max(0.0, 1.0 - elapsed / 180.0) * 4) / 4
            tired_key = tuple(v + MAX_FATIGUE_PENALTY * (1 - freshness) for v in opp_key)
            enforce = _lead_declaration_curves(
                pitch, remaining, own_key, tired_key, wear, wear_end,
                risk_appetite, DEFAULT_RISK_APPETITE, bucket, size)
            eligible = leads >= follow_on_margin
            prefer = eligible & ((enforce[0][index] + DRAW_VALUE * risk_appetite * enforce[1][index])
                                 > (chosen[0] + DRAW_VALUE * risk_appetite * chosen[1]))
            chosen = [np.where(prefer, curve[index], old)
                      for curve, old in zip(enforce, chosen)]
        for slot, values in zip(result, chosen):
            slot += values @ mass
    return _normalise_outcomes(result)


def innings_one_declaration_outcome(*, pitch, score, overs_remaining,
                                    own_strengths, opposition_strengths,
                                    wear_start=0.2, wear_end=0.8,
                                    risk_appetite=DEFAULT_RISK_APPETITE,
                                    bucket=5, follow_on_margin=200):
    """Declare innings one, including an eligible follow-on after the reply."""
    if overs_remaining <= 0:
        return 0.0, 1.0, 0.0
    size = max(_MIN_POSITION_BUCKETS, int((score + LEAD_FLOOR) / bucket) + 1)
    curves = _first_innings_curves(
        pitch, _rounded(overs_remaining), _key(own_strengths),
        _key(opposition_strengths), round(wear_start, 2), round(wear_end, 2),
        risk_appetite, bucket, size, follow_on_margin)
    index = int(_lead_index([score], bucket, size)[0])
    return tuple(float(curve[index]) for curve in curves)


MAX_FATIGUE_PENALTY = 12.0


def evaluate_follow_on(*, pitch, deficit, overs_remaining, own_strengths,
                       opposition_strengths, wear_start=0.3, wear_end=0.8,
                       risk_appetite=DEFAULT_RISK_APPETITE,
                       attack_freshness=1.0, bucket=5):
    """Enforce the follow-on, or bat again? Same value function, two branches.

    Enforcing means they bat now and we may have to chase last; declining
    means we bat now and they chase last. The real cost of enforcing is that
    our attack goes straight back out, so a tired attack is modelled as a
    genuinely weaker one rather than as a threshold on overs bowled.
    """
    overs_remaining = max(0, int(overs_remaining))
    if overs_remaining <= 0:
        return None

    penalty = MAX_FATIGUE_PENALTY * (1.0 - max(0.0, min(1.0, attack_freshness)))
    tired = {w: v + penalty for w, v in opposition_strengths.items()}
    enforce = lead_declaration_outcome(
        pitch=pitch, lead=deficit, overs_remaining=overs_remaining,
        opposition_strengths=tired, own_strengths=own_strengths,
        wear_start=wear_start, wear_end=wear_end, risk_appetite=risk_appetite)

    win_curve, draw_curve, loss_curve = _third_innings_curves(
        pitch, _rounded(overs_remaining), _key(own_strengths),
        _key(opposition_strengths), round(wear_start, 2), round(wear_end, 2),
        risk_appetite, bucket, max(_MIN_POSITION_BUCKETS, int((deficit + LEAD_FLOOR) / bucket) + 1))
    index = int(_lead_index([deficit], bucket, len(win_curve) - 1)[0])
    decline = (float(win_curve[index]), float(draw_curve[index]),
               float(loss_curve[index]))

    options = {
        "enforce": dict(win=enforce[0], draw=enforce[1], loss=enforce[2],
                        value=value(enforce[0], enforce[2], risk_appetite)),
        "decline": dict(win=decline[0], draw=decline[1], loss=decline[2],
                        value=value(decline[0], decline[2], risk_appetite)),
    }
    choice = max(options, key=lambda k: options[k]["value"])
    return dict(options=options, enforce=choice == "enforce",
                fatigue_penalty=penalty)


def target_defence_outcome(*, pitch, target, overs_remaining, opposition_strengths,
                           wear_start=0.5, wear_end=0.8,
                           opponent_risk_appetite=DEFAULT_RISK_APPETITE):
    """We have set `target`; the opposition's chase ends the match.

    Returns our (win, draw, loss). The third innings case: there is no
    further innings for us, so their win is our loss.
    """
    their_win, their_loss, draw = best_response(
        pitch, overs_remaining, opposition_strengths, wear_start, wear_end,
        target, opponent_risk_appetite)
    return their_loss, draw, their_win


def lead_declaration_outcome(*, pitch, lead, overs_remaining,
                             opposition_strengths, own_strengths,
                             wear_start=0.4, wear_end=0.8,
                             risk_appetite=DEFAULT_RISK_APPETITE,
                             opponent_risk_appetite=DEFAULT_RISK_APPETITE):
    """We declare with `lead`; they bat, then we may have to chase.

    The second innings case. Their innings consumes overs as well as runs,
    and the overs it consumes are what we have left to chase in — pricing
    that is the entire point.
    """
    if overs_remaining <= 0:
        return 0.0, 1.0, 0.0
    bucket = 5
    size = max(_MIN_POSITION_BUCKETS, int((lead + LEAD_FLOOR) / bucket) + 1)
    curves = _lead_declaration_curves(
        pitch, _rounded(overs_remaining), _key(own_strengths),
        _key(opposition_strengths), round(wear_start, 2), round(wear_end, 2),
        risk_appetite, opponent_risk_appetite, bucket, size)
    index = int(_lead_index([lead], bucket, size)[0])
    return tuple(float(curve[index]) for curve in curves)


def evaluate_declaration(*, pitch, fc_innings, lead, overs_remaining,
                         own_strengths, opposition_strengths,
                         wickets_in_hand, wear_start=0.4, wear_end=0.8,
                         risk_appetite=DEFAULT_RISK_APPETITE,
                         horizons=None, continuation_strengths=None, follow_on_margin=200):
    """Compare declaring now against batting on, and report every option.

    Batting on buys runs and spends overs. The old flat-threshold check only
    ever asked whether the lead was big enough, so its sole way to increase a
    lead was to keep batting — which is why it set targets nobody could chase.
    """
    options = []
    bucket = 5
    size = max(_MIN_POSITION_BUCKETS, int((lead + 900 + LEAD_FLOOR) / bucket) + 1)
    own_key, opp_key = _key(own_strengths), _key(opposition_strengths)
    continuation_key = _key(continuation_strengths or own_strengths)
    for horizon in (horizons if horizons is not None else horizons_for(overs_remaining)):
        check_budget()
        if horizon < 0 or horizon >= overs_remaining:
            continue
        if horizon == 0:
            expected_lead, survival, spent = float(lead), 1.0, 0.0
            groups, runs = {0: np.ones(1)}, np.zeros(1)
        else:
            continuation = _forecast(
                pitch, int(horizon), continuation_key, round(wear_start, 2),
                _wear_after(wear_start, wear_end, horizon, overs_remaining),
                0.5, bucket, True, max(1, int(wickets_in_hand)))
            expected_lead = lead + continuation["expected_runs"]
            survival = 1.0 - continuation["p_all_out"]
            spent = continuation["expected_overs"]
            groups, runs = _ending_groups(continuation, horizon), continuation["runs_axis"]
        outcome = np.zeros(3)
        for elapsed, mass in groups.items():
            check_budget()
            if mass.sum() <= 0:
                continue
            remaining = _rounded(overs_remaining - elapsed)
            if remaining <= 0:
                outcome[1] += mass.sum()
                continue
            wear = _wear_after(wear_start, wear_end, elapsed, overs_remaining)
            if fc_innings == 3:
                curves = _best_response_curves(pitch, remaining, opp_key, wear,
                                               round(wear_end, 2), DEFAULT_RISK_APPETITE, bucket)
                outcome += np.array(_defence_integral(
                    np.array([lead]), runs, mass, curves, bucket)).ravel()
            else:
                if fc_innings == 1:
                    curves = _first_innings_curves(
                        pitch, remaining, own_key, opp_key, wear, round(wear_end, 2),
                        risk_appetite, bucket, size, follow_on_margin)
                else:
                    curves = _lead_declaration_curves(
                        pitch, remaining, own_key, opp_key, wear, round(wear_end, 2),
                        risk_appetite, DEFAULT_RISK_APPETITE, bucket, size)
                index = _lead_index(lead + runs, bucket, size)
                outcome += [float(curve[index] @ mass) for curve in curves]
        win, draw, loss = map(float, _normalise_outcomes(outcome))
        options.append(dict(horizon=horizon, lead=expected_lead,
                            win=win, draw=draw, loss=loss, survival=survival,
                            expected_overs=spent,
                            value=value(win, loss, risk_appetite) - TIME_PREFERENCE * spent))

    if not options:
        return None
    # Ties go to the shorter horizon. When batting on buys no extra chance
    # of winning — which is most of what happens on a flat pitch late in a
    # dead match — a captain declares rather than occupying the crease for
    # nothing. Without this the tiniest floating-point edge sends it to the
    # far end of the horizon list, which is how 700-run first innings and
    # 800-run targets get built.
    best = max(options, key=lambda o: (round(o["value"], 3), -o["horizon"]))
    now = next((o for o in options if o["horizon"] == 0), None)
    return dict(options=options, best=best, declare_now=best["horizon"] == 0,
                now=now)
