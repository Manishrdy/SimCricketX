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
