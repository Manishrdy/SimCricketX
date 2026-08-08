"""Toss -> innings order.  Single source of truth.

Both the engine and the routes layer need to answer "who bats first?" from
(toss_winner, toss_decision).  That four-way mapping used to be copy-pasted at
six call sites and one of the copies had dropped the winner check entirely,
so an away side that won the toss and elected to bat did not get to bat.
Every call site now routes through the two helpers below.
"""


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
