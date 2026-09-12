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


def _lead_index(leads, bucket, size):
    position = (np.asarray(leads, dtype=float) + LEAD_FLOOR) / bucket
    return np.clip(np.rint(position).astype(int), 0, size)


@functools.lru_cache(maxsize=512)
def _third_innings_curves(pitch, overs, own_key, opp_key, wear_start, wear_end,
                          risk_appetite, bucket, size):
    """Our best (win, draw, loss) for every lead, batting the third innings.

    For each lead the captain may bat on for any horizon before declaring,
    so this already contains the "how long do we bat" decision. Indexed by
    lead via `_lead_index`, which spans deficits as well as leads.
    """
    lead_axis = np.arange(size + 1) * bucket - LEAD_FLOOR
    best_value = np.full(size + 1, -np.inf)
    best = [np.zeros(size + 1) for _ in range(3)]

    for horizon in horizons_for(overs):
        gained = 0.0
        if horizon:
            gained = _forecast(pitch, _rounded(horizon), own_key, wear_start,
                               wear_end, 0.5, bucket, False)["expected_runs"]
        their_win, their_loss, draw = _best_response_curves(
            pitch, _rounded(overs - horizon), opp_key, wear_start, wear_end,
            risk_appetite, bucket)
        index = _target_index(lead_axis + gained + 1, bucket,
                              len(their_win) - 1)
        # Their chase ends the match, so their loss is our win.
        win, loss, drawn = their_loss[index], their_win[index], draw[index]
        score = (win + DRAW_VALUE * risk_appetite * drawn
                 - TIME_PREFERENCE * horizon)
        take = score > best_value
        best_value = np.where(take, score, best_value)
        for slot, values in zip(best, (win, drawn, loss)):
            slot[:] = np.where(take, values, slot)
    return best[0], best[1], best[2]


