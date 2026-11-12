"""Add report-only storage; no scan or account mutation during migration."""
from database.models import IdentityAccountReport, IdentityAccountResult


def run_migration(db, app):
    with app.app_context(), db.engine.begin() as conn:
        for model in (IdentityAccountReport, IdentityAccountResult):
            model.__table__.create(conn, checkfirst=True)
