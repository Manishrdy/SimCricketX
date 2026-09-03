"""Deterministic, day-aware weather for First-Class cricket.

Version 2 scripts model *time* rather than revising an innings allocation.
They contain a public forecast (safe for captaincy logic) and a hidden actual
timeline (used by :class:`engine.match.Match`). Version 1 ``day_events``
scripts remain supported so an in-progress match is never re-rolled.

The clock is minutes after an 11:00 start. Lunch is 13:00-13:40, tea is
15:40-16:00 and scheduled close is 18:00. These are generic Test-style
defaults, not one competition's exact playing conditions.
"""

from __future__ import annotations

import random
from math import floor
from typing import Iterable, Optional


SCRIPT_VERSION = 2
DEFAULT_FORECAST = "clear"

DAY_START_MINUTE = 0
LUNCH_START_MINUTE = 120
LUNCH_END_MINUTE = 160
TEA_START_MINUTE = 280
TEA_END_MINUTE = 300
SCHEDULED_CLOSE_MINUTE = 420
PLAYING_MINUTES_PER_DAY = 360
MINUTES_PER_OVER = 4
MAX_SAME_DAY_EXTENSION_MINUTES = 60
MAX_CARRY_FORWARD_MINUTES = 30

INTERVALS = (
    (LUNCH_START_MINUTE, LUNCH_END_MINUTE),
    (TEA_START_MINUTE, TEA_END_MINUTE),
)

STATES = ("dry", "showers", "rain", "storm")
STATE_LABELS = {
    "dry": "Dry",
    "showers": "Showers possible",
    "rain": "Rain likely",
    "storm": "Heavy rain likely",
}
STATE_RAIN_CHANCE = {"dry": 0, "showers": 35, "rain": 65, "storm": 90}
EXPECTED_LOST_MINUTES = {"dry": 0, "showers": 25, "rain": 90, "storm": 220}
CLOUD_BASE = {"dry": 0.10, "showers": 0.45, "rain": 0.70, "storm": 0.95}

# Public forecast-state probabilities. Clear is deliberately a hard no-event
# mode because that is what the setup page promises.
FORECAST_TIERS = {
    "clear": {
        "label": "Clear",
        "weights": (1.00, 0.00, 0.00, 0.00),
        # Legacy fields retained for callers that inspected the old map.
        "interruption_chance": 0.0,
        "max_loss_fraction": 0.0,
    },
    "passing_showers": {
        "label": "Passing Showers",
        "weights": (0.60, 0.35, 0.05, 0.00),
        "interruption_chance": 0.20,
        "max_loss_fraction": 0.35,
    },
    "rain_around": {
        "label": "Rain Around",
        "weights": (0.15, 0.35, 0.45, 0.05),
        "interruption_chance": 0.40,
        "max_loss_fraction": 0.55,
    },
    "storm_warning": {
        "label": "Storm Warning",
        "weights": (0.05, 0.15, 0.35, 0.45),
        "interruption_chance": 0.60,
        "max_loss_fraction": 0.80,
    },
}


def forecast_label(forecast: str) -> str:
    return FORECAST_TIERS.get(forecast, FORECAST_TIERS[DEFAULT_FORECAST])["label"]


def _weighted_choice(rng: random.Random, values, weights):
    needle = rng.random()
    total = 0.0
    for value, weight in zip(values, weights):
        total += weight
        if needle <= total:
            return value
    return values[-1]


