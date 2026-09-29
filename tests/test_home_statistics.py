"""Public and signed-in homepage statistics share global, UTC-based counts."""
from datetime import datetime, timedelta
import re

import pytest

from database import db
from database.models import ActiveSession, Match, SiteCounter, User
from scripts.dev_test_accounts import ACCOUNTS, logged_in_client, seed


def values(response):
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Today is UTC' in body
    for label in ('Total matches simulated', 'Matches simulated today',
                  'Total signups', 'Signups today', 'Active (5m)'):
        assert label in body
    return re.findall(r'<dd[^>]*>([\d,]+)</dd>', body)


def test_empty_public_statistics(client):
    assert values(client.get('/')) == ['0', '0', '0', '0', '0']


@pytest.mark.parametrize('viewer', [None, 'user1', 'admin'])
def test_global_statistics_and_utc_boundaries(app, viewer):
    seed(app)
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    dates = [start - timedelta(microseconds=1), start,
             start + timedelta(hours=12), start + timedelta(days=1)]
    for index, date in enumerate(dates):
        db.session.add(Match(id=f'stats-{index}', date=date))
    for key, date in zip(ACCOUNTS, dates + [start - timedelta(days=2)]):
        db.session.get(User, ACCOUNTS[key]['email']).created_at = date
    db.session.add(SiteCounter(key='matches_simulated', value=1234))
    now = datetime.utcnow()
    for token, minutes in [('recent-one', 1), ('recent-two', 2), ('idle', 6)]:
        db.session.add(ActiveSession(
            session_token=token,
            user_id=ACCOUNTS['user2' if minutes < 5 else 'user3']['email'],
            last_active=now - timedelta(minutes=minutes),
        ))
    db.session.commit()
    client = logged_in_client(app, viewer) if viewer else app.test_client()
    assert values(client.get('/')) == ['1,234', '2', '5', '2', '2' if viewer else '1']
