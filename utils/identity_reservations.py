"""Transactional reservation primitive for the future signup integration."""
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from database.models import IdentityEmailReservation, User
from utils.identity_rules import InvalidEmail, normalize_email, validate_new_email


class IdentityConflict(ValueError):
    """The canonical address is already occupied; no account details exposed."""


def reserve_new_email(conn, address):
    """Reserve and return a validated identity inside a caller-owned transaction.

    Use ``with engine.begin() as conn`` and write the account on that SAME
    connection. Let failures escape the context so both writes roll back. This
    function never commits. It must run before other reads/writes in the signup
    transaction: its first SQL statement acquires SQLite's write lock. Busy/IO
    failures propagate; callers must not turn them into successful signups.

    Legacy write paths are not protected until Feature 6 integrates this helper.
    The fallback handles accounts that those paths created since migration.
    """
    identity = validate_new_email(address)
    reservations = IdentityEmailReservation.__table__
    result = conn.execute(insert(reservations).values(
        canonical_email=identity.canonical
    ).on_conflict_do_nothing(index_elements=["canonical_email"]))
    if result.rowcount != 1:
        raise IdentityConflict("identity_unavailable")

    users = User.__table__
    if conn.execute(select(users.c.id).where(
        users.c.canonical_email == identity.canonical
    ).limit(1)).first():
        raise IdentityConflict("identity_unavailable")
    # Until all email writers maintain metadata, even stale non-null values can
    # exist after legacy email changes. Verify stored keys under the write lock.
    for email in conn.execute(select(users.c.id)).scalars():
        try:
            existing = normalize_email(email)
        except InvalidEmail:
            continue
        if existing.canonical == identity.canonical:
            raise IdentityConflict("identity_unavailable")
    return identity
