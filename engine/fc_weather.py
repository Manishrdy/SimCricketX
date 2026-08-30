"""
engine/fc_weather.py
=====================

Day-aware weather/interruption model for First-Class (FC) matches.

Unlike engine/weather.py (rain events pinned to a "global over" spanning
both innings back-to-back — hardcoded to a 2-innings match, with a
MIN_OVERS_FOR_RESULT table that has no FC entry), FC weather is naturally
day-scoped: a rain delay or bad-light stoppage reduces how many overs get
bowled TODAY, not a revision to a fixed-innings-length target. FC has no
such target for 3 of its 4 innings, and even the 4th-innings chase isn't a
fixed-overs concept — so there is no DLS-style resource revision here.
Lost overs are simply lost; the match continues tomorrow using whatever
days remain (or ends as a draw if none do — see Match._fc_pre_ball_checks).

The script is rolled once (at match creation) and stored in match_data so
a resumed match replays identically, mirroring engine/weather.py's own
determinism discipline (see Match.__init__'s weather_script handling).
"""

import random

# Forecast tiers reuse the naming convention of engine/weather.py's
# FORECAST_TIERS but with FC-appropriate probabilities/severities — a full
# match day lost to weather is a much bigger deal across a 4-5 day match
# than a T20's forecast tiers imply.
FORECAST_TIERS = {
    "clear":            {"label": "Clear",           "interruption_chance": 0.05, "max_loss_fraction": 0.15},
    "passing_showers":  {"label": "Passing Showers",  "interruption_chance": 0.20, "max_loss_fraction": 0.35},
    "rain_around":      {"label": "Rain Around",      "interruption_chance": 0.40, "max_loss_fraction": 0.55},
    "storm_warning":    {"label": "Storm Warning",    "interruption_chance": 0.60, "max_loss_fraction": 0.80},
}
DEFAULT_FORECAST = "clear"

# Full-washout threshold: a rolled loss fraction at or above this wipes out
# the entire scheduled day rather than a partial reduction.
WASHOUT_THRESHOLD = 0.95


def forecast_label(forecast):
    return FORECAST_TIERS.get(forecast, FORECAST_TIERS[DEFAULT_FORECAST])["label"]


def generate_weather_script(forecast, days, overs_per_day, min_overs_last_hour=15, rng=None):
    """
    Roll one weather script for the whole match: for each day, either no
    event, a partial-overs loss (rain/bad light), or a full washout.

    Returns {"forecast": forecast, "day_events": {day_number: {...}}} —
    day_events only holds entries for days that actually lost overs; a day
    with no entry plays its full overs_per_day.

    min_overs_last_hour enforces the real-world "minimum 15 overs in the
    last hour" rule: a partial interruption (not a full washout) can never
    cut the day's overs below this floor, so bad light alone can't be used
    to shorten a day into a token session.
    """
    rng = rng or random.Random()
    tier = FORECAST_TIERS.get(forecast, FORECAST_TIERS[DEFAULT_FORECAST])
    day_events = {}
    for day in range(1, days + 1):
        if rng.random() >= tier["interruption_chance"]:
            continue
        loss_fraction = rng.uniform(0.1, tier["max_loss_fraction"])
        if loss_fraction >= WASHOUT_THRESHOLD:
            day_events[day] = {"reason": "washed_out", "overs_lost": overs_per_day}
            continue
        overs_lost = int(round(overs_per_day * loss_fraction))
        max_loss = max(0, overs_per_day - min_overs_last_hour)
        overs_lost = min(overs_lost, max_loss)
        if overs_lost <= 0:
            continue
        reason = "bad_light" if rng.random() < 0.5 else "rain"
        day_events[day] = {"reason": reason, "overs_lost": overs_lost}
    return {"forecast": forecast, "day_events": day_events}


def get_day_event(weather_script, day_number):
    """Return the event dict for *day_number*, or None. Tolerates string
    keys — weather_script may have round-tripped through JSON (match_data
    persistence), which turns int dict keys into strings."""
    events = (weather_script or {}).get("day_events") or {}
    return events.get(day_number) or events.get(str(day_number))


def effective_overs_today(weather_script, day_number, overs_per_day):
    """Return the actual overs schedulable today after any weather loss."""
    event = get_day_event(weather_script, day_number)
    if not event:
        return overs_per_day
    return max(0, overs_per_day - event.get("overs_lost", 0))


def remaining_rain_risk(weather_script, from_day, total_days, overs_per_day=90):
    """
    0-1 read on how much of the remaining match the pre-rolled weather is
    going to cost, as a fraction of the overs still scheduled.

    The script is rolled once for the whole match, so this is the captain's
    forecast: real captains do factor "rain about on the last two days" into
    whether to enforce the follow-on, because time lost is time they may not
    get back to bowl the opposition out twice.
    """
    if not weather_script or from_day > total_days:
        return 0.0
    scheduled = 0
    lost = 0
    for day in range(from_day, total_days + 1):
        scheduled += overs_per_day
        event = get_day_event(weather_script, day)
        if event:
            lost += min(overs_per_day, event.get("overs_lost", 0))
    if scheduled <= 0:
        return 0.0
    return min(1.0, lost / scheduled)


def day_summary_line(weather_script, day_number):
    """Human-readable one-liner for the day-break commentary, or None if
    the day was unaffected by weather."""
    event = get_day_event(weather_script, day_number)
    if not event:
        return None
    if event["reason"] == "washed_out":
        return "Play was washed out for the day."
    reason_label = "Bad light" if event["reason"] == "bad_light" else "Rain"
    return f"{reason_label} cost {event['overs_lost']} over(s) today."
