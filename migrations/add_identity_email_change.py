"""Add verified email-change workflow without changing accounts."""
from database.models import IdentityEmailChange


def run_migration(db, app):
    with app.app_context(), db.engine.begin() as conn:
        IdentityEmailChange.__table__.create(conn, checkfirst=True)
