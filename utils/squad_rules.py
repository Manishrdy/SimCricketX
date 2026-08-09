"""
Canonical squad-legality rules for team profiles.

Single source of truth for the player-count bounds, minimum bowling
options, and minimum wicketkeepers required to publish a squad. Every
path that validates a squad (team create/edit, the squad-page publish
API, the squad-page bulk-publish API) — plus the client-side JS in
team_create.html / team_squad.html via the injected SQUAD_* template
globals in app.py — must read these constants / call
validate_squad_composition() instead of re-deriving the numbers, or the
paths will drift out of sync with each other again.
"""

MIN_PLAYERS = 11
MAX_PLAYERS = 25
MIN_WICKETKEEPERS = 1
MIN_BOWLING_OPTIONS = 5

BOWLING_ROLES = ("Bowler", "All-rounder")


def validate_squad_composition(roles, has_captain, has_wicketkeeper, is_draft=False):
    """
    Validate squad composition given the list of player role strings.

    Drafts only enforce the player-count ceiling; only non-draft
    (published) squads must meet the full composition requirements.

    Returns an error message string if invalid, or None if legal.
    """
    count = len(roles)

    if is_draft:
        if count < 1:
            return "Draft must have at least 1 player."
        if count > MAX_PLAYERS:
            return f"Maximum {MAX_PLAYERS} players (have {count})."
        return None

    if not (MIN_PLAYERS <= count <= MAX_PLAYERS):
        return f"You must enter between {MIN_PLAYERS} and {MAX_PLAYERS} players (have {count})."

    wk_count = sum(1 for r in roles if r == "Wicketkeeper")
    if wk_count < MIN_WICKETKEEPERS:
        return f"Needs at least {MIN_WICKETKEEPERS} Wicketkeeper."

    bowl_count = sum(1 for r in roles if r in BOWLING_ROLES)
    if bowl_count < MIN_BOWLING_OPTIONS:
        return f"Needs at least {MIN_BOWLING_OPTIONS} Bowlers/All-rounders."

    if not has_captain:
        return "Captain must be selected."
    if not has_wicketkeeper:
        return "Wicketkeeper must be selected."

    return None
