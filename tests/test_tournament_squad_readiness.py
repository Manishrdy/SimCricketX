"""Creation must reject teams that cannot field the selected format."""
import pytest

from database import db
from database.models import Tournament, TournamentFixture, TournamentTeam


@pytest.mark.parametrize("fmt", ["T20", "ListA", "FC"])
@pytest.mark.parametrize("problem, message", [
    ("draft_missing_profile", "draft"),
    ("draft", "draft"),
    ("missing_profile", "Squad not created"),
    ("short_squad", "between 11 and 25"),
    ("oversized_squad", "between 11 and 25"),
    ("no_bowlers", "Bowlers/All-rounders"),
    ("no_keeper_role", "Wicketkeeper"),
    ("no_captain", "Captain must be selected"),
    ("no_keeper_selection", "Wicketkeeper must be selected"),
])
def test_invalid_squad_rejected(authenticated_client, ready_tournament_teams, fmt, problem, message):
    home, away = ready_tournament_teams
    profile = next(p for p in away.profiles if p.format_type == fmt)
    if problem in ("draft", "draft_missing_profile"):
        away.is_draft = True
    if problem in ("missing_profile", "draft_missing_profile"):
        db.session.delete(profile)
    elif problem == "short_squad":
        db.session.delete(profile.players[-1])
    elif problem == "oversized_squad":
        from database.models import Player
        for index in range(15):
            db.session.add(Player(team_id=away.id, profile_id=profile.id,
                                  name=f"Extra {index}", role="Batsman"))
    elif problem == "no_bowlers":
        for player in profile.players[1:]:
            player.role = "Batsman"
    elif problem == "no_keeper_role":
        profile.players[0].role = "Batsman"
    elif problem == "no_captain":
        profile.players[0].is_captain = False
    elif problem == "no_keeper_selection":
        profile.players[0].is_wicketkeeper = False
    db.session.commit()

    response = authenticated_client.post("/tournaments/create", data={
        "name": "Invalid squad cup", "team_ids": [home.id, away.id],
        "match_format": fmt, "mode": "round_robin",
    })

    assert response.status_code == 302
    assert response.location.endswith("/tournaments/create")
    with authenticated_client.session_transaction() as session:
        errors = " ".join(text for category, text in session["_flashes"] if category == "error")
    assert away.name in errors
    assert fmt in errors
    assert message in errors
    assert Tournament.query.count() == 0
    assert TournamentTeam.query.count() == 0
    assert TournamentFixture.query.count() == 0


@pytest.mark.parametrize("fmt", ["T20", "ListA", "FC"])
@pytest.mark.parametrize("mode", ["round_robin", "knockout", "custom_series"])
def test_ready_squads_create_tournament(authenticated_client, ready_tournament_teams, fmt, mode):
    home, away = ready_tournament_teams
    unused = "FC" if fmt != "FC" else "T20"
    db.session.delete(next(p for p in away.profiles if p.format_type == unused))
    db.session.commit()
    response = authenticated_client.post("/tournaments/create", data={
        "name": "Ready cup", "team_ids": [home.id, away.id],
        "match_format": fmt, "mode": mode, "series_matches": 3,
    })
    tournament = Tournament.query.one()
    assert response.status_code == 302
    assert response.location.endswith(f"/tournaments/{tournament.id}")
    assert tournament.format_type == fmt
    assert TournamentTeam.query.count() == 2
    assert TournamentFixture.query.count() == (3 if mode == "custom_series" else 1)
