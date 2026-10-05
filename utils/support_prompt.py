"""Support (Buy Me a Coffee) prompt rules.

One place decides who is asked, and how often, so the dashboard popup, the
post-match note and the event endpoint cannot drift apart.

State lives on three nullable ``users`` columns (NULL = never happened):

  support_prompt_shown_at     the popup was actually displayed
  support_prompt_dismissed_at the user closed it (informational; the cooldown
                              runs from ``shown_at`` so an unclosed popup is
                              not re-shown on the next refresh)
  support_prompt_clicked_at   the user followed the support link
  support_prompt_optout_at    the user chose "Don't ask again"

Rules, in order:
  * Opted out            -> never asked.
  * Admins               -> never asked (they are the people running the site).
  * Fewer than 5 matches -> not yet: nobody is asked before they got value.
  * Clicked the link     -> left alone for 90 days.
  * Popup shown < 15 days ago -> not again yet.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

SUPPORT_URL = "https://buymeacoffee.com/manishrdy"

MIN_MATCHES = 5
CLOSED_COOLDOWN_DAYS = 15
CLICKED_COOLDOWN_DAYS = 90

EVENTS = ("shown", "closed", "clicked", "optout")
SOURCES = ("popup", "postmatch")


def _recent(stamp: Optional[datetime], days: int, now: datetime) -> bool:
    return stamp is not None and now - stamp < timedelta(days=days)


def _blocked(user, matches_count: int) -> bool:
    """Rules that apply to every surface, popup or post-match note."""
    if getattr(user, "support_prompt_optout_at", None) is not None:
        return True
    if getattr(user, "is_admin", False):
        return True
    return matches_count < MIN_MATCHES


def should_show_popup(user, matches_count: int, now: Optional[datetime] = None) -> bool:
    """True if the dashboard popup is due for this user."""
    now = now or datetime.utcnow()
    if _blocked(user, matches_count):
        return False
    if _recent(user.support_prompt_clicked_at, CLICKED_COOLDOWN_DAYS, now):
        return False
    return not _recent(user.support_prompt_shown_at, CLOSED_COOLDOWN_DAYS, now)


def should_show_postmatch_note(user, matches_count: int, now: Optional[datetime] = None) -> bool:
    """True if the quiet note after a match may be shown.

    It is a passive line, not a popup, so it ignores the popup's 15-day
    cooldown; it still honours opt-out, the 5-match floor and the post-click
    quiet period.
    """
    now = now or datetime.utcnow()
    if _blocked(user, matches_count):
        return False
    return not _recent(user.support_prompt_clicked_at, CLICKED_COOLDOWN_DAYS, now)


def record_event(user, event: str, now: Optional[datetime] = None) -> bool:
    """Apply ``event`` to ``user`` (caller commits). False for an unknown event."""
    if event not in EVENTS:
        return False
    now = now or datetime.utcnow()
    if event == "shown":
        user.support_prompt_shown_at = now
    elif event == "closed":
        user.support_prompt_dismissed_at = now
        if user.support_prompt_shown_at is None:
            user.support_prompt_shown_at = now
    elif event == "clicked":
        user.support_prompt_clicked_at = now
        user.support_prompt_dismissed_at = now
        if user.support_prompt_shown_at is None:
            user.support_prompt_shown_at = now
    else:  # optout
        user.support_prompt_optout_at = now
        user.support_prompt_dismissed_at = now
    return True
