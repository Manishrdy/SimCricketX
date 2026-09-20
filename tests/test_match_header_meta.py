"""
Match page header chips show the format and the forecast, not an opaque id.

The header used to lead with a truncated match_id and the creation date —
neither of which tells you anything about the match you are watching. They are
replaced by the match format (with the day count for First-Class) and the
weather forecast, both of which change how the match actually plays.
"""
import copy
import json
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_fc_format import _squad

HOME = _squad("HOM")
AWAY = _squad("AWY")


def _match_data(user_id, **over):
    data = {
        "match_id": str(uuid.uuid4()), "created_by": user_id,
        "timestamp": "20260919115348",
        "team_home": "HOM_1", "team_away": "AWY_1",
        "stadium": "Waka Stadium, Perth", "pitch": "Green",
        "toss": "Heads", "toss_winner": "HOM", "toss_decision": "Bat",
        "match_format": "T20", "simulation_mode": "auto",
        "rain_probability": 0.0, "weather_forecast": "clear",
        "playing_xi": {"home": copy.deepcopy(HOME), "away": copy.deepcopy(AWAY)},
        "substitutes": {"home": [], "away": []},
    }
    data.update(over)
    return data


def _write(app_module, data):
    match_dir = os.path.join(app_module.PROJECT_ROOT, "data", "matches")
    os.makedirs(match_dir, exist_ok=True)
    path = os.path.join(match_dir, f"match_{data['match_id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def _meta_grid(html):
    """Just the header chips. The page also embeds the whole match dict as
    JSON for the client, so raw keys legitimately appear elsewhere."""
    start = html.index('class="match-meta-grid"')
    return html[start:html.index("</div>", html.index("fa-paint-roller", start))]


def _render(app_module, authenticated_client, data):
    path = _write(app_module, data)
    try:
        resp = authenticated_client.get(f"/match/{data['match_id']}")
        assert resp.status_code == 200, resp.status_code
        return resp.get_data(as_text=True)
    finally:
        app_module.MATCH_INSTANCES.pop(data["match_id"], None)
        try:
            os.remove(path)
        except OSError:
            pass


def test_header_drops_the_match_id_and_date(app, authenticated_client, regular_user):
    import app as app_module

    data = _match_data(regular_user.id)
    grid = _meta_grid(_render(app_module, authenticated_client, data))

    assert f"{data['match_id'][:8]}&hellip;" not in grid
    assert "fa-fingerprint" not in grid
    assert "fa-calendar-alt" not in grid
    assert "09-19-2026" not in grid
    # The things worth knowing are still there.
    assert "Waka Stadium, Perth" in grid
    assert "Green" in grid


def test_header_shows_format_and_forecast(app, authenticated_client, regular_user):
    import app as app_module

    grid = _meta_grid(_render(app_module, authenticated_client, _match_data(regular_user.id)))
    assert ">T20<" in grid
    assert "Clear skies" in grid


def test_list_a_and_first_class_get_their_real_names(
    app, authenticated_client, regular_user
):
    import app as app_module

    grid = _meta_grid(_render(app_module, authenticated_client,
                              _match_data(regular_user.id, match_format="ListA")))
    assert ">List A · 50 overs<" in grid

    grid = _meta_grid(_render(app_module, authenticated_client,
                              _match_data(regular_user.id, match_format="FC", days=5,
                                          weather_forecast="passing_showers")))
    # FC carries its length, which is a real setup choice (4 vs 5 days).
    assert "First-Class · 5 days" in grid
    # Forecast keys are not display strings: "passing_showers" must not leak.
    assert "Passing showers" in grid
    assert "passing_showers" not in grid


def test_day_night_is_flagged_and_a_legacy_match_simply_omits_weather(
    app, authenticated_client, regular_user
):
    import app as app_module

    grid = _meta_grid(_render(app_module, authenticated_client,
                              _match_data(regular_user.id, is_day_night=True)))
    assert "D/N" in grid

    legacy = _match_data(regular_user.id)
    legacy.pop("weather_forecast")
    grid = _meta_grid(_render(app_module, authenticated_client, legacy))
    assert "Clear skies" not in grid
    assert ">T20<" in grid  # the format chip still renders
