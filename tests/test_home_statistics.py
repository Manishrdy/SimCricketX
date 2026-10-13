"""Homepage counts are global and UTC-based; only the dashboard displays them."""
from datetime import datetime, timedelta
import re

import pytest
from flask import template_rendered

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


@pytest.fixture
def rendered_templates(app):
    rendered = []

    def record(sender, template, context, **extra):
        rendered.append((template.name, context))

    with template_rendered.connected_to(record, app):
        yield rendered


def public_values(response, rendered_templates):
    """The marketing page receives counts but intentionally does not show them."""
    assert response.status_code == 200
    template, context = rendered_templates[-1]
    assert template == 'landing.html'
    body = response.get_data(as_text=True)
    assert 'Build your cricket world' in body
    assert 'aria-label="Platform statistics"' not in body
    return [context[key] for key in (
        'matches_simulated', 'matches_today', 'total_signups',
        'signups_today', 'active_users',
    )]


def test_empty_public_statistics(client, rendered_templates):
    assert public_values(client.get('/'), rendered_templates) == [0, 0, 0, 0, 0]


@pytest.mark.parametrize('viewer', [None, 'user1', 'admin'])
def test_global_statistics_and_utc_boundaries(app, viewer, rendered_templates):
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
    response = client.get('/')
    if viewer is None:
        assert public_values(response, rendered_templates) == [1234, 2, 5, 2, 1]
    else:
        assert values(response) == ['1,234', '2', '5', '2', '2']
