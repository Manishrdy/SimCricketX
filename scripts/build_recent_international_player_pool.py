#!/usr/bin/env python3
"""Build import-ready player-pool JSON from recent Cricsheet internationals.

The database is opened with SQLite's immutable read-only URI. Match membership
and ball-by-ball statistics come from Cricsheet JSON archives, while player
display names, roles, and styles are enriched from ESPN's public cricket athlete
records using the Cricinfo IDs in the Cricsheet Register.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path


IMPORT_FIELDS = [
    "name", "role", "batting_rating", "bowling_rating", "fielding_rating",
    "list_a_batting_rating", "list_a_bowling_rating", "list_a_fielding_rating",
    "fc_batting_rating", "fc_bowling_rating", "fc_fielding_rating",
    "fc_technique_rating", "fc_temperament_rating", "fc_stamina_rating",
    "batting_hand", "bowling_type", "bowling_hand", "is_captain",
    "is_wicketkeeper",
]
FORMATS = {"IT20": "t20", "T20": "t20", "T20I": "t20", "ODI": "odi", "Test": "test"}
BOWLER_WICKETS = {
    "bowled", "caught", "caught and bowled", "lbw", "stumped", "hit wicket",
}


def clamp(value: float, low: int = 0, high: int = 100) -> int:
    return int(round(max(low, min(high, value))))


def load_people(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {row["identifier"]: row for row in csv.DictReader(handle)}


def read_existing(db_path: Path) -> dict[str, dict]:
    fields = ",".join(IMPORT_FIELDS)
    uri = f"file:{db_path.resolve()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(f"SELECT {fields} FROM master_players").fetchall()
    return {row["name"].casefold(): dict(row) for row in rows}


def empty_stats() -> dict:
    return {
        "matches": set(), "runs": 0, "balls": 0, "outs": 0,
        "bowl_runs": 0, "bowl_balls": 0, "wickets": 0,
        "catches": 0, "stumpings": 0, "runouts": 0,
        "innings": 0, "innings_runs": [], "teams": set(),
    }


def collect(match_dirs: list[Path], start: date, end: date):
    stats = defaultdict(lambda: defaultdict(empty_stats))
    identities: dict[str, set[str]] = defaultdict(set)
    match_counts = defaultdict(int)

    for directory in match_dirs:
        for path in sorted(directory.glob("*.json")):
            try:
                match = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            info = match.get("info", {})
            dates = info.get("dates") or []
            if not dates:
                continue
            match_date = date.fromisoformat(str(dates[0])[:10])
            fmt = FORMATS.get(info.get("match_type"))
            if not fmt or not (start <= match_date <= end):
                continue
            match_id = path.stem
            match_counts[fmt] += 1
            registry = info.get("registry", {}).get("people", {})
            name_to_id = {name: identifier for name, identifier in registry.items()}
            for team, names in info.get("players", {}).items():
                for name in names:
                    identifier = name_to_id.get(name)
                    if not identifier:
                        continue
                    identities[identifier].add(name)
                    bucket = stats[identifier][fmt]
                    bucket["matches"].add(match_id)
                    bucket["teams"].add(team)

            for innings in match.get("innings", []):
                innings_scores = defaultdict(int)
                appeared = set()
                for over in innings.get("overs", []):
                    for delivery in over.get("deliveries", []):
                        batter_name = delivery.get("batter")
                        bowler_name = delivery.get("bowler")
                        batter = name_to_id.get(batter_name)
                        bowler = name_to_id.get(bowler_name)
                        runs = delivery.get("runs", {})
                        extras = delivery.get("extras", {})
                        if batter:
                            identities[batter].add(batter_name)
                            appeared.add(batter)
                            made = int(runs.get("batter", 0))
                            stats[batter][fmt]["runs"] += made
                            innings_scores[batter] += made
                            if "wides" not in extras:
                                stats[batter][fmt]["balls"] += 1
                        if bowler:
                            identities[bowler].add(bowler_name)
                            legal = "wides" not in extras and "noballs" not in extras
                            if legal:
                                stats[bowler][fmt]["bowl_balls"] += 1
                            stats[bowler][fmt]["bowl_runs"] += int(runs.get("batter", 0))
                            stats[bowler][fmt]["bowl_runs"] += int(extras.get("wides", 0))
                            stats[bowler][fmt]["bowl_runs"] += int(extras.get("noballs", 0))
                        for wicket in delivery.get("wickets", []):
                            out = name_to_id.get(wicket.get("player_out"))
                            kind = wicket.get("kind", "")
                            if out and kind not in {"retired hurt", "retired not out", "obstructing the field"}:
                                stats[out][fmt]["outs"] += 1
                            if bowler and kind in BOWLER_WICKETS:
                                stats[bowler][fmt]["wickets"] += 1
                            for fielder in wicket.get("fielders", []):
                                fielder_name = fielder.get("name") if isinstance(fielder, dict) else fielder
                                fielder_id = name_to_id.get(fielder_name)
                                if not fielder_id:
                                    continue
                                if kind == "stumped":
                                    stats[fielder_id][fmt]["stumpings"] += 1
                                elif kind in {"run out", "retired out"}:
                                    stats[fielder_id][fmt]["runouts"] += 1
                                elif kind in {"caught", "caught and bowled"}:
                                    stats[fielder_id][fmt]["catches"] += 1
                for identifier in appeared:
                    stats[identifier][fmt]["innings"] += 1
                    stats[identifier][fmt]["innings_runs"].append(innings_scores[identifier])
    return stats, identities, match_counts


def fetch_metadata(identifier: str, people: dict, cache_dir: Path) -> dict:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{identifier}.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    person = people.get(identifier, {})
    cricinfo_id = person.get("key_cricinfo", "")
    if not cricinfo_id:
        return {}
    url = f"http://core.espnuk.org/v2/sports/cricket/athletes/{cricinfo_id}"
    try:
        with urllib.request.urlopen(url, timeout=12) as response:
            data = json.load(response)
        cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data
    except (OSError, ValueError, urllib.error.URLError):
        return {}


def batting_rating(bucket: dict, fmt: str) -> int:
    runs, balls, outs = bucket["runs"], bucket["balls"], bucket["outs"]
    if fmt == "t20":
        avg = (runs + 22) / (outs + 1)
        strike = 100 * (runs + 96) / max(1, balls + 80)
        raw = 50 + 0.90 * (avg - 22) + 0.18 * (strike - 120)
        confidence = min(1.0, math.sqrt(balls / 320))
        baseline = 44
    elif fmt == "odi":
        avg = (runs + 28) / (outs + 1)
        strike = 100 * (runs + 68) / max(1, balls + 80)
        raw = 50 + 0.85 * (avg - 28) + 0.20 * (strike - 85)
        confidence = min(1.0, math.sqrt(balls / 500))
        baseline = 44
    else:
        avg = (runs + 28) / (outs + 1)
        strike = 100 * (runs + 45) / max(1, balls + 90)
        raw = 50 + 0.95 * (avg - 28) + 0.10 * (strike - 50)
        confidence = min(1.0, math.sqrt(balls / 700))
        baseline = 44
    volume = min(6, math.log1p(runs) / math.log(10))
    return clamp(baseline * (1 - confidence) + raw * confidence + volume, 8, 96)


def bowling_rating(bucket: dict, fmt: str) -> int:
    balls, runs, wickets = bucket["bowl_balls"], bucket["bowl_runs"], bucket["wickets"]
    if balls == 0:
        return 8
    average = (runs + 28) / (wickets + 1)
    economy = 6 * (runs + 24) / (balls + 24)
    strike = (balls + 24) / (wickets + 1)
    if fmt == "t20":
        raw = 52 + 1.10 * (25 - average) + 3.0 * (7.8 - economy) + 0.42 * (22 - strike)
        target = 280
    elif fmt == "odi":
        raw = 52 + 1.05 * (30 - average) + 3.0 * (5.5 - economy) + 0.35 * (34 - strike)
        target = 450
    else:
        raw = 52 + 1.20 * (30 - average) + 0.25 * (55 - strike) + 1.5 * (3.3 - economy)
        target = 700
    confidence = min(1.0, math.sqrt(balls / target))
    volume = min(6, math.log1p(wickets) / math.log(4))
    return clamp(42 * (1 - confidence) + raw * confidence + volume, 8, 96)


def fielding_rating(bucket: dict, wicketkeeper: bool) -> int:
    matches = max(1, len(bucket["matches"]))
    impacts = bucket["catches"] + 1.4 * bucket["runouts"] + 1.5 * bucket["stumpings"]
    baseline = 72 if wicketkeeper else 65
    return clamp(baseline + min(16, 11 * impacts / matches), 50, 94)


def map_style(metadata: dict) -> tuple[str, str, str]:
    batting_hand = ""
    bowling_hand = ""
    bowling_type = ""
    for style in metadata.get("style") or []:
        description = str(style.get("description", "")).lower()
        style_type = style.get("type")
        if style_type == "batting":
            batting_hand = "Left" if "left" in description else "Right" if "right" in description else ""
        elif style_type == "bowling":
            bowling_hand = "Left" if "left" in description else "Right" if "right" in description else ""
            if "legbreak" in description or "leg break" in description:
                bowling_type = "Leg spin"
            elif "wrist" in description or "chinaman" in description:
                bowling_type = "Wrist spin"
            elif "offbreak" in description or "off break" in description:
                bowling_type = "Off spin"
            elif "orthodox" in description or "finger" in description:
                bowling_type = "Finger spin"
            elif "fast-medium" in description or "fast medium" in description:
                bowling_type = "Fast-medium"
            elif "medium-fast" in description or "medium fast" in description:
                bowling_type = "Medium-fast"
            elif "fast" in description:
                bowling_type = "Fast"
            elif "medium" in description:
                bowling_type = "Medium"
    return batting_hand, bowling_type, bowling_hand


def infer_role(metadata: dict, player_stats: dict) -> tuple[str, bool]:
    position = (metadata.get("position") or {}).get("name", "").lower()
    observed_keeper = any(b["stumpings"] for b in player_stats.values())
    wicketkeeper = "wicketkeeper" in position or "wicket-keeper" in position or observed_keeper
    if wicketkeeper:
        return "Wicketkeeper", True
    if "allround" in position or "all-round" in position:
        return "All-rounder", False
    if "bowl" in position:
        return "Bowler", False
    if "batt" in position or "opening" in position or "middle-order" in position:
        return "Batsman", False
    balls = sum(b["balls"] for b in player_stats.values())
    bowl_balls = sum(b["bowl_balls"] for b in player_stats.values())
    if bowl_balls >= 60 and balls >= 60:
        return "All-rounder", False
    if bowl_balls > balls * 1.5:
        return "Bowler", False
    return "Batsman", False


def fallback_bucket(player_stats: dict, fmt: str) -> dict:
    if fmt in player_stats and player_stats[fmt]["matches"]:
        return player_stats[fmt]
    available = [b for b in player_stats.values() if b["matches"]]
    if not available:
        return empty_stats()
    return max(available, key=lambda b: len(b["matches"]))


def build_player(identifier: str, player_stats: dict, identities: dict, people: dict,
                 metadata: dict, existing: dict[str, dict], name_override: str = "") -> tuple[dict, bool]:
    display_name = name_override or str(metadata.get("displayName") or "").strip()
    if not display_name:
        display_name = (people.get(identifier, {}).get("unique_name") or
                        people.get(identifier, {}).get("name") or
                        sorted(identities.get(identifier, {identifier}), key=len)[-1])
    matched = existing.get(display_name.casefold())
    if matched:
        matched["is_captain"] = bool(matched["is_captain"])
        matched["is_wicketkeeper"] = bool(matched["is_wicketkeeper"])
        return {field: matched[field] for field in IMPORT_FIELDS}, True

    role, wicketkeeper = infer_role(metadata, player_stats)
    batting_hand, bowling_type, bowling_hand = map_style(metadata)
    if wicketkeeper:
        bowling_type = ""
        bowling_hand = ""

    t20 = fallback_bucket(player_stats, "t20")
    odi = fallback_bucket(player_stats, "odi")
    test = fallback_bucket(player_stats, "test")
    t20_bat, odi_bat, test_bat = (batting_rating(t20, "t20"), batting_rating(odi, "odi"),
                                  batting_rating(test, "test"))
    t20_bowl, odi_bowl, test_bowl = (bowling_rating(t20, "t20"), bowling_rating(odi, "odi"),
                                     bowling_rating(test, "test"))
    if role == "Wicketkeeper":
        t20_bowl = odi_bowl = test_bowl = 0
    elif role == "Bowler":
        # The generic batting model shrinks sparse samples toward a batter's
        # baseline; specialist bowlers need a tail-ender baseline instead.
        t20_bat = clamp(0.68 * t20_bat, 8, 48)
        odi_bat = clamp(0.68 * odi_bat, 8, 48)
        test_bat = clamp(0.68 * test_bat, 8, 48)
    elif role == "Batsman" and sum(b["bowl_balls"] for b in player_stats.values()) < 12:
        t20_bowl = odi_bowl = test_bowl = min(t20_bowl, 15)

    balls_per_out = test["balls"] / max(1, test["outs"])
    technique = clamp(0.72 * test_bat + 0.28 * min(95, 30 + balls_per_out / 2), 10, 96)
    scores = test["innings_runs"]
    big_score_rate = sum(score >= 50 for score in scores) / max(1, len(scores))
    temperament = clamp(0.78 * test_bat + 22 * big_score_rate, 10, 96)
    bowl_load = test["bowl_balls"] / max(1, len(test["matches"]))
    stamina = clamp(48 + 0.45 * min(80, bowl_load) + 0.18 * (test_bowl - 50), 35, 96)
    if role in {"Batsman", "Wicketkeeper"} and test["bowl_balls"] == 0:
        stamina = 58

    player = {
        "name": display_name,
        "role": role,
        "batting_rating": t20_bat,
        "bowling_rating": t20_bowl,
        "fielding_rating": fielding_rating(t20, wicketkeeper),
        "list_a_batting_rating": odi_bat,
        "list_a_bowling_rating": odi_bowl,
        "list_a_fielding_rating": fielding_rating(odi, wicketkeeper),
        "fc_batting_rating": test_bat,
        "fc_bowling_rating": test_bowl,
        "fc_fielding_rating": fielding_rating(test, wicketkeeper),
        "fc_technique_rating": technique,
        "fc_temperament_rating": temperament,
        "fc_stamina_rating": stamina,
        "batting_hand": batting_hand,
        "bowling_type": bowling_type,
        "bowling_hand": bowling_hand,
        "is_captain": False,
        "is_wicketkeeper": wicketkeeper,
    }
    return player, False


def validate(players: list[dict]) -> list[str]:
    errors = []
    seen = set()
    allowed_roles = {"Batsman", "Bowler", "All-rounder", "Wicketkeeper"}
    allowed_hands = {"", "Left", "Right"}
    allowed_bowling = {"", "Fast", "Fast-medium", "Medium-fast", "Medium",
                       "Off spin", "Leg spin", "Finger spin", "Wrist spin"}
    rating_fields = IMPORT_FIELDS[2:14]
    for index, player in enumerate(players, 1):
        if list(player) != IMPORT_FIELDS:
            errors.append(f"row {index}: keys/order differ from importer schema")
        name_key = player.get("name", "").casefold()
        if not name_key or name_key in seen:
            errors.append(f"row {index}: blank or duplicate name {player.get('name')!r}")
        seen.add(name_key)
        if player.get("role") not in allowed_roles:
            errors.append(f"row {index}: invalid role")
        if player.get("batting_hand") not in allowed_hands or player.get("bowling_hand") not in allowed_hands:
            errors.append(f"row {index}: invalid hand")
        if player.get("bowling_type") not in allowed_bowling:
            errors.append(f"row {index}: invalid bowling type")
        if any(not isinstance(player.get(field), int) or not 0 <= player[field] <= 100 for field in rating_fields):
            errors.append(f"row {index}: invalid rating")
        if not isinstance(player.get("is_captain"), bool) or not isinstance(player.get("is_wicketkeeper"), bool):
            errors.append(f"row {index}: invalid boolean")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--people", type=Path, required=True)
    parser.add_argument("--matches", type=Path, action="append", required=True)
    parser.add_argument("--metadata-cache", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2023, 8, 29))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 8, 29))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    people = load_people(args.people)
    existing = read_existing(args.db)
    stats, identities, match_counts = collect(args.matches, args.start, args.end)
    metadata_by_id = {}
    identifiers = sorted(stats)
    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = {
            executor.submit(fetch_metadata, identifier, people, args.metadata_cache): identifier
            for identifier in identifiers
        }
        for number, future in enumerate(as_completed(futures), 1):
            metadata_by_id[futures[future]] = future.result()
            if number % 250 == 0:
                print(f"enriched metadata {number}/{len(identifiers)}", file=sys.stderr)

    base_names = {}
    grouped_names = defaultdict(list)
    for identifier in identifiers:
        metadata = metadata_by_id[identifier]
        name = str(metadata.get("displayName") or "").strip()
        if not name:
            name = (people.get(identifier, {}).get("unique_name") or
                    people.get(identifier, {}).get("name") or
                    sorted(identities.get(identifier, {identifier}), key=len)[-1])
        base_names[identifier] = name
        grouped_names[name.casefold()].append(identifier)

    resolved_names = dict(base_names)
    for duplicate_ids in grouped_names.values():
        if len(duplicate_ids) < 2:
            continue
        primary = max(
            duplicate_ids,
            key=lambda ident: sum(len(bucket["matches"]) for bucket in stats[ident].values()),
        )
        used = {base_names[primary].casefold()}
        for identifier in duplicate_ids:
            if identifier == primary:
                continue
            teams = sorted({team for bucket in stats[identifier].values() for team in bucket["teams"]})
            suffix = teams[0] if teams else identifier
            candidate = f"{base_names[identifier]} ({suffix})"
            if candidate.casefold() in used:
                candidate = f"{candidate} [{identifier}]"
            used.add(candidate.casefold())
            resolved_names[identifier] = candidate

    players = []
    matched_count = 0
    for identifier in identifiers:
        metadata = metadata_by_id[identifier]
        player, matched = build_player(
            identifier, stats[identifier], identities, people, metadata, existing,
            name_override=resolved_names[identifier],
        )
        players.append(player)
        matched_count += int(matched)
    players.sort(key=lambda player: player["name"].casefold())
    errors = validate(players)
    if errors:
        print("\n".join(errors[:30]), file=sys.stderr)
        return 1
    args.output.write_text(json.dumps(players, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "players": len(players), "existing_profiles_reused": matched_count,
        "matches": dict(match_counts), "start": args.start.isoformat(),
        "end": args.end.isoformat(), "output": str(args.output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
