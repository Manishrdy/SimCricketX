"""Broadcast-style toss commentary: a short script with breaks between speakers."""
from engine.format_config import get_any_format
from engine.toss_commentary import _pack, build_toss_commentary


def _xi(prefix):
    return [{"name": f"{prefix}_P{i+1}", "role": "Bowler", "batting_rating": 50,
             "bowling_rating": 60, "will_bowl": True, "is_captain": i == 0} for i in range(11)]


def _data(**over):
    d = {"match_id": "m-1", "team_home": "HOM_1", "team_away": "AWY_1",
         "stadium": "Test Oval", "pitch": "Green", "match_format": "T20", "overs": 20,
         "is_day_night": False, "playing_xi": {"home": _xi("H"), "away": _xi("A")},
         "conditions_snapshot": {"coefficients": {"Six": 1.1}, "average_temperature_c": 35,
                                 "outfield_speed": 1.0, "public_forecast": {"wear_rate_multiplier": 1.0}}}
    d.update(over)
    return d


def _build(data=None, **kw):
    args = dict(toss_call="Heads", toss_coin="Heads", winner="AWY", decision="Bowl",
                caller="A_P1", home_captain="H_P1", away_captain="A_P1", fmt=get_any_format("T20"))
    args.update(kw)
    return build_toss_commentary(data or _data(), **args)


def _shape(lines):
    return "".join("L" if l["kind"] == "line" else "-" for l in lines)


def test_script_layout_matches_the_requested_format():
    lines = _build()
    # pitch report | 3 toss lines | winner speech | loser speech, each followed by a break
    assert _shape(lines) == "L-LLL-L-L-"
    assert lines[0]["text"].startswith("[Pitch Report]: ")
    assert lines[2]["text"] == "It's toss time, the home captain H_P1 has the coin and A_P1 calls Heads."
    assert lines[3]["text"] == "It's Heads! AWY have won the toss."
    assert lines[4]["text"] == "AWY have chosen to bowl first."
    assert lines[6]["text"].startswith("A_P1 (AWY): ")
    assert lines[8]["text"].startswith("H_P1 (HOM): ")


def test_wrong_call_wording():
    lines = _build(toss_call="Tails", toss_coin="Heads", winner="HOM", decision="Bat")
    assert lines[3]["text"] == "It's Heads, not Tails, so HOM have won the toss."
    assert lines[6]["text"].startswith("H_P1 (HOM): ")


def test_pitch_report_blends_stadium_conditions():
    para = _build()[0]["text"]
    assert "short side" in para or "hot one" in para
    assert "{" not in para


def test_deterministic_per_match_and_varied_across_matches():
    assert _build() == _build()
    variants = {_build(_data(match_id=f"m-{i}"))[0]["text"] for i in range(15)}
    assert len(variants) > 1


def test_every_pitch_and_decision_has_five_variants():
    p = _pack()
    for pitch in ("Flat", "Dead", "Hard", "Green", "Dry"):
        assert len(p["pitch_report"][pitch]) >= 5
    for key in ("Bat_batting", "Bat_seam", "Bat_spin", "Bowl_batting", "Bowl_seam",
                "Bowl_spin", "Bat_against", "Bowl_against"):
        assert len(p["winner_speech"][key]) >= 5, key
    for key in ("Bat", "Bowl"):
        assert len(p["loser_speech"][key]) >= 5


def test_speech_is_honest_when_the_choice_defies_the_conditions():
    p = _pack()
    # Green favours bowling, so a Bat decision must use the "against" pool.
    speech = _build(decision="Bat")[6]["text"].split(": ", 1)[1]
    assert speech in p["winner_speech"]["Bat_against"]
    speech = _build(decision="Bowl")[6]["text"].split(": ", 1)[1]
    assert speech in p["winner_speech"]["Bowl_seam"]


def test_names_are_html_escaped_and_unknown_pitch_falls_back():
    d = _data(stadium="<img src=x>", pitch="Mystery")
    d.pop("conditions_snapshot")
    text = " ".join(l["text"] for l in _build(d, home_captain="<script>"))
    assert "<img" not in text and "<script>" not in text
