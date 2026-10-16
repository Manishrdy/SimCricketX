"""Shared batting participation rules for current and legacy scorecards."""

from sqlalchemy import or_


def has_batting_activity(card):
    """An explicit appearance includes 0*; counters preserve legacy innings."""
    return bool(
        getattr(card, "did_bat", False)
        or (card.balls or 0) > 0
        or (card.runs or 0) > 0
        or card.is_out
    )


def batting_activity_predicate(model):
    """SQL equivalent; NULL participation means legacy/unknown."""
    return or_(model.did_bat.is_(True), model.balls > 0, model.runs > 0,
               model.is_out.is_(True))


def stats_have_batting_activity(stats):
    """Engine/archive dictionary equivalent of has_batting_activity."""
    return bool(stats.get("did_bat") or stats.get("balls", 0)
                or stats.get("runs", 0) or stats.get("wicket_type"))
