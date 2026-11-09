"""Add dormant legacy-email campaigns; never enroll or delete at startup."""
from database.models import IdentityEmailCampaign, IdentityCampaignMember, IdentityCampaignAction, IdentityCampaignFile


def run_migration(db, app):
    with app.app_context(), db.engine.begin() as conn:
        for model in (IdentityEmailCampaign, IdentityCampaignMember, IdentityCampaignAction, IdentityCampaignFile):
            model.__table__.create(conn, checkfirst=True)
