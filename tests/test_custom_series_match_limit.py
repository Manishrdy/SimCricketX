"""The custom-series form's 1–7 match limit must hold for direct POSTs."""

import pytest

from database.models import Tournament, TournamentFixture, TournamentTeam
from engine.tournament_engine import TournamentEngine


def create_series(client, teams, count):
    payload = {
        "name": "Bounded series", "mode": "custom_series", "match_format": "T20",
        "team_ids": [team.id for team in teams],
    }
    if count is not None:
        payload["series_matches"] = count
    return client.post("/tournaments/create", data=payload)


def test_eight_match_series_is_rejected(authenticated_client, ready_tournament_teams):
    response = create_series(authenticated_client, ready_tournament_teams, "8")

    assert response.status_code == 302
    assert response.location.endswith("/tournaments/create")
    assert Tournament.query.count() == 0
    assert TournamentTeam.query.count() == 0
    assert TournamentFixture.query.count() == 0


@pytest.mark.parametrize("count", ["0", "-1", "8", "1000000000", "1.5", "abc", ""])
def test_invalid_count_never_reaches_engine(
    authenticated_client, ready_tournament_teams, monkeypatch, count,
):
    calls = []

    def unexpected_create(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("Invalid count reached fixture generation")

    monkeypatch.setattr(TournamentEngine, "create_tournament", unexpected_create)
    response = create_series(authenticated_client, ready_tournament_teams, count)

    assert response.status_code == 302
    assert response.location.endswith("/tournaments/create")
    assert calls == []
    with authenticated_client.session_transaction() as session:
        assert ("error", "Custom series must contain between 1 and 7 matches.") in session["_flashes"]
    assert Tournament.query.count() == 0
    assert TournamentTeam.query.count() == 0
    assert TournamentFixture.query.count() == 0


@pytest.mark.parametrize("count, expected", [("1", 1), ("2", 2), ("7", 7), (None, 3)])
def test_valid_counts_and_default(authenticated_client, ready_tournament_teams, count, expected):
    response = create_series(authenticated_client, ready_tournament_teams, count)

    tournament = Tournament.query.one()
    assert response.status_code == 302
    assert response.location.endswith(f"/tournaments/{tournament.id}")
    assert TournamentTeam.query.count() == 2
    assert TournamentFixture.query.count() == expected
