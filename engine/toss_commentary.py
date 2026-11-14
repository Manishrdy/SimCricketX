"""Broadcast-style toss segment: pitch report, the toss, then both captains.

Output is a short script, one paragraph per speaker, with a break between:

    [Pitch Report]: <paragraph about the pitch, plus one stadium note>
    <break>
    It's toss time, the home captain X has the coin and Y calls Tails.
    It's Heads, not Tails, so AUS have won the toss.
    AUS have chosen to bowl first.
    <break>
    Winning captain (AUS): <speech>
    <break>
    Other captain (IND): <speech>
    <break>

All wording lives in data/toss_commentary.json so it can be extended without
touching code: pitch reports are keyed by pitch, the winner's speech by the
decision and the pitch group (or "against" when the choice defies the
conditions), the other captain's by the winner's decision.

Only data the setup page already shows the user is read here (pitch, ground
dimensions, outfield speed, temperature, wear forecast, day/night). The hidden
weather tier and RNG state are never used -- see public_match_data().

The RNG is local and seeded from the match, so a refresh renders the same
broadcast and nothing here can disturb the engine's or CommentaryEngine's RNG.
Every name that reaches the log is HTML-escaped: appendLog() uses innerHTML.
"""

import json
import os
import random as _random

from markupsafe import escape

from engine.toss import correct_decision

_PACK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "toss_commentary.json")
_PACK = None


def _pack():
    global _PACK
    if _PACK is None:
        with open(_PACK_PATH, "r", encoding="utf-8") as f:
            _PACK = json.load(f)
    return _PACK


def _venue_notes(snapshot, is_day_night, pitch, fmt):
    """Candidate one-liners about the venue, strongest cues first."""
    notes = []
    snapshot = snapshot or {}
    coef = snapshot.get("coefficients") or {}
    six = coef.get("Six")
    if six is not None and six >= 1.06:
        notes.append("The boundaries are on the short side here, so the big hitters will fancy their chances.")
    elif six is not None and six <= 0.93:
        notes.append("It's a big ground. Clearing the ropes is hard work, and there are twos and threes out there for the running.")
    speed = snapshot.get("outfield_speed")
    if speed is not None and speed >= 1.05:
        notes.append("The outfield is lightning quick, so anything along the ground will race away.")
    elif speed is not None and speed <= 0.95:
        notes.append("The outfield is lush and a touch slow, so fours will have to be earned.")
    temp = snapshot.get("average_temperature_c")
    if temp is not None and temp >= 32:
        notes.append(f"It's a hot one at around {round(temp)} degrees. The heat will test the fielders as the day goes on.")
    elif temp is not None and temp <= 16:
        notes.append(f"A chilly {round(temp)} degrees out there, and the ball will be hard to grip for the bowlers.")
    wear = (snapshot.get("public_forecast") or {}).get("wear_rate_multiplier", 1)
    if wear > 1.1 and pitch in ("Dry", "Hard"):
        notes.append("This surface is expected to wear quickly, so it could be a very different pitch by the end of the game.")
    if is_day_night and fmt != "FC":
        notes.append("This one runs under the lights, and there's talk of dew arriving later, which can make life hard for the side bowling second.")
    elif is_day_night:
        notes.append("It's a day-night game, so the pink ball and the twilight session could change the picture.")
    return notes


def _fill(template, **values):
    return template.format(**{k: str(escape(v)) for k, v in values.items()})


def build_toss_commentary(match_data, *, toss_call, toss_coin, winner, decision,
                          caller, home_captain, away_captain, fmt=None, rng=None):
    """Return the broadcast as a list of {"kind": "line"|"break", "text"}.

    `decision` is the winner's actual Bat/Bowl choice (the user's in manual
    mode), graded against correct_decision() so the speech is honest about
    whether the call follows the conditions.
    """
    pack = _pack()
    home = str(match_data["team_home"]).split("_")[0]
    away = str(match_data["team_away"]).split("_")[0]
    pitch = match_data.get("pitch")
    snapshot = match_data.get("conditions_snapshot") or {}
    is_dn = bool(match_data.get("is_day_night", False))
    fmt_name = str(match_data.get("match_format", "T20"))
    rng = rng or _random.Random(f"{match_data.get('match_id', '')}:{toss_coin}:{decision}")
    favoured = (correct_decision(fmt, pitch, is_day_night=is_dn,
                                 venue_forecast=snapshot.get("public_forecast"))
                if fmt else decision)
    stadium = match_data.get("stadium") or snapshot.get("name") or "the ground"

    win_team, lose_team = (home, away) if winner == home else (away, home)
    win_cap, lose_cap = (home_captain, away_captain) if winner == home else (away_captain, home_captain)

    lines = []

    def line(text):
        lines.append({"kind": "line", "text": str(text)})

    def brk():
        lines.append({"kind": "break", "text": ""})

    # 1. Pitch report: one paragraph, plus one stadium note blended in.
    report = rng.choice(pack["pitch_report"].get(pitch) or pack["fallback_pitch_report"])
    paragraph = _fill(report, stadium=stadium)
    notes = _venue_notes(snapshot, is_dn, pitch, fmt_name)
    if notes:
        paragraph += " " + rng.choice(notes)
    line(f"[Pitch Report]: {paragraph}")
    brk()

    # 2. The toss.
    line(f"It's toss time, the home captain {escape(home_captain)} has the coin and "
         f"{escape(caller)} calls {escape(toss_call)}.")
    if toss_call == toss_coin:
        line(f"It's {escape(toss_coin)}! {escape(winner)} have won the toss.")
    else:
        line(f"It's {escape(toss_coin)}, not {escape(toss_call)}, so {escape(winner)} have won the toss.")
    line(f"{escape(winner)} have chosen to {'bat' if decision == 'Bat' else 'bowl'} first.")
    brk()

    # 3. Captains.
    group = pack["pitch_groups"].get(pitch, "batting")
    key = f"{decision}_{group}" if decision == favoured else f"{decision}_against"
    speech = rng.choice(pack["winner_speech"][key])
    line(f"{escape(win_cap)} ({escape(win_team)}): {_fill(speech, team=win_team, opp=lose_team, captain=win_cap)}")
    brk()
    reply = rng.choice(pack["loser_speech"][decision])
    line(f"{escape(lose_cap)} ({escape(lose_team)}): {_fill(reply, team=lose_team, opp=win_team, captain=lose_cap)}")
    brk()
    return lines
