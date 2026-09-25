"""Toss -> decision, and toss -> innings order.  Single source of truth.

Both the engine and the routes layer need to answer "who bats first?" from
(toss_winner, toss_decision).  That four-way mapping used to be copy-pasted at
six call sites and one of the copies had dropped the winner check entirely,
so an away side that won the toss and elected to bat did not get to bat.
Every call site now routes through the two helpers below.

correct_decision()/decide_toss() answer the other half: what the winning
captain should do with the toss he just won.  `spin_toss` used to settle that
with `random.choice(["Bat", "Bowl"])` — the captain flipped a second coin and
never looked at the pitch, so on a Green seamer he batted first half the time
and FormatConfig.correct_toss_choice, which exists to say what the conditions
favour, was only ever read afterwards to hand out a +/-3% boundary modifier
for a choice nobody had made deliberately.
"""

import random as _random


def home_bats_first(toss_winner, toss_decision, home_code):
    """True when the home side bats the first innings.

    The toss winner bats first iff it chose to bat; otherwise the other side
    does.  Missing toss data falls back to the home side, matching how legacy
    match files without a recorded toss have always been read.
    """
    if not toss_winner or not toss_decision:
        return True
    return (toss_winner == home_code) == (str(toss_decision).strip().lower() == "bat")


def innings_teams(toss_winner, toss_decision, home_code, home_xi, away_xi, innings=1):
    """Return (batting_xi, bowling_xi) for the given innings."""
    home_first = home_bats_first(toss_winner, toss_decision, home_code)
    home_bats = home_first if innings == 1 else not home_first
    return (home_xi, away_xi) if home_bats else (away_xi, home_xi)


# How often the AI captain actually reads the surface correctly. Deliberately
# not 1.0: a toss that always goes the same way on a given pitch makes every
# match on that surface open identically, and real captains do misread a
# track — the wrong call in one toss out of five is where "he'll regret that
# by tea" comes from.
CORRECT_DECISION_RATE = 0.8


def correct_decision(fmt, pitch, is_day_night=False):
    """The toss decision the conditions favour here, as "Bat" or "Bowl".

    Reads FormatConfig/MultiDayFormatConfig's correct_toss_choice, preferring
    the day/night override when one is configured and the match is under
    lights (dew in the second innings tilts almost every pitch towards
    bowling first). Shared by decide_toss() below and by Match.__init__'s
    toss-advantage flag so the choice the captain makes and the choice the
    engine grades him against can never disagree.
    """
    if is_day_night:
        dn_choices = getattr(fmt, "correct_toss_choice_dn", None)
        if dn_choices:
            return "Bowl" if dn_choices.get(pitch, "bowl") == "bowl" else "Bat"
    choices = getattr(fmt, "correct_toss_choice", None) or {}
    return "Bowl" if choices.get(pitch, "bat") == "bowl" else "Bat"


def decide_toss(fmt, pitch, is_day_night=False, rng=None):
    """An AI captain's call after winning the toss, as "Bat" or "Bowl"."""
    rng = rng or _random
    decision = correct_decision(fmt, pitch, is_day_night=is_day_night)
    rate = 0.65 if getattr(fmt, "name", None) == "Hundred" and is_day_night else CORRECT_DECISION_RATE
    if rng.random() >= rate:
        decision = "Bowl" if decision == "Bat" else "Bat"
    return decision