def innings_one_declaration_outcome(*, pitch, score, overs_remaining,
                                    own_strengths, opposition_strengths,
                                    wear_start=0.2, wear_end=0.8,
                                    risk_appetite=DEFAULT_RISK_APPETITE,
                                    bucket=5):
    """We declare the FIRST innings at `score`. Three innings still to play.

    They reply, we bat again, they chase. The old first-innings rule was a
    flat "is 300 enough" threshold with no model at all behind it, which is
    where the 600-plus first innings came from.
    """
    overs_remaining = max(0, int(overs_remaining))
    if overs_remaining <= 0:
        return 0.0, 1.0, 0.0

    reply = _forecast(pitch, _rounded(overs_remaining),
                      _key(opposition_strengths), round(wear_start, 2),
                      round(wear_end, 2), 0.0, bucket, True)
    axis = reply["runs_axis"]
    size = len(axis) - 1
    win = draw = loss = 0.0

    # They bat out the available overs: nobody bats again.
    draw += float(reply["survived_pmf"].sum())

    absorption = reply["absorption"]
    if absorption is None:
        return 0.0, 1.0, 0.0

    grouped = {}
    for over in range(absorption.shape[0]):
        row = absorption[over]
        if row.sum() < 1e-12:
            continue
        overs_left = _rounded(overs_remaining - over - 1)
        if overs_left <= 0:
            draw += float(row.sum())
            continue
        grouped[overs_left] = grouped.get(overs_left, 0) + row

    leads = score - axis
    for overs_left, mass in grouped.items():
        win_curve, draw_curve, loss_curve = _third_innings_curves(
            pitch, overs_left, _key(own_strengths), _key(opposition_strengths),
            round(wear_start, 2), round(wear_end, 2), risk_appetite, bucket,
            size)
        index = _lead_index(leads, bucket, size)
        win += float((mass * win_curve[index]).sum())
        draw += float((mass * draw_curve[index]).sum())
        loss += float((mass * loss_curve[index]).sum())

    win, draw, loss = max(0.0, win), max(0.0, draw), max(0.0, loss)
    total = win + draw + loss
    if total <= 0:
        return 0.0, 1.0, 0.0
    return win / total, draw / total, loss / total


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
        risk_appetite, bucket, len(np.arange(900 // bucket + 1)) - 1)
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
    overs_remaining = max(0, int(overs_remaining))
    if overs_remaining <= 0:
        return 0.0, 1.0, 0.0

    # Their best response when batting to set us a target rather than chase.
    opposition = None
    for tempo in TEMPOS:
        forecast = _forecast(pitch, _rounded(overs_remaining),
                             _key(opposition_strengths), round(wear_start, 2),
                             round(wear_end, 2), tempo, 5, True)
        # A side batting third wants runs on the board and time off the
        # clock; score it by how rarely it is dismissed cheaply.
        cheaply = float(forecast["all_out_pmf"][forecast["runs_axis"] < lead].sum())
        score = -cheaply
        if opposition is None or score > opposition[0]:
            opposition = (score, forecast)
    forecast = opposition[1]

    win = draw = loss = 0.0
    axis = forecast["runs_axis"]
    absorption = forecast["absorption"]
    bucket = forecast["bucket"]

    # Mass that survives the available overs: nobody bats again.
    draw += float(forecast["survived_pmf"].sum())

    if absorption is None:
        return 0.0, 1.0, 0.0

    # Bowled out short of our lead, whenever it happens: an innings victory.
    short = axis < lead
    win += float(absorption[:, short].sum())
    targets = axis[~short] - lead + 1

    # Group the remaining mass by how many overs our chase would have. The
    # chase curve is evaluated once per group rather than once per outcome.
    grouped = {}
    for over in range(absorption.shape[0]):
        rest = absorption[over][~short]
        if rest.sum() < 1e-12:
            continue
        overs_left = overs_remaining - over - 1
        if overs_left <= 0:
            draw += float(rest.sum())
            continue
        key = _rounded(overs_left)
        if key <= 0:
            draw += float(rest.sum())
            continue
        grouped[key] = grouped.get(key, 0) + rest

    for overs_left, mass in grouped.items():
        win_curve, loss_curve, draw_curve = _best_response_curves(
            pitch, overs_left, _key(own_strengths), round(wear_start, 2),
            round(wear_end, 2), risk_appetite, bucket)
        index = _target_index(targets, bucket, len(win_curve) - 1)
        win += float((mass * win_curve[index]).sum())
        loss += float((mass * loss_curve[index]).sum())
        draw += float((mass * draw_curve[index]).sum())

    win, draw, loss = (max(0.0, win), max(0.0, draw), max(0.0, loss))
    total = win + draw + loss
    if total <= 0:
        return 0.0, 1.0, 0.0
    return win / total, draw / total, loss / total


def evaluate_declaration(*, pitch, fc_innings, lead, overs_remaining,
                         own_strengths, opposition_strengths,
                         wickets_in_hand, wear_start=0.4, wear_end=0.8,
                         risk_appetite=DEFAULT_RISK_APPETITE,
                         horizons=None):
    """Compare declaring now against batting on, and report every option.

    Batting on buys runs and spends overs. The old flat-threshold check only
    ever asked whether the lead was big enough, so its sole way to increase a
    lead was to keep batting — which is why it set targets nobody could chase.
    """
    options = []
    for horizon in (horizons if horizons is not None
                    else horizons_for(overs_remaining)):
        if horizon >= overs_remaining:
            continue
        if horizon == 0:
            expected_lead, survival = float(lead), 1.0
        else:
            # Batting on happens with the wickets actually in hand, not a
            # fresh ten: a side eight down buys far fewer runs per over.
            continuation = _forecast(
                pitch, _rounded(horizon), _key(own_strengths),
                round(wear_start, 2), round(wear_end, 2), 0.5, 5, False,
                max(1, int(wickets_in_hand)))
            expected_lead = lead + continuation["expected_runs"]
            survival = 1.0 - continuation["p_all_out"]

        if fc_innings == 1:
            win, draw, loss = innings_one_declaration_outcome(
                pitch=pitch, score=int(expected_lead),
                overs_remaining=overs_remaining - horizon,
                own_strengths=own_strengths,
                opposition_strengths=opposition_strengths,
                wear_start=wear_start, wear_end=wear_end,
                risk_appetite=risk_appetite)
        elif fc_innings == 3:
            win, draw, loss = target_defence_outcome(
                pitch=pitch, target=int(expected_lead) + 1,
                overs_remaining=overs_remaining - horizon,
                opposition_strengths=opposition_strengths,
                wear_start=wear_start, wear_end=wear_end)
        else:
            win, draw, loss = lead_declaration_outcome(
                pitch=pitch, lead=int(expected_lead),
                overs_remaining=overs_remaining - horizon,
                opposition_strengths=opposition_strengths,
                own_strengths=own_strengths,
                wear_start=wear_start, wear_end=wear_end,
                risk_appetite=risk_appetite)

        options.append(dict(horizon=horizon, lead=expected_lead,
                            win=win, draw=draw, loss=loss,
                            survival=survival,
                            value=value(win, loss, risk_appetite)
                            - TIME_PREFERENCE * horizon))

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
