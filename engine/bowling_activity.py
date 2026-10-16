"""Shared inclusion rules for real bowling spells (including illegal deliveries)."""

from sqlalchemy import or_

BOWLING_ACTIVITY_FIELDS = (
    "balls_bowled", "runs_conceded", "wickets", "wides", "noballs",
)


def has_bowling_activity(card):
    """Exclude unused bowlers' empty rows without losing zero-legal-ball spells."""
    return any((getattr(card, field, None) or 0) > 0 for field in BOWLING_ACTIVITY_FIELDS)


def bowling_activity_predicate(model):
    """SQL equivalent of has_bowling_activity; NULL counters count as zero."""
    return or_(*(getattr(model, field) > 0 for field in BOWLING_ACTIVITY_FIELDS))
