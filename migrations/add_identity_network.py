"""Add network policy storage without classifying users or enabling enforcement."""
from database.models import (IdentityNetworkCache, IdentityNetworkBudget, IdentityRelayFeed,
    IdentityNetworkAppeal, IdentityNetworkAction, IdentityNetworkEvent)
MODELS = (IdentityNetworkCache, IdentityNetworkBudget, IdentityRelayFeed,
          IdentityNetworkAppeal, IdentityNetworkAction, IdentityNetworkEvent)


def run_migration(db, app):
    with app.app_context(), db.engine.begin() as conn:
        for model in MODELS:
            model.__table__.create(conn, checkfirst=True)
