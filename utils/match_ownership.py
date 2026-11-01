"""Current DB ownership outranks historical creator snapshots after email changes."""
from database import db
from database.models import Match


def owns_match_snapshot(data, user_id, match_id=None):
    match_id = match_id or data.get('match_id')
    if match_id:
        archived = db.session.get(Match, match_id)
        if archived is not None:
            return archived.user_id == user_id
    return data.get('created_by') == user_id


def owns_archive(path, user_id, *, conn=None):
    """Pinned exports use stable ownership; unpinned files retain legacy rules."""
    from pathlib import Path
    import hashlib
    from sqlalchemy import select
    from database.models import IdentityArchiveOwner, User
    path = Path(path)
    if not path.name.lower().endswith('.zip') or path.is_symlink():
        return False
    executor = conn if conn is not None else db.session
    table = IdentityArchiveOwner.__table__
    row = executor.execute(select(table).where(table.c.filename == path.name)).mappings().first()
    if row is None:
        return f'_{user_id}_' in path.name
    owner = executor.execute(select(User.id).where(User.stable_id == row['user_stable_id'])).scalar_one_or_none()
    if owner != user_id:
        return False
    try:
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest() == row['content_hash']
    except OSError:
        return False
