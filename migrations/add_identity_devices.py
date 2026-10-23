"""Add device evidence storage only; no collection or backfill on migration."""
from database.models import IdentityDeviceSighting


def run_migration(db, app):
    with app.app_context(), db.engine.begin() as conn:
        IdentityDeviceSighting.__table__.create(conn, checkfirst=True)
