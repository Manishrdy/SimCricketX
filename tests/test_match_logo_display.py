"""Team logos across /match/setup and /match/<match_id> (pre-match shell + score banner)."""

import copy
import io
import json
import os
import re
import uuid

from PIL import Image

from database import db
from database.models import Team as DBTeam
from tests.test_fc_format import _squad


def _png_bytes(color=(200, 30, 30, 255)):
    buf = io.BytesIO()
    Image.new("RGBA", (128, 128), color).save(buf, format="PNG")
    return buf.getvalue()


def _upload_logo(client, team_id):
    res = client.post(
        f"/team/{team_id}/logo",
        data={"logo": (io.BytesIO(_png_bytes()), "logo.png")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 200
    return res.get_json()


def _match_data(user_id, home_team, away_team, include_ids=True):
    data = {
        "match_id": str(uuid.uuid4()),
        "created_by": user_id,
        "timestamp": "20261114205000",
        "team_home": f"{home_team.short_code}_{user_id}",
        "team_away": f"{away_team.short_code}_{user_id}",
        "stadium": "Test Ground",
        "pitch": "Flat",
        "toss": "Heads",
        "toss_winner": home_team.short_code,
        "toss_decision": "Bat",
        "match_format": "T20",
        "simulation_mode": "auto",
        "rain_probability": 0.0,
        "weather_forecast": "clear",
        "playing_xi": {
            "home": copy.deepcopy(_squad(home_team.short_code)),
            "away": copy.deepcopy(_squad(away_team.short_code)),
        },
        "substitutes": {"home": [], "away": []},
    }
    if include_ids:
        data["team_home_id"] = home_team.id
        data["team_away_id"] = away_team.id
    return data


def _render_match_detail(app_module, client, data):
    match_dir = os.path.join(app_module.PROJECT_ROOT, "data", "matches")
    os.makedirs(match_dir, exist_ok=True)
    path = os.path.join(match_dir, f"match_{data['match_id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        resp = client.get(f"/match/{data['match_id']}")
        assert resp.status_code == 200
        return resp.get_data(as_text=True)
    finally:
        app_module.MATCH_INSTANCES.pop(data["match_id"], None)
        try:
            os.remove(path)
        except OSError:
            pass


def _create_second_team(regular_user, team_stadium_id, name="Away XI", short_code="AWY"):
    team = DBTeam(
        name=name,
        short_code=short_code,
        user_id=regular_user.id,
        home_ground="Test Ground",
        stadium_id=team_stadium_id,
        team_color="#1d4ed8",
    )
    db.session.add(team)
    db.session.commit()
    return team


class TestMatchSetupLogos:
    def test_setup_shows_logo_when_present_and_fallback_when_absent(
        self, authenticated_client, test_team, regular_user, team_stadium_id
    ):
        away_team = _create_second_team(regular_user, team_stadium_id)
        uploaded = _upload_logo(authenticated_client, test_team.id)

        resp = authenticated_client.get("/match/setup")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)

        assert "js/team_logo_fallback.js" in html
        assert uploaded["url"] in html
        assert f"/team/{away_team.id}/logo.png" not in html
        assert 'id="ticket-home-avatar"' in html
        assert 'id="ticket-away-avatar"' in html
        assert "refreshTicketTeams" in html

        match_json = re.search(
            r'<script id="teams-data" type="application/json">\s*(.*?)\s*</script>',
            html,
            re.DOTALL,
        )
        assert match_json is not None
        teams_payload = json.loads(match_json.group(1))
        by_id = {t["id"]: t for t in teams_payload}
        assert by_id[test_team.id]["logo_url"] == uploaded["url"]
        assert by_id[away_team.id]["logo_url"] is None


class TestMatchDetailLogos:
    def test_prematch_and_banner_show_logos_when_both_teams_have_logos(
        self, app, authenticated_client, test_team, regular_user, team_stadium_id
    ):
        import app as app_module

        away_team = _create_second_team(regular_user, team_stadium_id)
        home_logo = _upload_logo(authenticated_client, test_team.id)["url"]
        away_logo = _upload_logo(authenticated_client, away_team.id)["url"]

        html = _render_match_detail(
            app_module, authenticated_client, _match_data(regular_user.id, test_team, away_team)
        )

        assert "js/team_logo_fallback.js" in html

        hero_match = re.search(r'<h2 id="prematch-title".*?</h2>', html, re.DOTALL)
        assert hero_match is not None
        hero = hero_match.group(0)
        assert hero.count("has-logo") == 2
        assert home_logo in hero
        assert away_logo in hero
        assert test_team.short_code in hero
        assert away_team.short_code in hero

        banner_match = re.search(r'<div class="sb-team sb-batting".*?</div>', html, re.DOTALL)
        assert banner_match is not None
        banner = banner_match.group(0)
        assert "has-logo" in banner
        assert home_logo in banner
        assert test_team.short_code in banner

        logos_json = re.search(
            r'<script id="team-logos-json" type="application/json">\s*(.*?)\s*</script>',
            html,
            re.DOTALL,
        )
        assert logos_json is not None
        assert json.loads(logos_json.group(1)) == {"home": home_logo, "away": away_logo}

    def test_prematch_and_banner_fall_back_to_short_codes_when_logos_absent(
        self, app, authenticated_client, test_team, regular_user, team_stadium_id
    ):
        import app as app_module

        away_team = _create_second_team(regular_user, team_stadium_id)
        html = _render_match_detail(
            app_module, authenticated_client, _match_data(regular_user.id, test_team, away_team)
        )

        hero_match = re.search(r'<h2 id="prematch-title".*?</h2>', html, re.DOTALL)
        assert hero_match is not None
        hero = hero_match.group(0)
        assert "has-logo" not in hero
        assert "<img" not in hero
        assert test_team.short_code in hero
        assert away_team.short_code in hero

        banner_match = re.search(r'<div class="sb-team sb-batting".*?</div>', html, re.DOTALL)
        assert banner_match is not None
        banner = banner_match.group(0)
        assert "has-logo" not in banner
        assert "<img" not in banner
        assert test_team.short_code in banner

    def test_prematch_supports_mixed_logo_availability_and_legacy_code_lookup(
        self, app, authenticated_client, test_team, regular_user, team_stadium_id
    ):
        import app as app_module

        away_team = _create_second_team(regular_user, team_stadium_id)
        away_logo = _upload_logo(authenticated_client, away_team.id)["url"]

        html = _render_match_detail(
            app_module,
            authenticated_client,
            _match_data(regular_user.id, test_team, away_team, include_ids=False),
        )

        hero_match = re.search(r'<h2 id="prematch-title".*?</h2>', html, re.DOTALL)
        assert hero_match is not None
        hero = hero_match.group(0)
        assert hero.count("has-logo") == 1
        assert away_logo in hero
        assert f"/team/{test_team.id}/logo.png" not in hero
        assert test_team.short_code in hero
