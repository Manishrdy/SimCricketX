"""Add signup enforcement storage; no backfill, activation or account changes."""
from database.models import (IdentitySignupSuccess, IdentitySignupAttempt,
    IdentitySignupAppeal, IdentitySignupAction, IdentityDeletionMarker, IdentityAppealDelivery)

MODELS = (IdentitySignupSuccess, IdentitySignupAttempt, IdentitySignupAppeal,
          IdentitySignupAction, IdentityDeletionMarker, IdentityAppealDelivery)


def run_migration(db, app):
    with app.app_context(), db.engine.begin() as conn:
        for model in MODELS:
            model.__table__.create(conn, checkfirst=True)
