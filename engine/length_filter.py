"""Shared List A scheduled-length filtering; never use rain-revised overs."""
from sqlalchemy import func, or_
from engine.format_config import resolve_scheduled_overs


def parse_length_filter(value):
    if value in (None, "", "all"):
        return None
    return resolve_scheduled_overs("ListA", value)


def length_predicate(model, scheduled_overs):
    length = parse_length_filter(scheduled_overs)
    if length is None:
        return True
    # Legacy null List A rows predate this feature and were scheduled for 50.
    return or_(model.match_format != "ListA", func.coalesce(model.scheduled_overs, 50) == length)
