"""Current DB ownership outranks historical creator snapshots after email changes."""
from database import db
from database.models import Match


def owns_match_snapshot(data, user_id, match_id=None):
    match_id = match_id or data.get('match_id')
    from database.models import IdentityCampaignFile
    if match_id and db.session.query(IdentityCampaignFile.id).filter_by(match_key=str(match_id)).first():
        return False
    if match_id:
        archived = db.session.get(Match, match_id)
        if archived is not None:
            return archived.user_id == user_id
    return data.get('created_by') == user_id


def owns_archive(path, user_id, *, conn=None):
    """Pinned exports use stable ownership; unpinned files retain legacy rules."""
    return bool(owned_archives([path], user_id, conn=conn))


# SQLite builds before 3.32 cap a statement at 999 bound parameters.
_OWNERSHIP_LOOKUP_CHUNK = 400


def owned_archives(paths, user_id, *, conn=None):
    """Return the subset of ``paths`` (as a set of file names) owned by ``user_id``.

    The single source of the archive-ownership rules; ``owns_archive`` is this
    with one path. Listing a folder must call this once, not ``owns_archive``
    per file: the folder holds every user's exports, and a per-file check costs
    two or three queries for each of them on every page load.

    The lookups here are a fixed number of queries per ``_OWNERSHIP_LOOKUP_CHUNK``
    files, and only archives pinned to this user are hashed.
    """
    from pathlib import Path
    from utils.file_hash import sha256_hexdigest
    from sqlalchemy import select
    from database.models import IdentityArchiveOwner, IdentityCampaignFile, User

    by_name = {}
    for path in paths:
        path = Path(path)
        if not path.name.lower().endswith('.zip') or path.is_symlink():
            continue
        by_name[path.name] = path
    if not by_name:
        return set()

    executor = conn if conn is not None else db.session
    table = IdentityArchiveOwner.__table__
    names = list(by_name)
    held = set()      # claimed by an identity campaign that has not cleaned up yet
    pinned = {}       # filename -> (user_stable_id, content_hash)
    for start in range(0, len(names), _OWNERSHIP_LOOKUP_CHUNK):
        chunk = names[start:start + _OWNERSHIP_LOOKUP_CHUNK]
        held.update(executor.execute(
            select(IdentityCampaignFile.name).where(
                IdentityCampaignFile.area == 'archives',
                IdentityCampaignFile.name.in_(chunk),
                IdentityCampaignFile.cleaned_at.is_(None),
            )
        ).scalars())
        for row in executor.execute(select(table).where(table.c.filename.in_(chunk))).mappings():
            pinned[row['filename']] = (row['user_stable_id'], row['content_hash'])

    # A pinned file belongs to whoever currently holds its stable id. A deleted
    # owner leaves the pin NULL, which matches nobody.
    stable_ids = {stable_id for stable_id, _ in pinned.values() if stable_id is not None}
    owner_by_stable_id = {}
    if stable_ids:
        owner_by_stable_id = dict(executor.execute(
            select(User.stable_id, User.id).where(User.stable_id.in_(stable_ids))
        ).all())

    owned = set()
    marker = f'_{user_id}_'
    for name, path in by_name.items():
        if name in held:
            continue
        if name not in pinned:
            if marker in name:
                owned.add(name)
            continue
        stable_id, content_hash = pinned[name]
        if stable_id is None or owner_by_stable_id.get(stable_id) != user_id:
            continue
        try:
            with path.open('rb') as stream:
                if sha256_hexdigest(stream) == content_hash:
                    owned.add(name)
        except OSError:
            continue
    return owned
