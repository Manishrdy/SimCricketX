"""Fresh fixture attempts may change venue; active attempts stay immutable."""
import pytest
from tests.test_fixture_replay import qa, tournament, fixture, record, reset
from tests.test_stadiums import read_created
from database import db
from services.stadiums import neutral_profile, create_custom

@pytest.mark.parametrize('stage', ['league', 'qualifier_1'])
@pytest.mark.parametrize('action', ['abandon', 'replay'])
@pytest.mark.parametrize('version', [2, 3])
def test_change_stadium_after_abandon_or_replay(qa, stage, action, version):
    def venue(name):
        profile = {**neutral_profile(), 'name':name}
        if version == 2:
            row = create_custom(qa.user, profile)
            db.session.commit()
            return row.id
        response = qa.client.post('/api/stadiums', json=profile)
        assert response.status_code == 201, response.json
        return response.json['id']
    wrong, correct = venue('Wrong ground'), venue('Correct ground')
    # Existing v2 home link is permitted; v3 permits neutral choice too.
    for team in qa.teams:
        team.stadium_id = correct
        team.venue_mode = 'stadium'
    qa.teams[0].stadium_id = wrong if version == 2 else correct
    qa.teams[0].venue_mode = 'stadium'
    db.session.commit()
    t = tournament(qa, mode='ipl_style' if stage == 'qualifier_1' else 'round_robin')
    f = fixture(qa, t, stage, 1 if stage == 'qualifier_1' else None)
    if stage == 'qualifier_1':
        for pos, name in [(2, 'eliminator'), (3, 'qualifier_2'), (4, 'final')]:
            fixture(qa, t, name, pos)
    payload = dict(team_home=f.home_team_id, team_away=f.away_team_id,
        tournament_id=t.id, fixture_id=f.id, match_format='T20', pitch='Hard', toss='Heads', stadium_id=wrong)
    data, path = read_created(qa.client.post('/match/setup', json=payload))
    assert data['stadium_id'] == wrong
    assert f.stadium_id == wrong
    if action == 'abandon':
        assert qa.client.post(f'/fixture/{f.id}/abandon').status_code == 302
    else:
        f.active_match_id = None
        record(qa, f)
        assert reset(qa, f).status_code == 200
    assert f.active_match_id is None and f.status == 'Scheduled'
    page = qa.client.get(f'/match/setup?fixture_id={f.id}')
    assert page.status_code == 200
    expected = f'const STADIUM_FIXTURE_ID = "{wrong}";'
    assert expected in page.get_data(as_text=True)
    # Existing home links remain selectable even when the remembered venue differs.
    qa.teams[0].stadium_id = correct
    db.session.commit()
    payload['stadium_id'] = correct
    new, new_path = read_created(qa.client.post('/match/setup', json=payload))
    assert new['stadium_id'] == correct
    assert f.stadium_id == correct
    assert new['conditions_snapshot']['stadium_id'] == correct
    # Repeat the reset: the latest choice is remembered, and remains editable.
    assert qa.client.post(f'/fixture/{f.id}/abandon').status_code == 302
    payload.pop('stadium_id')
    again, again_path = read_created(qa.client.post('/match/setup', json=payload))
    assert again['stadium_id'] == correct
    again_path.unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    new_path.unlink(missing_ok=True)


@pytest.fixture
def setup_case(qa):
    from types import SimpleNamespace
    from services.stadium_tuning import create_personal
    from scripts.dev_test_accounts import ACCOUNTS

    def venue(name, owner=qa.user):
        row = create_personal(owner, {**neutral_profile(), 'name': name})
        db.session.commit()
        return row

    previous, home, other = venue('Previous'), venue('Home'), venue('Other')
    foreign = venue('Private', ACCOUNTS['user2']['email'])
    for team in qa.teams:
        team.stadium_id = home.id
        team.venue_mode = 'stadium'
    t = tournament(qa)
    f = fixture(qa, t)
    f.stadium_id = previous.id
    db.session.commit()
    payload = dict(team_home=f.home_team_id, team_away=f.away_team_id,
        tournament_id=t.id, fixture_id=f.id, match_format='T20', pitch='Hard', toss='Heads')
    return SimpleNamespace(f=f, payload=payload, previous=previous, home=home,
                           other=other, foreign=foreign)


@pytest.mark.parametrize('choice', ['omitted', 'empty', 'retired_default', 'private_default'])
def test_remembered_venue_fallbacks(qa, setup_case, choice):
    case = setup_case
    expected = case.previous.id
    if choice == 'empty':
        case.payload['stadium_id'] = None
        expected = case.home.id
    elif choice == 'retired_default':
        case.previous.status = 'retired'
        expected = case.home.id
    elif choice == 'private_default':
        case.f.stadium_id = case.foreign.id
        expected = case.home.id
    db.session.commit()
    data, path = read_created(qa.client.post('/match/setup', json=case.payload))
    assert data['stadium_id'] == case.f.stadium_id == expected
    path.unlink(missing_ok=True)


@pytest.mark.parametrize('choice', ['foreign', 'retired', 'missing', 'unadopted'])
def test_invalid_explicit_venue_does_not_silently_use_previous(qa, setup_case, choice):
    case = setup_case
    if choice == 'foreign':
        selected = case.foreign.id
    elif choice == 'retired':
        case.other.status = 'retired'
        selected = case.other.id
    elif choice == 'unadopted':
        selected = create_custom(qa.user, neutral_profile()).id
    else:
        selected = 'missing-stadium'
    db.session.commit()
    response = qa.client.post('/match/setup', json={**case.payload, 'stadium_id': selected})
    assert response.status_code == 400
    assert case.f.stadium_id == case.previous.id
    assert case.f.active_match_id is None


def test_active_attempt_keeps_venue_and_snapshot(qa, setup_case):
    case = setup_case
    data, path = read_created(qa.client.post('/match/setup', json=case.payload))
    original = path.read_bytes()
    active = case.f.active_match_id
    page = qa.client.get(f'/match/setup?fixture_id={case.f.id}')
    assert page.status_code == 302
    assert active in page.location
    rejected = qa.client.post('/match/setup', json={**case.payload, 'stadium_id': case.other.id})
    assert rejected.status_code == 409
    assert case.f.active_match_id == active
    assert case.f.stadium_id == case.previous.id
    assert path.read_bytes() == original
    assert data['conditions_snapshot']['stadium_id'] == case.previous.id
    path.unlink(missing_ok=True)


def test_failed_setup_does_not_remember_new_venue(qa, setup_case, monkeypatch):
    case = setup_case
    import builtins
    original_open = builtins.open

    def fail_match_write(path, mode='r', *args, **kwargs):
        if str(path).endswith('.json') and 'match_' in str(path) and mode == 'w':
            raise OSError('Simulated match file failure')
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', fail_match_write)
    response = qa.client.post('/match/setup', json={**case.payload, 'stadium_id': case.other.id})
    assert response.status_code == 500
    assert case.f.active_match_id is None
    assert case.f.stadium_id == case.previous.id
