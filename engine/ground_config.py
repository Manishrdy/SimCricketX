"""
Ground Conditions Configuration Loader

Single source of truth for the pitch model. Every pitch/wicket/scoring number
the engine uses lives in config/ground_conditions_defaults.yaml; the engine
modules read them through here rather than holding their own copies. (Four
diverging copies of these numbers is exactly the bug this module was rewritten
to kill — see utils/squad_rules.py for the same lesson applied elsewhere.)

Per-user, per-format isolation: each user can store an independent config per
match format in the UserGroundConfig table. A stored config is deep-merged OVER
the current factory defaults at read time, so numbers added to the defaults
file after a user saved still reach them.

Match creation snapshots the merged config into the match JSON, so simulation
is deterministic regardless of later edits.
"""

import copy
import logging
from pathlib import Path

import yaml

from utils.exception_tracker import log_exception

logger = logging.getLogger(__name__)

_DEFAULTS_PATH = Path(__file__).parent.parent / "config" / "ground_conditions_defaults.yaml"

# Formats that have a ground-conditions profile. Mirrors MATCH_SETUP_FORMATS
# in routes/match_routes.py and VALID_FORMATS in routes/team_routes.py.
VALID_FORMATS = ("T20", "ListA", "FC")
DEFAULT_FORMAT = "T20"

# Game mode value meaning "let the engine pick per delivery from match state".
AUTO_GAME_MODE = "auto"

_defaults_cache = None

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


# ──────────────────────────── Defaults loading ─────────────────────────────

