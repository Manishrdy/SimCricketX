"""
Story Packs (historical scenario arcs)
======================================
Loader for data-driven match stories stored as JSON files in
``engine/data/scenarios/``.  Each pack describes the *pressure arc* of a
famous real-world match — per-innings "beats": score/wicket corridors at over
checkpoints — which the HistoricalScenarioEngine steers any user match
through.  Packs are team-agnostic: the user picks their own teams, venue, and
pitch in regular match setup; the story only shapes the match's narrative.

Corridor forms per checkpoint:
  - "par_pct":   fraction of the engine's par score at that over for the
                 match's pitch/format (innings 1 — adapts to any conditions)
  - "score_pct": fraction of the chase target (innings 2)
  - "score":     absolute runs corridor (fixed-conditions packs)
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

SCENARIO_DIR = os.path.join(os.path.dirname(__file__), "data", "scenarios")

_PACK_ID_RE = re.compile(r"^[a-z0-9_]+$")

REQUIRED_TOP_KEYS = ("id", "title", "format", "beats")
_CORRIDOR_KEYS = ("score", "score_pct", "par_pct")


def _validate_pack(pack):
    """Return None if the pack is structurally sound, else an error string."""
    for key in REQUIRED_TOP_KEYS:
        if key not in pack:
            return f"missing key '{key}'"
    if not _PACK_ID_RE.match(pack["id"]):
        return "invalid pack id"

    for innings_key, checkpoints in (pack["beats"] or {}).items():
        if innings_key not in ("1", "2"):
            return f"beats key must be '1' or '2', got '{innings_key}'"
        prev_over = 0
        for cp in checkpoints:
            at_over = cp.get("at_over")
            if not isinstance(at_over, int) or at_over <= prev_over:
                return f"innings {innings_key} checkpoints must have increasing integer at_over"
            prev_over = at_over
            if not any(k in cp for k in _CORRIDOR_KEYS):
                return (
                    f"innings {innings_key} checkpoint at over {at_over} "
                    f"needs one of {_CORRIDOR_KEYS}"
                )

    return None


def get_scenario_pack(pack_id):
    """Load and validate a single scenario pack by id. Returns dict or None."""
    if not isinstance(pack_id, str) or not _PACK_ID_RE.match(pack_id):
        return None
    path = os.path.join(SCENARIO_DIR, f"{pack_id}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            pack = json.load(f)
    except (OSError, ValueError) as e:
        logger.error("[ScenarioPack] Failed to load %s: %s", pack_id, e)
        return None

    err = _validate_pack(pack)
    if err:
        logger.error("[ScenarioPack] Invalid pack %s: %s", pack_id, err)
        return None
    return pack


def list_scenario_packs():
    """Return all valid scenario packs, sorted by title."""
    packs = []
    if not os.path.isdir(SCENARIO_DIR):
        return packs
    for fname in sorted(os.listdir(SCENARIO_DIR)):
        if not fname.endswith(".json"):
            continue
        pack = get_scenario_pack(fname[:-5])
        if pack:
            packs.append(pack)
    return sorted(packs, key=lambda p: p.get("title", ""))
