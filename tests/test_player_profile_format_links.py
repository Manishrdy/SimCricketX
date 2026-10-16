"""Player-profile format selector must follow linked per-format Player rows."""
import uuid
from datetime import datetime

from app import db
from database.models import (
    Match as DBMatch, MatchScorecard, Player as DBPlayer,
    Team as DBTeam, TeamProfile as DBTeamProfile,
)
from engine.stats_service import linked_format_players


def _squad(user, formats, name="Linked Star"):
    team = DBTeam(user_id=user.id, name="Link XI", short_code="LNK")
    db.session.add(team)
    db.session.flush()
    rows = {}
    for fmt in formats:
        prof = DBTeamProfile(team_id=team.id, format_type=fmt)
        db.session.add(prof)
        db.session.flush()
        p = DBPlayer(team_id=team.id, profile_id=prof.id, name=name, role="Batsman")
        db.session.add(p)
        db.session.flush()
        rows[fmt] = p
    db.session.commit()
    return team, rows


def _score(user, team, player, fmt, runs):
    mid = str(uuid.uuid4())
    db.session.add(DBMatch(id=mid, user_id=user.id, home_team_id=team.id,
                           away_team_id=team.id, match_format=fmt, date=datetime.utcnow()))
    db.session.add(MatchScorecard(match_id=mid, player_id=player.id, team_id=team.id,
                                  record_type="batting", innings_number=1, runs=runs,
                                  balls=10, is_out=True))
    db.session.commit()


def test_links_across_profiles_and_hundred_shares_t20(regular_user):
    _, rows = _squad(regular_user, ["T20", "ListA", "T10"])
    links = linked_format_players(rows["T20"])
    assert links == {"T20": rows["T20"].id, "Hundred": rows["T20"].id,
                     "T10": rows["T10"].id, "ListA": rows["ListA"].id}
    assert "FC" not in links


def test_t20_only_player_supports_only_t20_and_hundred(regular_user):
    _, rows = _squad(regular_user, ["T20"])
    assert set(linked_format_players(rows["T20"])) == {"T20", "Hundred"}


def test_different_name_not_linked(regular_user):
    team, rows = _squad(regular_user, ["T20"])
    prof = DBTeamProfile(team_id=team.id, format_type="ListA")
    db.session.add(prof)
    db.session.flush()
    db.session.add(DBPlayer(team_id=team.id, profile_id=prof.id, name="Someone Else"))
    db.session.commit()
    assert "ListA" not in linked_format_players(rows["T20"])


def test_route_redirects_to_sibling_row_with_stats(authenticated_client, regular_user):
    team, rows = _squad(regular_user, ["T20", "ListA"])
    _score(regular_user, team, rows["ListA"], "ListA", 77)
    _score(regular_user, team, rows["T20"], "T20", 12)
    resp = authenticated_client.get(f"/player/{rows['T20'].id}?match_format=ListA")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith(f"/player/{rows['ListA'].id}")
    page = authenticated_client.get(resp.headers["Location"])
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert f'data-player-id="{rows["T20"].id}"' in html
    assert 'value="FC"' not in html


def test_selector_hides_formats_without_matches(authenticated_client, regular_user):
    team, rows = _squad(regular_user, ["T20", "ListA", "FC"])
    _score(regular_user, team, rows["T20"], "T20", 30)
    _score(regular_user, team, rows["ListA"], "ListA", 40)
    html = authenticated_client.get(f"/player/{rows['T20'].id}?match_format=T20").get_data(as_text=True)
    assert 'value="ListA"' in html and 'value="T20"' in html
    assert 'value="FC"' not in html and 'value="T10"' not in html
