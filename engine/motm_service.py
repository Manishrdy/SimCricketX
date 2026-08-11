# -*- coding: utf-8 -*-
"""
Man of the Match selection + dynamic (non-AI) post-match commentary.

Selection reuses StatsService.compute_match_impact_scores() for the actual
Impact Index numbers (engine/stats_service.py) — this module only adds
MOTM-specific logic on top: winning-side weighting, tie-break, and
template-based "MOTM speaks" quote generation (random.choice + str.format
over data/motm_quotes.json, the same mechanics CommentaryEngine already uses
for ball-by-ball narrative text — no AI/LLM involved).
"""

import json
import logging
import os
import random

from utils.exception_tracker import log_exception

logger = logging.getLogger(__name__)

# Winning-side players get their Impact Index multiplied by this before
# comparison. Losing-side players remain eligible and can still win with a
# genuinely dominant performance.
WINNING_SIDE_BONUS = 1.2

CLOSE_FINISH_WICKETS_MARGIN = 2
CLOSE_FINISH_RUNS_MARGIN = 10
ALL_ROUNDER_RUNS_THRESHOLD = 25
ALL_ROUNDER_WICKETS_THRESHOLD = 2

_QUOTES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "motm_quotes.json"
)
_quotes_cache = None


def _load_quotes():
    global _quotes_cache
    if _quotes_cache is None:
        try:
            with open(_QUOTES_PATH, "r") as f:
                _quotes_cache = json.load(f)
        except Exception as e:
            log_exception(e)
            logger.error(f"Failed to load MOTM quotes from {_QUOTES_PATH}: {e}")
            _quotes_cache = {"archetype": {}, "context": {}}
    return _quotes_cache


def select_match_motm(match_id, winner_team_id, match_status):
    """Pick Man of the Match for a single match.

    Returns a dict (player_id, player_name, team_id, team_name, runs,
    wickets, catches, run_outs, impact_score, weighted_score,
    is_winning_side) or None if no MOTM should be awarded — either the
    match had insufficient play (no_result/aborted) or no scorecard rows
    exist yet for it (scorecard save is best-effort/non-fatal upstream).
    """
    if match_status in ("no_result", "aborted"):
        return None

    from engine.stats_service import StatsService
    scores = StatsService().compute_match_impact_scores(match_id)
    if not scores:
        return None

    is_tied = match_status == "tied"

    def _weighted(entry):
        if is_tied or not winner_team_id:
            return entry["impact_score"]
        bonus = WINNING_SIDE_BONUS if entry["team_id"] == winner_team_id else 1.0
        return entry["impact_score"] * bonus

    # Deterministic tie-break: highest weighted score, then highest raw
    # impact, then most runs, then lowest player_id.
    best = max(
        scores,
        key=lambda e: (_weighted(e), e["impact_score"], e["runs"], -e["player_id"]),
    )

    return {
        **best,
        "weighted_score": _weighted(best),
        "is_winning_side": bool(winner_team_id) and best["team_id"] == winner_team_id,
    }


def _classify_archetype(motm_result):
    runs = motm_result["runs"] or 0
    wickets = motm_result["wickets"] or 0
    catches = motm_result["catches"] or 0
    run_outs = motm_result["run_outs"] or 0

    if runs >= ALL_ROUNDER_RUNS_THRESHOLD and wickets >= ALL_ROUNDER_WICKETS_THRESHOLD:
        return "all_rounder"

    bat_component = runs
    bowl_component = wickets * 20
    field_component = catches * 8 + run_outs * 10

    top = max(bat_component, bowl_component, field_component)
    if top == bowl_component and bowl_component > 0:
        return "bowling_hero"
    if top == field_component and field_component > 0:
        return "fielding_hero"
    return "batting_hero"


def _classify_context(match):
    status = getattr(match, "match_status", None)
    margin_type = getattr(match, "margin_type", None)
    margin_value = getattr(match, "margin_value", None) or 0

    if status == "tied":
        return "tied"
    if margin_type == "wickets":
        return "close_finish" if margin_value <= CLOSE_FINISH_WICKETS_MARGIN else "chase_won"
    if margin_type == "runs":
        return "close_finish" if margin_value <= CLOSE_FINISH_RUNS_MARGIN else "defended"
    return "comfortable_win"


def _stat_line(motm_result):
    parts = []
    if motm_result["runs"]:
        parts.append(f"{motm_result['runs']} runs")
    if motm_result["wickets"]:
        parts.append(f"{motm_result['wickets']} wicket{'s' if motm_result['wickets'] != 1 else ''}")
    if motm_result["catches"]:
        parts.append(f"{motm_result['catches']} catch{'es' if motm_result['catches'] != 1 else ''}")
    if motm_result["run_outs"]:
        parts.append(f"{motm_result['run_outs']} run out{'s' if motm_result['run_outs'] != 1 else ''}")
    return ", ".join(parts) if parts else "a match-defining all-round effort"


def build_motm_commentary(motm_result, match):
    """Build the HTML block appended to the end-of-match commentary — a
    stat line plus a randomly-assembled quote (archetype line + match-context
    line), entirely template-driven."""
    if not motm_result:
        return ""

    quotes = _load_quotes()
    archetype_templates = quotes.get("archetype", {}).get(_classify_archetype(motm_result)) or []
    context_templates = quotes.get("context", {}).get(_classify_context(match)) or []

    fmt_kwargs = {
        "player": motm_result["player_name"],
        "team": motm_result["team_name"],
        "runs": motm_result["runs"],
        "wickets": motm_result["wickets"],
        "catches": motm_result["catches"],
        "run_outs": motm_result["run_outs"],
    }

    lines = []
    if archetype_templates:
        lines.append(random.choice(archetype_templates).format(**fmt_kwargs))
    if context_templates:
        lines.append(random.choice(context_templates).format(**fmt_kwargs))
    quote = " ".join(lines) if lines else f"{motm_result['player_name']} delivered when it mattered most."

    return (
        f'<br><br><strong>\U0001f3a4 Man of the Match: {motm_result["player_name"]} '
        f'({motm_result["team_name"]})</strong><br>'
        f'{_stat_line(motm_result)}<br>'
        f'<em>"{quote}"</em>'
    )
