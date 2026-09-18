"""History search must apply before pagination and preserve user/filter scope."""
from datetime import datetime, timedelta
import re

import pytest
from app import db
from database.models import Match, Team


@pytest.fixture
def history(regular_user):
    teams = [Team(user_id=regular_user.id, name=name, short_code=str(i))
             for i, name in enumerate(['Common', 'Opponent', 'Rare 100%_XI'])]
    db.session.add_all(teams)
    db.session.flush()
    now = datetime.utcnow()
    for i in range(26):
        # All rare-team matches start on the unfiltered second page.
        db.session.add(Match(id=f'history-{i:02}', user_id=regular_user.id,
                             home_team_id=teams[0 if i < 12 else 2].id,
                             away_team_id=teams[1].id, date=now - timedelta(minutes=i),
                             match_format='T20' if i != 25 else 'ListA'))
    db.session.commit()


def cards(response):
    assert response.status_code == 200
    return re.findall(r'id="match-(history-\d+)"', response.get_data(as_text=True))


def test_search_finds_later_matches_and_paginates_filtered_results(authenticated_client, history):
    first = cards(authenticated_client.get('/my-matches'))
    assert len(first) == 12
    assert 'history-12' not in first
    assert 'history-12' in cards(authenticated_client.get('/my-matches?page=2'))
    response = authenticated_client.get('/my-matches?q=+rArE+&format=T20')
    assert cards(response) == [f'history-{i:02}' for i in range(12, 24)]
    assert 'name="q" form="filter-bar" value="rArE"' in response.get_data(as_text=True)
    second = authenticated_client.get('/my-matches?q=rare&format=T20&page=2')
    assert cards(second) == ['history-24']
    assert 'id="load-more-btn"' not in second.get_data(as_text=True)


def test_search_matches_away_team_and_treats_wildcards_literally(authenticated_client, history):
    assert len(cards(authenticated_client.get('/my-matches?q=opponent'))) == 12
    assert cards(authenticated_client.get('/my-matches?q=%25_')) == [f'history-{i:02}' for i in range(12, 24)]
    response = authenticated_client.get('/my-matches?q=missing')
    assert cards(response) == []
    assert 'No matches found' in response.get_data(as_text=True)


def test_search_never_returns_another_users_matches(authenticated_client, history, admin_user):
    match = Match.query.filter_by(id='history-12').one()
    match.user_id = admin_user.id
    db.session.commit()
    assert 'history-12' not in cards(authenticated_client.get('/my-matches?q=rare'))
