"""Tournament venue pools using shared QA identities and isolated databases."""
import pytest
from database import db
from database.models import Tournament
from tests.test_fixture_replay import qa
from tests.test_fixture_stadium_selection import setup_case
from tests.test_stadiums import read_created
from services.stadium_tuning import create_personal
from services.stadiums import neutral_profile


@pytest.fixture
def venues(qa):
    rows = [create_personal(qa.user, {**neutral_profile(), 'name': f'Pool {i}'}) for i in range(16)]
    db.session.commit()
    return [v.id for v in rows]


@pytest.mark.parametrize('mode', ['round_robin', 'double_round_robin', 'knockout', 'round_robin_knockout', 'double_round_robin_knockout', 'ipl_style', 'custom_series'])
def test_assigns_every_fixture(qa, venues, mode, monkeypatch):
    import itertools
    choices = itertools.cycle(venues[:2])
    monkeypatch.setattr('engine.tournament_engine.random.choice', lambda pool: next(choices))
    teams = [t.id for t in qa.teams]
    t = qa.engine.create_tournament('Pool cup', qa.user, teams[:2] if mode == 'custom_series' else teams,
        mode=mode, stadium_ids=venues[:2], series_config={'matches':[{'home':0}, {'home':1}]})
    assert t.stadium_ids == venues[:2]
    assert {f.stadium_id for f in t.fixtures} == set(venues[:2])
    assert all(f.stadium_id in venues[:2] for f in t.fixtures)


@pytest.mark.parametrize('count', [0, 1, 15, 16])
def test_creation_bounds(qa, venues, count):
    response = qa.client.post('/tournaments/create', data=dict(name='Pool cup', mode='round_robin',
        team_ids=[t.id for t in qa.teams], stadium_ids=venues[:count]))
    assert response.status_code == 302
    t = Tournament.query.filter_by(name='Pool cup').first()
    assert bool(t) == (1 <= count <= 15)
    if t:
        assert t.stadium_ids == venues[:count]


@pytest.mark.parametrize('choice', ['outside', 'empty', 'allowed', 'omitted'])
def test_setup_enforces_pool(qa, setup_case, choice):
    c = setup_case
    c.f.tournament.stadium_ids = [c.previous.id, c.other.id]
    db.session.commit()
    page = qa.client.get(f'/match/setup?fixture_id={c.f.id}').get_data(as_text=True)
    assert f'<option value="{c.home.id}"' not in page
    assert f'<option value="{c.other.id}"' in page
    payload = dict(c.payload)
    if choice != 'omitted':
        payload['stadium_id'] = {'outside':c.home.id, 'empty':None, 'allowed':c.other.id}[choice]
    response = qa.client.post('/match/setup', json=payload)
    if choice in ('outside', 'empty'):
        assert response.status_code == 400
        assert c.f.active_match_id is None
    else:
        data, path = read_created(response)
        assert data['stadium_id'] == (c.other.id if choice == 'allowed' else c.previous.id)
        path.unlink(missing_ok=True)


def test_rejects_private_retired_and_unadopted(qa, setup_case):
    from services.stadiums import create_custom
    c = setup_case
    c.other.status = 'retired'
    legacy = create_custom(qa.user, neutral_profile())
    db.session.commit()
    for ident in (c.foreign.id, c.other.id, legacy.id, 'missing'):
        with pytest.raises(ValueError):
            qa.engine.create_tournament('Invalid', qa.user, [t.id for t in qa.teams], stadium_ids=[ident])


def test_migration_preserves_legacy_and_is_repeatable(tmp_path):
    from flask import Flask
    from sqlalchemy import create_engine, text
    from types import SimpleNamespace
    from migrations.add_tournament_stadiums import run_migration
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE tournaments (id INTEGER PRIMARY KEY)'))
        conn.execute(text('INSERT INTO tournaments (id) VALUES (1)'))
    for _ in range(2):
        run_migration(SimpleNamespace(engine=engine), Flask(__name__))
    with engine.connect() as conn:
        assert conn.execute(text('SELECT stadium_ids FROM tournaments')).scalar() is None


def test_retry_preserves_original_pool(qa, venues):
    kwargs = dict(name='Retry pool', user_id=qa.user, team_ids=[t.id for t in qa.teams], creation_token='same-request')
    first = qa.engine.create_tournament(**kwargs, stadium_ids=venues[:2])
    assignments = [f.stadium_id for f in first.fixtures]
    second = qa.engine.create_tournament(**kwargs, stadium_ids=venues[2:4])
    assert second.id == first.id
    assert second.stadium_ids == venues[:2]
    assert [f.stadium_id for f in second.fixtures] == assignments


def test_tour_setup_keeps_unrestricted_choice(qa, setup_case):
    from engine.tour_engine import create_tour
    c = setup_case
    tour = create_tour('Unchanged tour', qa.user, qa.teams[0].id, qa.teams[1].id,
                      {'T20': 2}, ['T20'])
    series = tour.series[0]
    assert series.stadium_ids is None
    assert all(f.stadium_id is None for f in series.fixtures)
    f = series.fixtures[0]
    page = qa.client.get(f'/match/setup?fixture_id={f.id}')
    assert page.status_code == 200
    assert f'<option value="{c.other.id}"' in page.get_data(as_text=True)
    payload = {**c.payload, 'fixture_id': f.id, 'tournament_id': series.id,
               'team_home': f.home_team_id, 'team_away': f.away_team_id, 'stadium_id': c.other.id}
    data, path = read_created(qa.client.post('/match/setup', json=payload))
    assert data['stadium_id'] == c.other.id
    path.unlink(missing_ok=True)


def test_creation_page_offers_only_playable_owned_venues(qa, setup_case):
    c = setup_case
    page = qa.client.get('/tournaments/create')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert f'name="stadium_ids" value="{c.previous.id}"' in html
    assert c.foreign.id not in html
