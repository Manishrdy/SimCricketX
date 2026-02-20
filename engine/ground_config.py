"""
Ground Conditions Configuration Loader

Centralizes all pitch/wicket/scoring factors into a YAML-based config.
Provides getter functions for ball_outcome.py and other engine modules.
Falls back gracefully if YAML is missing — engine uses hardcoded constants.
"""

import os
import yaml
import logging
import copy

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(
    os.path.abspath(os.path.dirname(__file__)), "..", "config", "ground_conditions.yaml"
)
_cached_config = None
_loaded = False

OUTCOME_MODIFIER_MAP = {
    "Dot": "dot_mult",
    "Single": "single_mult",
    "Double": "double_mult",
    "Three": "three_mult",
    "Four": "four_mult",
    "Six": "six_mult",
    "Wicket": "wicket_mult",
    "Extras": "extras_mult",
}


def _load():
    """Load config from YAML file."""
    global _cached_config, _loaded
    _loaded = True
    if not os.path.exists(_CONFIG_PATH):
        logger.warning("ground_conditions.yaml not found, engine will use hardcoded defaults")
        _cached_config = None
        return
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            _cached_config = yaml.safe_load(f) or {}
        _validate_matrices()
        logger.info("Ground conditions config loaded")
    except Exception as e:
        logger.error(f"Failed to load ground_conditions.yaml: {e}")
        _cached_config = None


def reload():
    """Force reload from disk (called after admin saves changes)."""
    global _loaded
    _loaded = False
    _load()


def _validate_matrices():
    """Warn if any scoring matrix doesn't sum to ~1.0."""
    if not _cached_config:
        return
    for pitch, profile in _cached_config.get("pitch_profiles", {}).items():
        matrix = profile.get("scoring_matrix", {})
        total = sum(matrix.values())
        if abs(total - 1.0) > 0.02:
            logger.warning(f"Pitch '{pitch}' scoring matrix sums to {total:.4f}, expected ~1.0")


def get_config():
    """Return the full config dict (for UI rendering)."""
    if not _loaded:
        _load()
    return _cached_config


def get_pitch_profile(pitch_type):
    """Return the full profile dict for a pitch type."""
    cfg = get_config()
    if cfg:
        return cfg.get("pitch_profiles", {}).get(pitch_type)
    return None


def get_active_game_mode_name():
    """Return the name of the active game mode."""
    cfg = get_config()
    if cfg:
        return cfg.get("active_game_mode", "natural_game")
    return "natural_game"


def get_active_game_mode():
    """Return the full game mode dict for the active mode."""
    cfg = get_config()
    if cfg:
        mode_name = cfg.get("active_game_mode", "natural_game")
        return cfg.get("game_modes", {}).get(mode_name)
    return None


def get_scoring_matrix(pitch_type, mode_override: str = None):
    """
    Return the scoring matrix for a pitch type with game mode modifiers applied.
    Probabilities are re-normalized to sum to 1.0.
    Returns None if config unavailable (caller should use hardcoded fallback).

    Parameters
    ----------
    pitch_type    : str  – e.g. 'Green', 'Flat', 'Hard', 'Dry', 'Dead'
    mode_override : str  – optional; if provided, uses this game mode instead
                          of the statically configured active_game_mode.
                          Supports Feature 13 (Dynamic Game Mode).
    """
    profile = get_pitch_profile(pitch_type)
    if not profile or "scoring_matrix" not in profile:
        return None

    base_matrix = dict(profile["scoring_matrix"])
    cfg = get_config()

    if mode_override and cfg:
        mode = cfg.get("game_modes", {}).get(mode_override)
    else:
        mode = get_active_game_mode()

    if mode:
        modifiers = mode.get("modifiers", {})
        for outcome, mod_key in OUTCOME_MODIFIER_MAP.items():
            if outcome in base_matrix:
                base_matrix[outcome] *= modifiers.get(mod_key, 1.0)

    # Re-normalize to 1.0
    total = sum(base_matrix.values())
    if total > 0:
        base_matrix = {k: v / total for k, v in base_matrix.items()}

    return base_matrix


def get_run_factor(pitch_type):
    """Return the run factor multiplier for a pitch type. None if unavailable."""
    profile = get_pitch_profile(pitch_type)
    if profile:
        return profile.get("run_factor")
    return None


def get_wicket_factors(pitch_type):
    """Return bowling-style-keyed wicket factors dict. None if unavailable."""
    profile = get_pitch_profile(pitch_type)
    if profile:
        return profile.get("wicket_factors")
    return None


def get_phase_boosts():
    """Return the phase boosts config dict. None if unavailable."""
    cfg = get_config()
    if cfg:
        return cfg.get("phase_boosts")
    return None


def get_blending_weights():
    """Return (pitch_weight, skill_weight) tuple. None if unavailable."""
    cfg = get_config()
    if cfg:
        blending = cfg.get("blending")
        if blending:
            return blending.get("pitch_weight", 0.6), blending.get("skill_weight", 0.4)
    return None


def get_game_modes():
    """Return all game modes dict (for UI rendering)."""
    cfg = get_config()
    if cfg:
        return cfg.get("game_modes", {})
    return {}


def save_config(config_dict):
    """
    Write the full config dict to YAML and reload.
    Returns (success: bool, error_msg: str or None).
    """
    # Validate scoring matrices
    for pitch, profile in config_dict.get("pitch_profiles", {}).items():
        matrix = profile.get("scoring_matrix", {})
        total = sum(matrix.values())
        if abs(total - 1.0) > 0.02:
            return False, f"{pitch} scoring matrix sums to {total:.4f}, must be ~1.0"

    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        reload()
        return True, None
    except Exception as e:
        logger.error(f"Failed to save ground_conditions.yaml: {e}")
        return False, str(e)


def reset_to_defaults():
    """Copy defaults YAML over the active config and reload."""
    import shutil
    defaults_path = os.path.join(
        os.path.abspath(os.path.dirname(__file__)), "..", "config", "ground_conditions_defaults.yaml"
    )
    if not os.path.exists(defaults_path):
        return False, "Defaults file not found"
    try:
        shutil.copy2(defaults_path, _CONFIG_PATH)
        reload()
        return True, None
    except Exception as e:
        logger.error(f"Failed to reset ground conditions: {e}")
        return False, str(e)


# Auto-load on import
_load()
