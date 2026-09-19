"""
Canonical startability rules for a tournament fixture.

Single source of truth for "may this fixture be played right now?".
Every path that starts a match from a fixture — the /match/setup page
render (GET) and the /match/setup match creation (POST) — must call
fixture_start_block() instead of re-deriving the rules, or the paths will
drift out of sync with each other again.

They already did once: the GET branch refused Locked fixtures, Completed
fixtures and knockout pairings whose feeders hadn't been played, while the
POST branch that actually creates the match enforced none of it. Since the
setup page bakes its fixtureId in at render time, a tab left open (or
reached with Back) while that fixture was played elsewhere would re-POST a
now-Completed fixture_id and be handed a fresh match id.

The resulting duplicate is not merely redundant. On completion,
_handle_tournament_match_completion looks the match up by its *new* id, so
the resimulation-reversal branch never fires; it then repoints
fixture.match_id / winner_team_id at the duplicate while update_standings
no-ops on standings_applied. The first match is left orphaned — still
carrying tournament_id, still counted in career and tournament totals, and
no longer reachable by Re-simulate, which works off fixture.match_id.
"""

from collections import namedtuple

# reason:   user-facing explanation
# category: flash category for the HTML path ('error' / 'info')
# scope:    which dashboard the HTML path should send the user back to
FixtureBlock = namedtuple("FixtureBlock", ("reason", "category", "scope"))

SCOPE_TOUR = "tour"
SCOPE_TOURNAMENT = "tournament"


def fixture_start_block(fixture):
    """Why `fixture` cannot be started, or None if it can be.

    Ordered cheapest/most-specific first so the message the user sees names
    the actual obstacle: tour-series ordering and squad legality, then the
    fixture's own status, then whether a knockout pairing was earned.
    """
    from engine.tour_engine import fixture_block_reason
    from engine.tournament_engine import TournamentEngine

    # Tour series: ordering, squad legality, and its own status rule. Only
    # ever returns a reason for a fixture whose tournament belongs to a tour.
    tour_reason = fixture_block_reason(fixture)
    if tour_reason:
        return FixtureBlock(tour_reason, "error", SCOPE_TOUR)

    if fixture.status == "Locked":
        return FixtureBlock(
            "Cannot start a locked match. Wait for previous rounds to complete.",
            "error",
            SCOPE_TOURNAMENT,
        )

    if fixture.status == "Completed":
        return FixtureBlock(
            "This match is already completed.",
            "info",
            SCOPE_TOURNAMENT,
        )

    if fixture.status != "Scheduled":
        # Fail closed on any status these rules haven't been taught.
        return FixtureBlock(
            "This fixture is not available to start.",
            "error",
            SCOPE_TOURNAMENT,
        )

    # 'Scheduled' is not on its own proof the pairing was earned: a
    # historical bracket bug could fabricate one, and playing it would
    # cement teams that never qualified into a real result. Verify against
    # the fixture's own feeders before allowing it.
    if not TournamentEngine().feeders_decided(fixture.tournament, fixture):
        return FixtureBlock(
            "This fixture's teams haven't been decided by the previous "
            "round yet. Play the earlier rounds first.",
            "error",
            SCOPE_TOURNAMENT,
        )

    return None


# ── In-flight match reservation ───────────────────────────────────────────
#
# A fixture's status stays 'Scheduled' from the moment a match is set up
# until that match finishes, so status alone cannot stop a second start.
# Two tabs (or a double-submit) each used to POST /match/setup for the same
# Scheduled fixture and get two different match ids back. Both were playable
# to completion, and the second one to finish would repoint
# fixture.match_id/winner_team_id at itself while update_standings no-oped on
# standings_applied — the corruption described in this module's docstring,
# reached from the other direction.
#
# The claim is a conditional UPDATE rather than a read-then-write so two
# concurrent requests cannot both see "free" and both proceed.


def claim_fixture(fixture, match_id):
    """Atomically reserve `fixture` for `match_id`.

    Returns True if this caller won the claim. A False return means another
    match already holds the fixture — read active_match_id to find out which.
    The caller must release the claim if whatever it was claiming for then
    fails, or the fixture stays locked to a match that never existed.
    """
    from database import db
    from database.models import TournamentFixture

    updated = (
        db.session.query(TournamentFixture)
        .filter(
            TournamentFixture.id == fixture.id,
            TournamentFixture.active_match_id.is_(None),
        )
        .update({"active_match_id": match_id}, synchronize_session=False)
    )
    db.session.commit()
    db.session.refresh(fixture)
    return bool(updated)


def release_fixture(fixture, match_id=None, commit=True):
    """Drop `fixture`'s claim, optionally only if `match_id` still holds it.

    Passing match_id makes the release safe to call from a match's own
    completion path: a stale match finishing late cannot clear the claim a
    *different*, still-running match has since taken. Omit it to void any
    claim, which is what resetting a fixture wants.

    commit=False leaves the write to the caller's transaction, for callers
    (re-simulation, bracket resets) that are mid-way through one.
    """
    from database import db
    from database.models import TournamentFixture

    filters = [TournamentFixture.id == fixture.id]
    if match_id is not None:
        filters.append(TournamentFixture.active_match_id == match_id)

    updated = (
        db.session.query(TournamentFixture)
        .filter(*filters)
        .update({"active_match_id": None}, synchronize_session=False)
    )
    if commit:
        db.session.commit()
        db.session.refresh(fixture)
    return bool(updated)


def active_match(fixture, is_live):
    """The id of the match actually still running on `fixture`, or None.

    `is_live(match_id)` is supplied by the caller (the route layer owns the
    match files and the in-memory instance cache) and answers whether that
    match can still be resumed. A claim whose match is gone — its JSON aged
    out of data/matches, or was deleted — is stale and released here rather
    than locking the fixture out of play forever.
    """
    claimed = getattr(fixture, "active_match_id", None)
    if not claimed:
        return None
    if is_live(claimed):
        return claimed
    release_fixture(fixture, claimed)
    return None


def settled_by_other_match(fixture, match_id):
    """The id of a *different* match that already completed `fixture`, if any.

    A fixture carries one result. When a second match finishes on one that is
    already Completed, recording it repoints fixture.match_id and
    winner_team_id at the newcomer while update_standings no-ops on
    standings_applied — so the points table keeps the first result, the
    fixture card shows the second, and the first match is orphaned with its
    career and tournament stats still counted.

    claim_fixture() makes this unreachable for matches started after it
    shipped; this is what catches one that was already in flight.
    """
    if fixture.status != "Completed":
        return None
    settled_by = fixture.match_id
    if not settled_by or settled_by == match_id:
        return None
    return settled_by
