"""Shared helpers for partnerships and fall of wickets.

A fallen wicket ends exactly one partnership, so fall-of-wickets is never
stored separately: it is derived from the broken partnerships. The engine,
the end-of-match commentary and the saved scoreboard page all go through
here so the three can't drift apart.

A partnership dict (engine or DB-derived) uses these keys:
    wicket_number, batsman1_name, batsman2_name, runs, balls,
    batsman1_contribution, batsman1_balls, batsman2_contribution,
    batsman2_balls, fours, sixes, is_unbroken, score_at_fall,
    fall_over, wicket_type, dismissed_slot (1 or 2, None if unbroken)
Legacy rows may carry None for fours/sixes/batter balls/dismissed_slot.
"""

from engine.cricket_math import balls_to_overs_str


def ordinal(n):
    """1 -> '1st', 2 -> '2nd', 11 -> '11th', 22 -> '22nd'."""
    n = int(n)
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def dismissed_name(partnership):
    """Name of the batter dismissed at the end of this stand, if known."""
    slot = partnership.get("dismissed_slot")
    if slot == 1:
        return partnership.get("batsman1_name")
    if slot == 2:
        return partnership.get("batsman2_name")
    return None


def legacy_fall_over(end_over, balls_per_over=6, is_hundred=False):
    """Convert the stored decimal ``end_over`` (over + ball/bpo) to notation.

    Used only to backfill rows saved before ``fall_over`` existed.
    """
    if end_over is None:
        return None
    balls = int(round(float(end_over) * balls_per_over))
    return str(balls) if is_hundred else balls_to_overs_str(balls, balls_per_over)


def fall_of_wickets(partnerships):
    """Fall-of-wickets entries, in wicket order, from broken partnerships."""
    entries = []
    for p in sorted(partnerships or [], key=lambda x: x.get("wicket_number") or 0):
        if p.get("is_unbroken") or p.get("score_at_fall") is None:
            continue
        entries.append({
            "wicket": p.get("wicket_number"),
            "score": p.get("score_at_fall"),
            "batter": dismissed_name(p),
            "over": p.get("fall_over"),
            "wicket_type": p.get("wicket_type"),
        })
    return entries


def fow_text(entry, over_unit="ov"):
    """'3-112 (S Iyer, 14.2 ov)' — omits whatever is unknown."""
    detail = []
    if entry.get("batter"):
        detail.append(entry["batter"])
    if entry.get("over"):
        detail.append(f"{entry['over']} {over_unit}".strip())
    head = f"{entry.get('wicket')}-{entry.get('score')}"
    return f"{head} ({', '.join(detail)})" if detail else head


def batter_share(name, runs, balls, unbroken=False):
    """'S Gill 18(10)*' — balls omitted when unknown (legacy rows)."""
    star = "*" if unbroken else ""
    runs = runs if runs is not None else 0
    if balls is None:
        return f"{name} {runs}{star}"
    return f"{name} {runs}({balls}){star}"