def _next_public_state(previous: str, weights, rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.60:
        return previous
    if roll < 0.90:
        return _weighted_choice(rng, STATES, weights)
    return STATES[max(0, STATES.index(previous) - 1)]


def _actual_state(public: str, forecast: str, rng: random.Random) -> str:
    if forecast == "clear":
        return "dry"
    idx = STATES.index(public)
    roll = rng.random()
    if roll < 0.70:
        return public
    distance = 1 if roll < 0.95 else 2
    candidates = [
        candidate for candidate in range(len(STATES))
        if abs(candidate - idx) == distance
    ]
    if candidates:
        return STATES[rng.choice(candidates)]
    # There is no two-level state on the requested side only when a future
    # state scale has fewer levels than this one. Keep this total without
    # accidentally turning a specified mismatch into a forecast match.
    fallback = [candidate for candidate in range(len(STATES)) if candidate != idx]
    if fallback:
        return STATES[max(fallback, key=lambda candidate: abs(candidate - idx))]
    return public


def _event_start(rng: random.Random) -> int:
    """Choose a wall-clock start, including a realistic delayed-start band."""
    roll = rng.random()
    if roll < 0.15:
        return 0
    if roll < 0.40:
        return rng.randint(10, 115)
    if roll < 0.75:
        return rng.randint(160, 275)
    return rng.randint(300, 405)


def _merge_rain_events(events: list[dict]) -> list[dict]:
    if not events:
        return []
    events = sorted(events, key=lambda event: event["start_minute"])
    merged: list[dict] = []
    for event in events:
        rain_end = event["start_minute"] + event["rain_minutes"]
        end = rain_end + event["recovery_minutes"]
        if not merged or event["start_minute"] >= merged[-1]["end_minute"] + 20:
            copy = dict(event)
            copy["rain_end_minute"] = rain_end
            copy["end_minute"] = end
            merged.append(copy)
            continue
        prior = merged[-1]
        prior["rain_end_minute"] = max(prior["rain_end_minute"], rain_end)
        prior["end_minute"] = max(prior["end_minute"], end)
        prior["rain_minutes"] = prior["rain_end_minute"] - prior["start_minute"]
        prior["recovery_minutes"] = prior["end_minute"] - prior["rain_end_minute"]
    return merged


def _rain_events_for_state(state: str, day: int, rng: random.Random) -> tuple[list[dict], bool]:
    if state == "dry":
        return [], False
    if state == "storm" and rng.random() < 0.12:
        return [{
            "id": f"d{day}-e1",
            "kind": "rain",
            "start_minute": 0,
            "rain_minutes": SCHEDULED_CLOSE_MINUTE,
            "recovery_minutes": 0,
            "rain_end_minute": SCHEDULED_CLOSE_MINUTE,
            "end_minute": SCHEDULED_CLOSE_MINUTE,
            "washout": True,
        }], True

    if state == "showers":
        count = rng.choice((1, 1, 2))
        rain_range, recovery_range = (8, 25), (5, 15)
    elif state == "rain":
        count = rng.choice((1, 2, 2, 3))
        rain_range, recovery_range = (20, 60), (15, 35)
    else:
        count = rng.choice((1, 1, 2))
        rain_range, recovery_range = (90, 240), (30, 75)

    raw = []
    for index in range(count):
        start = _event_start(rng)
        rain_minutes = rng.randint(*rain_range)
        recovery_minutes = rng.randint(*recovery_range)
        raw.append({
            "id": f"d{day}-e{index + 1}",
            "kind": "rain",
            "start_minute": start,
            "rain_minutes": rain_minutes,
            "recovery_minutes": recovery_minutes,
            "washout": False,
        })

    merged = _merge_rain_events(raw)
    for index, event in enumerate(merged, 1):
        event["id"] = f"d{day}-e{index}"
    return merged, False


def bad_light_start_minute(cloud_index: float, is_day_night: bool = False) -> Optional[int]:
    """Return the late-session suspension minute, or ``None`` if light holds."""
    bonus = 0.25 if is_day_night else 0.0
    crossing = (0.70 - 0.45 * cloud_index + bonus) / 0.55
    if crossing < 0:
        crossing = 0
    if crossing > 1:
        return None
    return int(round(TEA_END_MINUTE + crossing * 120))


def light_quality(minute: float, cloud_index: float, is_day_night: bool = False) -> float:
    progress = 0.0 if minute <= TEA_END_MINUTE else min(
        1.0, (minute - TEA_END_MINUTE) / 120.0)
    bonus = 0.25 if is_day_night else 0.0
    return 1.0 - 0.55 * progress - 0.45 * cloud_index + bonus


def _add_bad_light_event(events: list[dict], cloud: float, day: int,
                         is_day_night: bool) -> list[dict]:
    start = bad_light_start_minute(cloud, is_day_night=is_day_night)
    if start is None or start >= SCHEDULED_CLOSE_MINUTE:
        return events
    # Rain remains the public reason when it already owns the rest of the day.
    if any(event["kind"] == "rain" and event["start_minute"] <= start
           and event["end_minute"] >= SCHEDULED_CLOSE_MINUTE for event in events):
        return events
    result = list(events)
    result.append({
        "id": f"d{day}-light",
        "kind": "bad_light",
        "start_minute": start,
        "end_minute": SCHEDULED_CLOSE_MINUTE,
        "rain_minutes": 0,
        "recovery_minutes": 0,
        "washout": False,
    })
    return sorted(result, key=lambda event: (event["start_minute"], event["kind"] != "rain"))


def generate_weather_script(forecast: str, days: int, overs_per_day: int,
                            min_overs_last_hour: int = 15,
                            rng: Optional[random.Random] = None,
                            is_day_night: bool = False) -> dict:
    """Generate an immutable version-2 public forecast and hidden timeline."""
    del min_overs_last_hour  # a final-day rule, never a shortened-day floor
    rng = rng or random.Random()
    forecast = forecast if forecast in FORECAST_TIERS else DEFAULT_FORECAST
    weights = FORECAST_TIERS[forecast]["weights"]
    script = {
        "v": SCRIPT_VERSION,
        "forecast": forecast,
        "is_day_night": bool(is_day_night),
        "overs_per_day": int(overs_per_day),
        "days": {},
    }

    public_state = _weighted_choice(rng, STATES, weights)
    for day in range(1, int(days) + 1):
        if day > 1:
            public_state = _next_public_state(public_state, weights, rng)
        actual_state = _actual_state(public_state, forecast, rng)
        cloud = max(0.0, min(1.0, CLOUD_BASE[actual_state] + rng.uniform(-0.08, 0.08)))
        events, washout = _rain_events_for_state(actual_state, day, rng)
        if not washout:
            events = _add_bad_light_event(events, cloud, day, bool(is_day_night))
        script["days"][str(day)] = {
            "public_state": public_state,
            "public_label": STATE_LABELS[public_state],
            "rain_chance": STATE_RAIN_CHANCE[public_state],
            "late_light_risk": public_state in ("rain", "storm"),
            "actual_state": actual_state,
            "cloud_index": round(cloud, 3),
            "washout": washout,
            "events": events,
        }
    return script


def is_v2_script(weather_script: Optional[dict]) -> bool:
    return isinstance(weather_script, dict) and weather_script.get("v") == SCRIPT_VERSION


def get_day(weather_script: Optional[dict], day_number: int) -> Optional[dict]:
    if not is_v2_script(weather_script):
        return None
    days = weather_script.get("days") or {}
    return days.get(str(day_number)) or days.get(day_number)


def public_day_forecast(weather_script: Optional[dict], day_number: int) -> dict:
    day = get_day(weather_script, day_number)
    if not day:
        return {"state": "dry", "label": "Dry", "rain_chance": 0,
                "late_light_risk": False}
    return {
        "state": day.get("public_state", "dry"),
        "label": day.get("public_label", STATE_LABELS["dry"]),
        "rain_chance": int(day.get("rain_chance", 0)),
        "late_light_risk": bool(day.get("late_light_risk", False)),
    }


def day_events(weather_script: Optional[dict], day_number: int) -> list[dict]:
    day = get_day(weather_script, day_number)
    return list((day or {}).get("events") or [])


def interval_overlap_minutes(start: float, end: float) -> float:
    if end <= start:
        return 0.0
    return sum(max(0.0, min(end, hi) - max(start, lo)) for lo, hi in INTERVALS)


def playable_minutes_lost(start: float, end: float) -> float:
    return max(0.0, end - start - interval_overlap_minutes(start, end))


def _union_ranges(ranges: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def day_timing_summary(weather_script: Optional[dict], day_number: int,
                       overs_per_day: int = 90) -> dict:
    """Resolve gross delay, recoverable time and net loss for one day."""
    if not is_v2_script(weather_script):
        event = get_day_event(weather_script, day_number)
        lost_overs = min(overs_per_day, int((event or {}).get("overs_lost", 0)))
        return {
            "gross_delay_minutes": lost_overs * MINUTES_PER_OVER,
            "makeup_minutes": 0,
            "net_lost_minutes": lost_overs * MINUTES_PER_OVER,
            "overs_lost": lost_overs,
            "revised_close_minute": SCHEDULED_CLOSE_MINUTE,
        }

    events = day_events(weather_script, day_number)
    ranges = _union_ranges(
        (max(DAY_START_MINUTE, event["start_minute"]),
         min(SCHEDULED_CLOSE_MINUTE, event["end_minute"]))
        for event in events
    )
    gross = sum(playable_minutes_lost(start, end) for start, end in ranges)
    close_events = [
        event for event in events
        if event.get("end_minute", 0) >= SCHEDULED_CLOSE_MINUTE
    ]
    hard_stop = any(
        event.get("washout") or event.get("kind") == "bad_light"
        for event in close_events
    )
    resume_after_close = max(
        [SCHEDULED_CLOSE_MINUTE]
        + [event.get("end_minute", SCHEDULED_CLOSE_MINUTE) for event in close_events]
    )
    extension_available = max(
        0, SCHEDULED_CLOSE_MINUTE + MAX_SAME_DAY_EXTENSION_MINUTES - resume_after_close
    )
    makeup = 0 if hard_stop else min(
        MAX_SAME_DAY_EXTENSION_MINUTES, gross,
        extension_available if close_events else MAX_SAME_DAY_EXTENSION_MINUTES,
    )
    net = max(0, int(round(gross - makeup)))
    overs_lost = min(overs_per_day, floor(net / MINUTES_PER_OVER))
    return {
        "gross_delay_minutes": int(round(gross)),
        "makeup_minutes": int(round(makeup)),
        "net_lost_minutes": net,
        "overs_lost": overs_lost,
        "revised_close_minute": (
            max(SCHEDULED_CLOSE_MINUTE, resume_after_close) + int(round(makeup))
            if makeup else SCHEDULED_CLOSE_MINUTE
        ),
    }


# Version-1 compatibility helpers.
def get_day_event(weather_script: Optional[dict], day_number: int) -> Optional[dict]:
    events = (weather_script or {}).get("day_events") or {}
    return events.get(day_number) or events.get(str(day_number))


def effective_overs_today(weather_script: Optional[dict], day_number: int,
                          overs_per_day: int) -> int:
    if is_v2_script(weather_script):
        return max(0, overs_per_day - day_timing_summary(
            weather_script, day_number, overs_per_day)["overs_lost"])
    event = get_day_event(weather_script, day_number)
    if not event:
        return overs_per_day
    return max(0, overs_per_day - int(event.get("overs_lost", 0)))


def remaining_rain_risk(weather_script: Optional[dict], from_day: int,
                        total_days: int, overs_per_day: int = 90) -> float:
    """Public expected risk only; hidden actual events are never consulted."""
    if not weather_script or from_day > total_days:
        return 0.0
    if is_v2_script(weather_script):
        expected = 0
        for day_number in range(from_day, total_days + 1):
            state = public_day_forecast(weather_script, day_number)["state"]
            expected += EXPECTED_LOST_MINUTES.get(state, 0)
        scheduled = (total_days - from_day + 1) * PLAYING_MINUTES_PER_DAY
        return min(1.0, expected / scheduled) if scheduled else 0.0

    scheduled = lost = 0
    for day_number in range(from_day, total_days + 1):
        scheduled += overs_per_day
        event = get_day_event(weather_script, day_number)
        if event:
            lost += min(overs_per_day, int(event.get("overs_lost", 0)))
    return min(1.0, lost / scheduled) if scheduled else 0.0


def minute_label(minute: float, start_hour: int = 11) -> str:
    absolute = start_hour * 60 + int(round(minute))
    hours, minutes = divmod(absolute, 60)
    return f"{hours % 24:02d}:{minutes:02d}"


def day_summary_line(weather_script: Optional[dict], day_number: int) -> Optional[str]:
    if is_v2_script(weather_script):
        summary = day_timing_summary(
            weather_script, day_number,
            int((weather_script or {}).get("overs_per_day", 90)),
        )
        if not day_events(weather_script, day_number):
            return None
        day = get_day(weather_script, day_number) or {}
        if day.get("washout"):
            return "Play was washed out for the day."
        causes = {event.get("kind") for event in day_events(weather_script, day_number)}
        reason = "Rain and bad light" if len(causes) > 1 else (
            "Bad light" if "bad_light" in causes else "Rain"
        )
        return (
            f"{reason} caused {summary['gross_delay_minutes']} minutes of delay; "
            f"{summary['makeup_minutes']} were recovered and "
            f"{summary['overs_lost']} over(s) were lost."
        )

    event = get_day_event(weather_script, day_number)
    if not event:
        return None
    if event.get("reason") == "washed_out":
        return "Play was washed out for the day."
    reason_label = "Bad light" if event.get("reason") == "bad_light" else "Rain"
    return f"{reason_label} cost {event.get('overs_lost', 0)} over(s) today."
