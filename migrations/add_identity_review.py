"""Add private identity review storage, without scanning or changing accounts."""
from database.models import IdentityReviewCase, IdentityReviewEvidence, IdentityReviewAction


def run_migration(db, app):
    with app.app_context(), db.engine.begin() as conn:
        for model in (IdentityReviewCase, IdentityReviewEvidence, IdentityReviewAction):
            model.__table__.create(conn, checkfirst=True)
