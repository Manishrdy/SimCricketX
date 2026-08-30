"""End-to-end check that the FC Ground Conditions page renders and saves."""
import json


def test_fc_ground_conditions_page_renders(authenticated_client):
    resp = authenticated_client.get("/ground-conditions?format=FC")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Previously the FC tab rendered a body with nothing in it.
    assert 'gc-fc-pitch-card' in html, "FC editor body missing"
    for pitch in ("Green", "Dry", "Hard", "Flat", "Dead"):
        assert f'data-pitch="{pitch}"' in html, f"{pitch} card missing"
    assert 'data-table="wicket_factors_start"' in html
    assert 'data-table="wicket_factors_end"' in html
    assert 'data-field="new_ball_swing_overs"' in html
    assert "First Class" in html
    # ...and it must not be mislabelled as List A any more.
    assert "Editing the <strong>First Class</strong>" in html


def test_fc_ground_conditions_round_trips_a_save(authenticated_client):
    payload = {
        "match_format": "FC",
        "pitch_profiles": {
            "Flat": {"scoring_matrix": {
                "Dot": 0.60, "Single": 0.24, "Double": 0.05, "Three": 0.01,
                "Four": 0.06, "Six": 0.005, "Wicket": 0.015, "Extras": 0.02},
                "run_factor": 1.0}
        },
    }
    resp = authenticated_client.post("/ground-conditions/save",
                                     data=json.dumps(payload),
                                     content_type="application/json")
    assert resp.status_code == 200, resp.get_data(as_text=True)

    # It must come back on the FC page, and must NOT have leaked into T20.
    page = authenticated_client.get("/ground-conditions?format=FC").get_data(as_text=True)
    assert "0.6" in page
    # The FC editor markup must stay gated to the FC tab. (The buildFCConfig
    # JS is shared across formats, so assert on markup, not the class name.)
    t20 = authenticated_client.get("/ground-conditions?format=T20").get_data(as_text=True)
    assert 'data-table="wicket_factors_start"' not in t20
    assert "Editing the <strong>T20</strong>" in t20
