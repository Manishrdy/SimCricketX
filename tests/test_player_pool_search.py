"""Pool search: ranking, forgiving matching, and paging.

Squad building searches this endpoint on every keystroke, so the order it
returns matters as much as the set: what was typed should come back at the top,
and a name the user cannot easily type (accents, curly apostrophes) should still
be reachable from its plain-ASCII spelling.
"""

import pytest

from app import db
from database.models import MasterPlayer, UserPlayer
from routes.player_pool_routes import MAX_POOL_PAGE_LIMIT

SEARCH_URL = "/api/player-pool/search"

POOL_NAMES = [
    ("Josh Hazlewood", "Bowler"),
    ("Josh Butler", "Batsman"),
    ("Avaniben Joshi", "All-rounder"),
    ("Jos Buttler", "Wicketkeeper"),
    ("Shaun Fouché", "Bowler"),
    ("Will O’Rourke", "Bowler"),
    ("Maria Castiñeiras", "Batsman"),
    ("Harry Johnson", "Batsman"),
]


@pytest.fixture
def stocked_pool(app):
    for name, role in POOL_NAMES:
        db.session.add(MasterPlayer(name=name, role=role, batting_rating=50,
                                    bowling_rating=50, fielding_rating=50))
    db.session.commit()
    return POOL_NAMES


def _names(client, query, **params):
    params.setdefault("limit", 20)
    params["q"] = query
    res = client.get(SEARCH_URL, query_string=params)
    assert res.status_code == 200
    return [p["name"] for p in res.get_json()["players"]]


def test_exact_prefix_outranks_a_mid_name_match(authenticated_client, stocked_pool):
    names = _names(authenticated_client, "josh")
    assert names[:2] == ["Josh Butler", "Josh Hazlewood"]
    # The substring match is still returned, just below the real prefixes.
    assert names.index("Avaniben Joshi") > names.index("Josh Hazlewood")


def test_typing_matches_a_later_word(authenticated_client, stocked_pool):
    assert _names(authenticated_client, "hazle") == ["Josh Hazlewood"]


def test_multiple_tokens_match_across_words(authenticated_client, stocked_pool):
    names = _names(authenticated_client, "jo ha")
    assert names[0] == "Josh Hazlewood"
    assert "Harry Johnson" in names


def test_accents_are_reachable_from_plain_ascii(authenticated_client, stocked_pool):
    assert _names(authenticated_client, "fouche") == ["Shaun Fouché"]
    assert _names(authenticated_client, "castineiras") == ["Maria Castiñeiras"]


def test_punctuation_is_optional(authenticated_client, stocked_pool):
    assert _names(authenticated_client, "orourke") == ["Will O’Rourke"]
    assert _names(authenticated_client, "o'rourke") == ["Will O’Rourke"]


def test_every_prefix_of_a_name_finds_it(authenticated_client, stocked_pool):
    # The symptom that started this: partial words must match, not just whole ones.
    for prefix in ["j", "jo", "jos", "josh", "josh ", "josh h", "josh haz"]:
        assert "Josh Hazlewood" in _names(authenticated_client, prefix), prefix


def test_role_filter_still_applies(authenticated_client, stocked_pool):
    names = _names(authenticated_client, "josh", role="Bowler")
    assert names == ["Josh Hazlewood"]


def test_override_is_searchable_by_its_new_name(authenticated_client, regular_user, stocked_pool):
    master = MasterPlayer.query.filter_by(name="Josh Butler").one()
    db.session.add(UserPlayer(user_id=regular_user.id, master_player_id=master.id,
                              name="Joshua Butler-Smith", role="Batsman",
                              batting_rating=70, bowling_rating=20, fielding_rating=60))
    db.session.commit()

    assert "Joshua Butler-Smith" in _names(authenticated_client, "butler-smith")
    # The shadowed global name is gone for this user, not duplicated.
    assert "Josh Butler" not in _names(authenticated_client, "josh")


def test_custom_players_are_searchable(authenticated_client, regular_user, stocked_pool):
    db.session.add(UserPlayer(user_id=regular_user.id, master_player_id=None,
                              name="Josh Homegrown", role="All-rounder",
                              batting_rating=60, bowling_rating=60, fielding_rating=60))
    db.session.commit()
    assert "Josh Homegrown" in _names(authenticated_client, "josh")


def test_total_counts_all_matches_while_a_page_is_returned(authenticated_client, stocked_pool):
    res = authenticated_client.get(SEARCH_URL, query_string={"q": "jo", "limit": 2, "offset": 0})
    body = res.get_json()
    assert len(body["players"]) == 2
    assert body["total"] > 2
    assert body["has_more"] is True

    rest = authenticated_client.get(SEARCH_URL, query_string={"q": "jo", "limit": 20, "offset": 2})
    # Paging walks the same ranked list without repeating or skipping a row.
    first_page = [p["name"] for p in body["players"]]
    second_page = [p["name"] for p in rest.get_json()["players"]]
    assert not set(first_page) & set(second_page)
    assert len(first_page) + len(second_page) == body["total"]


def test_no_query_returns_the_pool_alphabetically(authenticated_client, stocked_pool):
    names = _names(authenticated_client, "")
    assert names == sorted(names, key=lambda n: n.lower())
    assert len(names) == len(POOL_NAMES)


def test_page_limit_is_capped(authenticated_client, stocked_pool):
    res = authenticated_client.get(SEARCH_URL, query_string={"limit": 99999})
    assert res.status_code == 200
    assert len(res.get_json()["players"]) <= MAX_POOL_PAGE_LIMIT


def test_nonsense_query_matches_nothing(authenticated_client, stocked_pool):
    res = authenticated_client.get(SEARCH_URL, query_string={"q": "zzzqqq"})
    body = res.get_json()
    assert body["players"] == [] and body["total"] == 0