def _load_defaults():
    """Parse and cache the defaults YAML.

    Raises on a missing or malformed file. This is deliberate: the file ships
    with the application, and the previous silent fallback to hardcoded
    constants is what allowed the engine's numbers to drift away from the
    config without anyone noticing.
    """
    global _defaults_cache
    if _defaults_cache is not None:
        return _defaults_cache

    with open(_DEFAULTS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    formats = data.get("formats")
    if not isinstance(formats, dict) or not formats:
        raise ValueError(
            f"{_DEFAULTS_PATH} has no 'formats' mapping — expected a v2 config"
        )
    missing = [f for f in VALID_FORMATS if f not in formats]
    if missing:
        raise ValueError(f"{_DEFAULTS_PATH} is missing format block(s): {missing}")

    _defaults_cache = data
    _warn_on_bad_matrices(data)
    logger.info("Ground conditions defaults loaded (version %s)", data.get("version"))
    return _defaults_cache


def reload_defaults():
    """Drop the cache so the next read re-parses the YAML (tests / hot edits)."""
    global _defaults_cache
    _defaults_cache = None


def _warn_on_bad_matrices(data):
    """Log a warning for any shipped matrix that doesn't sum to ~1.0."""
    for fmt in VALID_FORMATS:
        block = data.get("formats", {}).get(fmt, {})
        for name, matrix in _iter_matrices(block, fmt):
            total = sum(matrix.values())
            if abs(total - 1.0) > 0.02:
                logger.warning("%s %s matrix sums to %.4f, expected ~1.0", fmt, name, total)


def _iter_matrices(block, match_format):
    """Yield (label, matrix) for every scoring matrix in a format block."""
    if match_format == "ListA":
        for phase, matrix in (block.get("scoring_matrices") or {}).items():
            if isinstance(matrix, dict):
                yield phase, matrix
    else:
        for pitch, profile in (block.get("pitch_profiles") or {}).items():
            matrix = (profile or {}).get("scoring_matrix")
            if isinstance(matrix, dict):
                yield pitch, matrix


def normalise_format(match_format):
    """Coerce an arbitrary input to a supported format name."""
    if match_format in VALID_FORMATS:
        return match_format
    return DEFAULT_FORMAT


def get_defaults(match_format=DEFAULT_FORMAT, mutable=False):
    """Return the factory defaults block for *match_format*.

    The cached dict is returned directly for read-only callers (the per-ball
    hot path); pass mutable=True for a deep copy the caller may modify.
    """
    fmt = normalise_format(match_format)
    block = _load_defaults()["formats"][fmt]
    return copy.deepcopy(block) if mutable else block


def get_pitch_options(match_format, config=None):
    """Return the ordered list of pitch conditions available for *match_format*.

    Every format block carries its own `pitch_profiles` map (name -> profile
    dict with a `description`), so this reads straight from that instead of
    a name list duplicated in the UI — the thing that keeps drifting out of
    sync elsewhere in this codebase. Pass a user's `get_effective_config()`
    result to reflect their saved overrides; omit it for factory defaults.
    """
    fmt = normalise_format(match_format)
    block = config if config is not None else get_defaults(fmt)
    profiles = block.get("pitch_profiles") or {}
    return [
        {"key": name, "description": (profile or {}).get("description", "")}
        for name, profile in profiles.items()
    ]


# ──────────────────────────────── Merging ──────────────────────────────────

def _deep_merge(base, override):
    """Recursively merge *override* onto *base*, returning a new dict.

    Values in *override* win. Keys present only in *base* are preserved — this
    is what lets a defaults-file addition reach a user who saved their config
    before that key existed.
    """
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _normalise_stored(stored, match_format):
    """Coerce a stored config blob into a flat format block.

    Tolerates three shapes:
      • flat v2 block (what save_user_config writes)
      • {"formats": {...}} wrapper, in case a whole document was stored
      • flat v1 (pre-format) blob, which is T20-shaped by definition
    """
    if not isinstance(stored, dict):
        return {}
    if "formats" in stored and isinstance(stored["formats"], dict):
        fmt = normalise_format(match_format)
        return stored["formats"].get(fmt) or {}
    return stored


# ─────────────────────── Per-user config persistence ───────────────────────

def get_user_config(user_id, match_format=DEFAULT_FORMAT):
    """Return the user's stored config block, or None if they have none."""
    fmt = normalise_format(match_format)
    try:
        from database import db
        from database.models import UserGroundConfig
        row = (db.session.query(UserGroundConfig)
               .filter_by(user_id=user_id, match_format=fmt).first())
        return _normalise_stored(row.config_json, fmt) if row else None
    except Exception as e:
        log_exception(e)
        logger.error("get_user_config(%s, %s): %s", user_id, fmt, e)
        return None


def get_effective_config(user_id, match_format=DEFAULT_FORMAT):
    """Return the user's config for *match_format*, merged over factory defaults.

    Always returns a fresh dict the caller may mutate. Callers with no stored
    config get the defaults verbatim.
    """
    fmt = normalise_format(match_format)
    defaults = get_defaults(fmt, mutable=True)
    stored = get_user_config(user_id, fmt)
    if not stored:
        return defaults
    return _deep_merge(defaults, stored)


def save_user_config(user_id, config_dict, match_format=DEFAULT_FORMAT):
    """Validate and persist a user's config for one format.

    Returns (True, None) on success or (False, error_str) on failure.
    """
    fmt = normalise_format(match_format)
    ok, err = _validate_config(config_dict, fmt)
    if not ok:
        return False, err
    try:
        from datetime import datetime
        from database import db
        from database.models import UserGroundConfig
        row = (db.session.query(UserGroundConfig)
               .filter_by(user_id=user_id, match_format=fmt).first())
        if row:
            row.config_json = config_dict
            row.updated_at = datetime.utcnow()
        else:
            row = UserGroundConfig(user_id=user_id, match_format=fmt,
                                   config_json=config_dict)
            db.session.add(row)
        db.session.commit()
        return True, None
    except Exception as e:
        log_exception(e)
        logger.error("save_user_config(%s, %s): %s", user_id, fmt, e)
        try:
            from database import db
            db.session.rollback()
        except Exception:
            pass
        return False, str(e)


def reset_user_config(user_id, match_format=DEFAULT_FORMAT):
    """Delete the user's stored config, reverting them to factory defaults.

    Pass match_format=None to clear every format for that user.
    Returns (True, None) on success or (False, error_str) on failure.
    """
    try:
        from database import db
        from database.models import UserGroundConfig
        query = db.session.query(UserGroundConfig).filter_by(user_id=user_id)
        if match_format is not None:
            query = query.filter_by(match_format=normalise_format(match_format))
        for row in query.all():
            db.session.delete(row)
        db.session.commit()
        return True, None
    except Exception as e:
        log_exception(e)
        logger.error("reset_user_config(%s, %s): %s", user_id, match_format, e)
        try:
            from database import db
            db.session.rollback()
        except Exception:
            pass
        return False, str(e)


def _validate_config(config_dict, match_format=DEFAULT_FORMAT):
    """Validate every scoring matrix in *config_dict* sums to ~1.0.

    Returns (True, None) on success or (False, error_str) on failure.
    """
    fmt = normalise_format(match_format)
    if not isinstance(config_dict, dict):
        return False, "Config must be an object"
    for label, matrix in _iter_matrices(config_dict, fmt):
        try:
            total = sum(matrix.values())
        except TypeError:
            return False, f"{label} scoring matrix contains a non-numeric value"
        if abs(total - 1.0) > 0.02:
            return False, f"{label} scoring matrix sums to {total:.4f}, must be ~1.0"
    return True, None


# ───────────────────────── Shared (T20) accessors ──────────────────────────
# Every accessor takes an optional *config* — a user's merged snapshot. When
# omitted the factory defaults are used, which is the path legacy matches,
# direct engine calls and tests take.

def _block(config, match_format):
    return config if config is not None else get_defaults(match_format)


def get_pitch_profile(pitch_type, config=None):
    """Return the T20 profile dict for a pitch type, or None."""
    return (_block(config, "T20").get("pitch_profiles") or {}).get(pitch_type)


def get_active_game_mode_name(config=None):
    """Return the configured game mode, or "auto" to let the engine choose."""
    return _block(config, "T20").get("active_game_mode", AUTO_GAME_MODE)


def get_scoring_matrix(pitch_type, mode_override=None, config=None):
    """
    Return the T20 scoring matrix for a pitch with game-mode modifiers applied,
    re-normalized to sum to 1.0. Returns None when the pitch is unknown.

    mode_override names the game mode to apply; when omitted the config's own
    active_game_mode is used. "auto" applies no modifiers — the caller
    (Match._resolve_game_mode) is responsible for turning "auto" into a
    concrete mode per delivery.
    """
    profile = get_pitch_profile(pitch_type, config=config)
    if not profile or "scoring_matrix" not in profile:
        return None

    base_matrix = dict(profile["scoring_matrix"])
    block = _block(config, "T20")

    mode_name = mode_override or block.get("active_game_mode", AUTO_GAME_MODE)
    mode = None
    if mode_name and mode_name != AUTO_GAME_MODE:
        mode = (block.get("game_modes") or {}).get(mode_name)

    if mode:
        modifiers = mode.get("modifiers", {})
        for outcome, mod_key in OUTCOME_MODIFIER_MAP.items():
            if outcome in base_matrix:
                base_matrix[outcome] *= modifiers.get(mod_key, 1.0)

    total = sum(base_matrix.values())
    if total > 0:
        base_matrix = {k: v / total for k, v in base_matrix.items()}

    return base_matrix


def get_game_modes(config=None):
    """Return the T20 game modes dict (for UI rendering and mode pinning)."""
    return _block(config, "T20").get("game_modes") or {}


def get_run_factor(pitch_type, config=None):
    """Return the T20 run-factor multiplier for a pitch. None if unknown."""
    profile = get_pitch_profile(pitch_type, config=config)
    return profile.get("run_factor") if profile else None


def get_wicket_factors(pitch_type, config=None):
    """Return the T20 bowling-style-keyed wicket factors. None if unknown."""
    profile = get_pitch_profile(pitch_type, config=config)
    return profile.get("wicket_factors") if profile else None


def get_phase_boosts(config=None):
    """Return the T20 phase boosts dict. None if absent."""
    return _block(config, "T20").get("phase_boosts")


def get_blending_weights(config=None, match_format=DEFAULT_FORMAT):
    """Return (pitch_weight, skill_weight), or None if absent."""
    blending = _block(config, match_format).get("blending")
    if blending:
        return blending.get("pitch_weight", 0.6), blending.get("skill_weight", 0.4)
    return None


# ─────────────────────────── ListA accessors ───────────────────────────────

def get_lista_matrix(phase, config=None):
    """Return the ListA base scoring matrix for a phase (pp1/middle/death)."""
    matrices = _block(config, "ListA").get("scoring_matrices") or {}
    return matrices.get(phase)


def get_lista_pitch_profile(pitch_type, config=None):
    """Return the ListA profile dict for a pitch type, or {}."""
    return (_block(config, "ListA").get("pitch_profiles") or {}).get(pitch_type) or {}


def get_lista_run_factor(pitch_type, config=None):
    """Return the ListA per-pitch run factor (default 1.0)."""
    return get_lista_pitch_profile(pitch_type, config=config).get("run_factor", 1.0)


def get_lista_wicket_mult(pitch_type, config=None):
    """Return the ListA per-pitch wicket multiplier (default 1.0)."""
    return get_lista_pitch_profile(pitch_type, config=config).get("wicket_mult", 1.0)


def get_lista_wicket_factors(pitch_type, config=None):
    """Return the ListA bowling-style-keyed wicket factors for a pitch, or {}.

    Same schema as the T20 `wicket_factors` block: keys are bowling_type names
    with a `default` fallback. ListA had no per-style factors at all until
    2026-08-16 — only the scalar `wicket_mult` — so a green top and a turner
    distributed wickets identically and pitch character was invisible in who
    took them.
    """
    return get_lista_pitch_profile(pitch_type, config=config).get("wicket_factors") or {}


def get_lista_wicket_factor_for(pitch_type, bowling_type, config=None):
    """Resolve one bowling style's ListA wicket factor (1.0 when unconfigured)."""
    factors = get_lista_wicket_factors(pitch_type, config=config)
    if not factors:
        return 1.0
    return factors.get(bowling_type, factors.get("default", 1.0))


def get_lista_dot_single(pitch_type, config=None):
    """Return the ListA per-pitch dot/single rotation nudges, or {}."""
    return get_lista_pitch_profile(pitch_type, config=config).get("dot_single") or {}


def get_lista_fine_tune(pitch_type, config=None):
    """Return the ListA per-pitch post-wear fine-tuning multipliers, or {}."""
    return get_lista_pitch_profile(pitch_type, config=config).get("fine_tune") or {}


def get_lista_phase_boosts(config=None):
    """Return the ListA phase boost table, or {}."""
    return _block(config, "ListA").get("phase_boosts") or {}


def get_lista_pitch_wear(config=None):
    """Return the ListA per-pitch wear spec table, or {}."""
    return _block(config, "ListA").get("pitch_wear") or {}


def get_lista_dew(config=None):
    """Return the ListA dew spec, or {}."""
    return _block(config, "ListA").get("dew") or {}


# ───────────────────────── First-Class (FC) accessors ──────────────────────
# FC's pitch-support-differs-by-bowler-type dynamic (the reason FC exists as
# a format at all) is NOT a static per-pitch table like T20's wicket_factors
# — it interpolates between a "fresh pitch" table and a "fully worn" table
# as the match's continuous wear scalar climbs. See
# engine/fc_bowler_workload.py and the module docstring in the FC YAML block
# for the "why".

def get_fc_pitch_profile(pitch_type, config=None):
    """Return the FC profile dict for a pitch type, or {}."""
    return (_block(config, "FC").get("pitch_profiles") or {}).get(pitch_type) or {}


def get_fc_scoring_matrix(pitch_type, config=None):
    """Return the FC base scoring matrix for a pitch, or None if unknown."""
    profile = get_fc_pitch_profile(pitch_type, config=config)
    matrix = profile.get("scoring_matrix")
    return dict(matrix) if matrix else None


def get_fc_run_factor(pitch_type, config=None):
    """Return the FC per-pitch run factor (default 1.0)."""
    return get_fc_pitch_profile(pitch_type, config=config).get("run_factor", 1.0)


def get_fc_wicket_factors_start(pitch_type, config=None):
    """Return the FC "fresh pitch" bowling-style wicket factors, or {}."""
    return get_fc_pitch_profile(pitch_type, config=config).get("wicket_factors_start") or {}


def get_fc_wicket_factors_end(pitch_type, config=None):
    """Return the FC "fully worn" bowling-style wicket factors, or {}."""
    return get_fc_pitch_profile(pitch_type, config=config).get("wicket_factors_end") or {}


def get_fc_wicket_factor_for(pitch_type, bowling_type, pitch_wear, config=None):
    """
    Resolve one bowling style's FC wicket factor at the current match wear
    (0.0 = fresh pitch, 1.0 = fully worn), linearly interpolated between
    wicket_factors_start and wicket_factors_end. 1.0 (neutral) when either
    table is unconfigured for this pitch/bowling_type.
    """
    start = get_fc_wicket_factors_start(pitch_type, config=config)
    end = get_fc_wicket_factors_end(pitch_type, config=config)
    if not start and not end:
        return 1.0
    w = max(0.0, min(1.0, pitch_wear))
    start_val = start.get(bowling_type, start.get("default", 1.0))
    end_val = end.get(bowling_type, end.get("default", start_val))
    return start_val + (end_val - start_val) * w


def get_fc_pitch_wear(config=None):
    """Return the FC per-pitch general wear spec table, or {}."""
    return _block(config, "FC").get("pitch_wear") or {}


def get_fc_ball_condition(config=None):
    """Return the FC ball-condition spec (Phase 2 — new-ball/reverse-swing
    modeling), or {}. Pitch-independent, unlike the wear/wicket-factor
    tables above."""
    return _block(config, "FC").get("ball_condition") or {}


def get_fc_ball_condition_factor(bowling_type, ball_overs_bowled, new_ball_overs, config=None):
    """
    Resolve the ball-condition wicket multiplier for one delivery, given
    how many overs the CURRENT ball has been in use (ball_overs_bowled,
    reset at the start of every innings and whenever the new ball is taken
    at new_ball_overs — see Match.fc_ball_overs_bowled) and how many overs
    a ball stays in use before the next new ball is due (new_ball_overs,
    fmt.new_ball_overs).

    Three windows, resolved from this single ball-age counter:
      - [0, new_ball_swing_overs): fresh-ball swing — boosts genuine
        pace/swing bowlers.
      - middle overs: flat — neutral (1.0) for everyone.
      - [new_ball_overs - reverse_swing_window_overs, new_ball_overs):
        old-ball reverse-swing — boosts genuinely fast bowlers only.

    1.0 (neutral) when the ball_condition block is unconfigured.
    """
    spec = get_fc_ball_condition(config=config)
    if not spec:
        return 1.0

    swing_overs = spec.get("new_ball_swing_overs", 10)
    reverse_window = spec.get("reverse_swing_window_overs", 15)

    if ball_overs_bowled < swing_overs:
        factors = spec.get("new_ball_wicket_factors") or {}
        return factors.get(bowling_type, factors.get("default", 1.0))

    if ball_overs_bowled >= new_ball_overs - reverse_window:
        factors = spec.get("reverse_swing_wicket_factors") or {}
        return factors.get(bowling_type, factors.get("default", 1.0))

    return 1.0


def get_fc_ball_condition_outcome_factors(bowling_type, ball_overs_bowled,
                                          new_ball_overs, config=None):
    """
    Per-outcome multipliers for the current ball's condition — the scoring
    counterpart to get_fc_ball_condition_factor's wicket scalar, returned
    together so a caller applies one dict.

    The wicket term stays keyed by bowling style (only genuine pace swings a
    new ball, only pace reverses an old one). The scoring terms are NOT: a
    hard new ball comes onto the bat and carries to the rope whoever is
    bowling, and a scuffed old one is hard work off anybody.

    Returns {} when the ball_condition block is unconfigured, so callers can
    apply it unconditionally.
    """
    spec = get_fc_ball_condition(config=config)
    if not spec:
        return {}

    swing_overs = spec.get("new_ball_swing_overs", 10)
    reverse_window = spec.get("reverse_swing_window_overs", 15)

    if ball_overs_bowled < swing_overs:
        wicket_key, scoring_key = "new_ball_wicket_factors", "new_ball_scoring_factors"
    elif ball_overs_bowled >= new_ball_overs - reverse_window:
        wicket_key = "reverse_swing_wicket_factors"
        scoring_key = "reverse_swing_scoring_factors"
    else:
        return {}   # middle overs: the ball is doing nothing special

    factors = dict(spec.get(scoring_key) or {})
    wicket_factors = spec.get(wicket_key) or {}
    factors["Wicket"] = wicket_factors.get(
        bowling_type, wicket_factors.get("default", 1.0))
    return factors


def get_fc_rough_targeting(config=None):
    """Return the FC rough-targeting spec (per-pitch max bonus at full
    wear), or {}. Pitch-keyed like pitch_wear/wicket_factors, but a single
    scalar per pitch rather than a bowling-style table — see
    get_fc_rough_targeting_factor for how it's actually resolved."""
    return _block(config, "FC").get("rough_targeting") or {}


def get_fc_rough_targeting_factor(bowling_type, batting_hand, pitch_wear, pitch, config=None):
    """
    Resolve the handedness-specific rough-targeting wicket multiplier for
    one delivery.

    Footmark rough only exists where days of the same bowling angle have
    worn the same patch of turf — it is a wear-*and*-handedness-matchup
    effect, distinct from both the general wear-interpolated
    get_fc_wicket_factor_for (bowling-style only, no handedness) and the
    format-agnostic compute_matchup_boost in ball_outcome.py (handedness
    only, static — no wear scaling, so it treats a fresh Day 1 pitch and a
    crumbling Day 5 one identically). This layers a wear-ramped bonus on
    top of that static matchup, active only for the two classic
    turning-away-from-the-bat combinations:
      - Off spin / Finger spin bowling to a Left-hand batter
      - Leg spin / Wrist spin bowling to a Right-hand batter
    1.0 (neutral) for every other combination, or when the pitch has no
    rough_targeting entry configured (e.g. a pace-only matchup, or a pitch
    type — like a true Hard track — that doesn't break up enough to matter).
    """
    turns_away = (
        (bowling_type in ("Off spin", "Finger spin") and batting_hand == "Left")
        or (bowling_type in ("Leg spin", "Wrist spin") and batting_hand == "Right")
    )
    if not turns_away:
        return 1.0

    bonus = get_fc_rough_targeting(config=config).get(pitch, 0.0)
    if not bonus:
        return 1.0

    w = max(0.0, min(1.0, pitch_wear))
    return 1.0 + bonus * w


# ───────────────────────── GSME baseline coupling ──────────────────────────

def get_rrr_baseline(pitch, fmt, config=None):
    """Return the "neutral" run rate for *pitch*, adjusted for a custom config.

    GSME divides the required run rate by this baseline to get a normalised
    aggression index. The baseline lives in FormatConfig as a fixed per-pitch
    table, so a user who makes a pitch more run-friendly used to shift actual
    scoring while GSME kept judging them against the stock number — the
    momentum engine then misread how hard the chase really was.

    Scaling the baseline by (user run factor / default run factor) keeps the
    index meaningful. A user on stock settings gets exactly the stock value.
    """
    baseline = getattr(fmt, "rrr_baseline", {}).get(pitch) if fmt is not None else None
    if baseline is None:
        return None

    fmt_name = normalise_format(getattr(fmt, "name", DEFAULT_FORMAT))
    if fmt_name == "ListA":
        user_factor = get_lista_run_factor(pitch, config=config)
        default_factor = get_lista_run_factor(pitch)
    else:
        user_factor = get_run_factor(pitch, config=config)
        default_factor = get_run_factor(pitch)

    if not user_factor or not default_factor:
        return baseline
    return baseline * (user_factor / default_factor)
