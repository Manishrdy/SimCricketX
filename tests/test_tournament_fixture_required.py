"""Tournament setup must always bind a match to a scheduled fixture."""

import json
from pathlib import Path

import pytest

from database import db
from database.models import Tournament, TournamentFixture


@pytest.fixture
def scheduled_fixture(regular_user, test_team, test_team_2):
    tournament = Tournament(
        name="Fixture required", user_id=regular_user.id,
        mode="round_robin", format_type="T20",
    )
    db.session.add(tournament)
    db.session.flush()
    fixture = TournamentFixture(
        tournament_id=tournament.id, home_team_id=test_team.id,
        away_team_id=test_team_2.id, round_number=1,
        stage="league", status="Scheduled",
    )
    db.session.add(fixture)
    db.session.commit()
    return fixture


def setup_payload(fixture):
    return {
        "team_home": fixture.home_team_id,
        "team_away": fixture.away_team_id,
        "overs": 20, "simulation_mode": "auto", "match_format": "T20",
    }


@pytest.mark.parametrize("fixture_value", ["omitted", None, "", 0, False])
def test_tournament_requires_fixture(authenticated_client, scheduled_fixture, fixture_value):
    from app import PROJECT_ROOT

    match_dir = Path(PROJECT_ROOT) / "data" / "matches"
    before = set(match_dir.glob("match_*.json"))
    payload = setup_payload(scheduled_fixture)
    payload["tournament_id"] = scheduled_fixture.tournament_id
    if fixture_value != "omitted":
        payload["fixture_id"] = fixture_value

    response = authenticated_client.post("/match/setup", json=payload)

    assert response.status_code == 400, response.get_json()
    assert "fixture" in response.get_json()["error"].lower()
    assert "match_id" not in response.get_json()
    assert set(match_dir.glob("match_*.json")) == before
    db.session.refresh(scheduled_fixture)
    assert scheduled_fixture.active_match_id is None
    assert scheduled_fixture.status == "Scheduled"


@pytest.mark.parametrize("context", ["friendly", "fixture_only", "tournament_and_fixture"])
def test_valid_match_contexts_still_start(authenticated_client, scheduled_fixture, context):
    from app import PROJECT_ROOT

    payload = setup_payload(scheduled_fixture)
    if context != "friendly":
        payload["fixture_id"] = scheduled_fixture.id
    if context == "tournament_and_fixture":
        payload["tournament_id"] = scheduled_fixture.tournament_id

    response = authenticated_client.post("/match/setup", json=payload)

    assert response.status_code == 200, response.get_json()
    match_id = response.get_json()["match_id"]
    saved = json.loads(
        (Path(PROJECT_ROOT) / "data" / "matches" / f"match_{match_id}.json").read_text()
    )
    db.session.refresh(scheduled_fixture)
    if context == "friendly":
        assert saved["tournament_id"] is None
        assert saved["fixture_id"] is None
        assert scheduled_fixture.active_match_id is None
    else:
        assert saved["tournament_id"] == scheduled_fixture.tournament_id
        assert saved["fixture_id"] == scheduled_fixture.id
        assert scheduled_fixture.active_match_id == match_id
