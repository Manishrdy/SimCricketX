"""Replay receipts, retryable artifact cleanup, and invalidated match IDs."""


def run_migration(db, app):
    from database.models import FixtureReplay, VoidedFixtureMatch
    with app.app_context():
        FixtureReplay.__table__.create(db.engine, checkfirst=True)
        VoidedFixtureMatch.__table__.create(db.engine, checkfirst=True)
