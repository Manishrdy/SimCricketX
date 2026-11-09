"""Add merge workflow; no account mutations or backfill."""
from database.models import IdentityMerge


def run_migration(db, app):
    with app.app_context(), db.engine.begin() as conn:
        IdentityMerge.__table__.create(conn, checkfirst=True)
