"""
engine/format_config.py
=======================

Single source of truth for all format-specific parameters in SimCricketX.
Last reviewed: 2026-04-30.

FormatConfig centralizes simulation parameters. List A supports scheduled
40- and 50-over variants while retaining the same team and rating identity.

Usage
-----
    from engine.format_config import FORMAT_REGISTRY, FormatConfig

    fmt = FORMAT_REGISTRY.get(match_data.get("match_format", "T20"),
                              FORMAT_REGISTRY["T20"])
    fmt.overs            # 20 or 50
    fmt.max_bowler_overs # 4 or 10
    fmt.is_death(over)   # True/False
    fmt.get_phase(over)  # Phase object
"""

import dataclasses
import copy
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union
from engine.format_catalog import FORMAT_CATALOG, default_scheduled_overs


# ---------------------------------------------------------------------------
# Phase descriptor
# ---------------------------------------------------------------------------

@dataclass
class Phase:
    """Describes one scoring/fielding phase within a format."""
    name: str
    start: int               # first over index (0-based, inclusive)
    end: int                 # last over index (0-based, inclusive)
    max_fielders_outside: int = 4   # fielders permitted outside 30-yard circle


# ---------------------------------------------------------------------------
# FormatConfig
# ---------------------------------------------------------------------------

@dataclass
class FormatConfig:
    """
    Complete parameterisation of a cricket format.

    Attributes
    ----------
    name                    : canonical format name ("T20", "ListA")
    overs                   : overs per innings
    max_bowler_overs        : bowling quota per bowler per innings
    allow_consecutive_overs : whether a bowler may bowl back-to-back overs
    powerplay_phases        : ordered list of Powerplay Phase objects
    middle_phase            : the consolidation/middle Phase
    death_phase             : the final/slog Phase
    par_scores              : {over_index: cumulative_runs} for 1st innings
                              (neutral/Hard pitch baseline)
    pitch_par_factors       : per-pitch multiplier on par_scores
    expected_rr             : {phase_key: runs_per_over} for pressure engine
    extras_per_innings      : tuning target for extra deliveries
    target_scores           : {pitch_type: expected_1st_innings_total}
    correct_toss_choice     : {pitch_type: "bat"|"bowl"} optimal toss decision
    """
    name: str
    overs: int
    max_bowler_overs: int
    allow_consecutive_overs: bool
    powerplay_phases: List[Phase]
    middle_phase: Phase
    death_phase: Phase
    par_scores: Dict[int, float]
    pitch_par_factors: Dict[str, float]
    expected_rr: Dict[str, float]
    extras_per_innings: int
    target_scores: Dict[str, int]
    correct_toss_choice: Dict[str, str]
    # D/N override: in floodlit matches dew in the 2nd innings almost always
    # makes bowling first correct.  None = fall back to correct_toss_choice.
    correct_toss_choice_dn: Optional[Dict[str, str]]
    # Per-pitch "neutral" run-rate used by GSME to normalise required_aggression.
    # T20:   Hard pitch neutral ≈ 8.5 RPO (IPL / international averages)
    # ListA: Hard pitch neutral ≈ 6.0 RPO (ODI first-innings scoring rate)
    rrr_baseline: Dict[str, float]

    # Format family tag. "limited_overs" (the default) covers every field
    # above — fixed overs, bowler quota, fielding-circle phases, a single
    # target. "multi_day" (see MultiDayFormatConfig below) is a genuinely
    # different shape and does not populate this dataclass at all; engine
    # code should branch on this tag rather than on `match_format` strings.
    format_family: str = "limited_overs"

    # The free hit is a limited-overs invention. The no-ball itself (one-run
    # penalty, delivery not counted, restricted modes of dismissal) is a Law
    # and applies everywhere; the free hit that follows it is not, and the
    # first-class playing conditions do not provide for one.
    free_hit_after_no_ball: bool = True
    # Original match length: rain may mutate overs, but never this value.
    scheduled_overs: Optional[int] = None
    strict_short_bowling: bool = False
    momentum_window: int = 18
    dot_thresholds: tuple = (2, 4, 6, 8)
    partnership_thresholds: tuple = (25, 50, 75, 100)

    def revise_short_innings(self, overs: int) -> None:
        """Rebuild short-format rules; never change the scheduled length."""
        self.overs = overs
        self.max_bowler_overs = max(1, math.ceil(overs / 5))
        pp = 1 if overs <= 4 else 2 if overs <= 8 else 3
        death_start = max(pp, overs - math.ceil(overs * .3))
        self.powerplay_phases = [Phase("Powerplay", 0, pp - 1, 2)]
        self.middle_phase = Phase("Middle", pp, death_start - 1, 5)
        self.death_phase = Phase("Death", death_start, overs - 1, 5)
        if self.strict_short_bowling:
            self.par_scores = {0: 0.0}
            for over in range(overs):
                self.par_scores[over + 1] = self.par_scores[over] + self.expected_rr[self.phase_key(over)]

    # ------------------------------------------------------------------ #
    # Phase helpers                                                        #
    # ------------------------------------------------------------------ #

    def get_phase(self, over: int) -> Phase:
        """
        Return the Phase that contains the given over index.

        Checks powerplay phases first (in order), then death, then middle.
        Falls back to middle if nothing matches (should not happen in valid
        over range).
        """
        for pp in self.powerplay_phases:
            if pp.start <= over <= pp.end:
                return pp
        if self.death_phase.start <= over <= self.death_phase.end:
            return self.death_phase
        return self.middle_phase

    def is_powerplay(self, over: int) -> bool:
        return any(pp.start <= over <= pp.end for pp in self.powerplay_phases)

    def is_middle(self, over: int) -> bool:
        return (self.middle_phase.start <= over <= self.middle_phase.end
                and not self.is_powerplay(over)
                and not self.is_death(over))

    def is_death(self, over: int) -> bool:
        return self.death_phase.start <= over <= self.death_phase.end

    def phase_key(self, over: int) -> str:
        """Return a string key suitable for expected_rr lookups."""
        phase = self.get_phase(over)
        return phase.name

    def max_fielders_outside(self, over: int) -> int:
        return self.get_phase(over).max_fielders_outside


# ---------------------------------------------------------------------------
# T20 FormatConfig
# ---------------------------------------------------------------------------

_T20_PAR_SCORES: Dict[int, float] = {
    0:   0.0,
    1:   7.0,
    2:  14.5,
    3:  22.0,
    4:  30.5,
    5:  38.5,
    6:  48.0,   # End of powerplay
    7:  55.5,
    8:  63.5,
    9:  72.0,
    10: 81.0,
    11: 90.0,
    12: 99.0,
    13: 108.0,
    14: 116.5,
    15: 125.5,
    16: 135.5,
    17: 146.5,
    18: 157.5,
    19: 167.5,
    20: 176.0,
}

_T20_PITCH_PAR_FACTORS: Dict[str, float] = {
    "Green": 0.84,
    "Dry":   0.86,
    "Hard":  1.00,
    "Flat":  1.10,
    "Dead":  1.25,
}

_T20 = FormatConfig(
    name="T20",
    overs=20,
    max_bowler_overs=4,
    allow_consecutive_overs=False,
    powerplay_phases=[
        Phase("Powerplay", start=0, end=5, max_fielders_outside=2),
    ],
    middle_phase=Phase("Middle", start=6, end=15, max_fielders_outside=4),
    death_phase=Phase("Death", start=16, end=19, max_fielders_outside=5),
    par_scores=_T20_PAR_SCORES,
    pitch_par_factors=_T20_PITCH_PAR_FACTORS,
    expected_rr={
        "Powerplay": 7.5,
        "Middle":    8.0,
        "Death":    10.5,
    },
    extras_per_innings=5,
    target_scores={
        "Green": 145,
        "Dry":   145,
        "Hard":  165,
        "Flat":  185,
        "Dead":  210,
    },
    correct_toss_choice={
        "Green": "bowl",  # Seam/swing → bowl first
        "Dry":   "bat",   # Spin worsens with wear → bat first
        "Hard":  "bat",   # Good batting surface → bat first
        "Flat":  "bowl",  # Run-fest; dew helps chaser → bowl first
        "Dead":  "bowl",  # Extreme batting; chaser advantaged → bowl first
    },
    correct_toss_choice_dn={
        # D/N T20: shorter game but dew still tilts towards chasing.
        # Bowling first = your team bats second under lights with dew.
        "Green": "bowl",   # Already bowl; unchanged
        "Dry":   "bowl",   # Dew neutralises spin in 2nd innings → bowl first
        "Hard":  "bowl",   # Chase with dew advantage → bowl first
        "Flat":  "bowl",   # Already bowl; unchanged
        "Dead":  "bowl",   # Already bowl; unchanged
    },
    rrr_baseline={
        # "Neutral" RPO for each pitch in T20 context.
        # GSME divides actual RRR by this to get a normalised aggression index.
        "Green": 7.5,
        "Dry":   7.5,
        "Hard":  8.5,
        "Flat":  10.5,
        "Dead":  11.5,
    },
)


# ---------------------------------------------------------------------------
# List A (50-over) FormatConfig
# ---------------------------------------------------------------------------

# Par scores reflect a neutral (Hard) pitch first-innings average of ~290.
# Phase breakdown:
#   PP1  (overs  0- 9): ~62 runs  (6.2 RPO)
#   Middle (overs 10-39): ~126 runs  (5.0 RPO)  [62 → 188]
#   Death (overs 40-49): ~102 runs  (8.5 RPO)  [188 → 290]
_LISTA_PAR_SCORES: Dict[int, float] = {
    0:   0.0,
    1:   6.0,
    2:  12.5,
    3:  19.5,
    4:  26.5,
    5:  34.0,
    6:  40.5,
    7:  47.5,
    8:  54.5,
    9:  58.5,
    10: 63.0,   # End of PP1
    11: 68.0,
    12: 73.0,
    13: 78.0,
    14: 83.0,
    15: 88.0,
    16: 93.0,
    17: 98.0,
    18: 103.0,
    19: 108.0,
    20: 113.0,
    21: 118.0,
    22: 123.0,
    23: 128.0,
    24: 133.0,
    25: 138.0,
    26: 143.0,
    27: 148.0,
    28: 153.0,
    29: 158.0,
    30: 163.0,
    31: 168.0,
    32: 173.0,
    33: 178.0,
    34: 183.0,
    35: 188.0,  # End of consolidation phase
    36: 193.5,
    37: 199.5,
    38: 206.0,
    39: 213.0,
    40: 220.0,  # Death begins — acceleration
    41: 228.5,
    42: 237.5,
    43: 246.5,
    44: 255.5,
    45: 264.5,
    46: 273.5,
    47: 280.5,
    48: 285.5,
    49: 288.0,
    50: 290.0,
}

_LISTA_PITCH_PAR_FACTORS: Dict[str, float] = {
    "Green": 0.76,   # ~220 expected
    "Dry":   0.80,   # ~232 expected
    "Hard":  1.00,   # ~290 expected (baseline)
    "Flat":  1.10,   # ~319 expected
    "Dead":  1.18,   # ~342 expected
}

_LISTA = FormatConfig(
    name="ListA",
    overs=50,
    max_bowler_overs=10,
    allow_consecutive_overs=False,   # ← KEY RULE: no back-to-back overs
    powerplay_phases=[
        # PP1: mandatory fielding restriction — only 2 outside 30-yard circle
        Phase("PP1", start=0, end=9, max_fielders_outside=2),
    ],
    # PP2/middle (overs 10-39): 4 fielders permitted outside
    middle_phase=Phase("Middle", start=10, end=39, max_fielders_outside=4),
    # Death/slog (overs 40-49): 5 fielders permitted outside
    death_phase=Phase("Death", start=40, end=49, max_fielders_outside=5),
    par_scores=_LISTA_PAR_SCORES,
    pitch_par_factors=_LISTA_PITCH_PAR_FACTORS,
    expected_rr={
        "PP1":    5.8,   # New ball, attacking but measured
        "Middle": 4.8,   # Consolidation, spin, dot-ball pressure
        "Death":  8.5,   # Slog overs — maximum aggression
    },
    extras_per_innings=12,   # More deliveries → proportionally more extras
    target_scores={
        "Green": 220,
        "Dry":   230,
        "Hard":  285,
        "Flat":  320,
        "Dead":  340,
    },
    correct_toss_choice={
        # ListA day-match toss logic (pitch wear only, no dew)
        "Green": "bowl",   # New-ball seam threat; pitch stays decent all day
        "Dry":   "bat",    # Pitch deteriorates; spin brutal in 2nd innings
        "Hard":  "bowl",   # Balanced; slight chase advantage
        "Flat":  "bowl",   # High totals still chaseable
        "Dead":  "bat",    # Set a huge total; spinners can do nothing anyway
    },
    correct_toss_choice_dn={
        # D/N ListA: dew from over 25 of the 2nd innings tips all pitches
        # towards bowling first.  Your team bats 2nd at night with dew:
        #   - Spin grips less (Dry advantage lost)
        #   - Ball becomes slippery (more wides/extras)
        #   - Outfield faster from moisture (Four chance ↑)
        "Green": "bowl",   # Already bowl; dew makes chase even easier
        "Dry":   "bowl",   # Overrides day "bat" — dew kills spin in overs 25-50
        "Hard":  "bowl",   # Already bowl; unchanged
        "Flat":  "bowl",   # Already bowl; unchanged
        "Dead":  "bowl",   # Overrides day "bat" — batting paradise + dew = huge chase
    },
    rrr_baseline={
        # "Neutral" RPO for each pitch in ListA (ODI) context.
        # ODI scoring rates are significantly lower than T20; Hard pitch
        # averages ~5.7-6.0 RPO in the first innings.
        # A required rate above these baselines represents escalating pressure.
        "Green": 4.8,   # Seam-friendly: low-scoring; 5+ RPO is already urgent
        "Dry":   5.0,   # Spin-friendly: modest target, 5+ RPO is challenging
        "Hard":  6.0,   # Balanced baseline for ODI cricket
        "Flat":  7.0,   # High-scoring; 7+ RPO still challenging even on flat deck
        "Dead":  7.5,   # Batting paradise; 8+ RPO is genuinely hard to sustain
    },
)


# ---------------------------------------------------------------------------
# Public registry — look up by match_format string
# ---------------------------------------------------------------------------

FORMAT_REGISTRY: Dict[str, FormatConfig] = {
    "T20":   _T20,
    "ListA": _LISTA,
}

# Independent preset, sharing only the delivery model with T20.
_T10 = copy.deepcopy(_T20)
_T10.name = "T10"
_T10.scheduled_overs = 10
_T10.strict_short_bowling = True
_T10.momentum_window = 12
_T10.dot_thresholds = (2, 3, 5, 6)
_T10.partnership_thresholds = (15, 30, 45, 60)
# Par shape measured from the engine, not assumed: the powerplay is the
# highest-scoring phase of a T10, the middle overs dip, and the death is the
# spike. par_scores/pitch_par_factors/rrr_baseline below are all DERIVED from
# these two dicts, so they must move whenever the scoring matrices move —
# otherwise every first innings reads as ahead of par (and bats conservatively)
# while every chase sits permanently above required_aggression 1.0.
_T10.expected_rr = {"Powerplay": 12.0, "Middle": 11.0, "Death": 15.0}
_T10.revise_short_innings(10)
_T10.par_scores = {0: 0.0}
for _over in range(10):
    _T10.par_scores[_over + 1] = _T10.par_scores[_over] + _T10.expected_rr[_T10.phase_key(_over)]
_T10.target_scores = {"Green": 95, "Dry": 100, "Hard": 125, "Flat": 137, "Dead": 155}
_T10.pitch_par_factors = {pitch: total / _T10.par_scores[10] for pitch, total in _T10.target_scores.items()}
_T10.rrr_baseline = {pitch: total / 10 for pitch, total in _T10.target_scores.items()}
_T10.extras_per_innings = 3
FORMAT_REGISTRY["T10"] = _T10


def resolve_scheduled_overs(match_format, scheduled_overs=None):
    """Validate immutable match length, independently of rain-revised overs."""
    default = default_scheduled_overs(match_format)
    if scheduled_overs is None or scheduled_overs == "":
        return default
    allowed = FORMAT_CATALOG.get(match_format, {}).get("lengths", ())
    if isinstance(scheduled_overs, bool) or str(scheduled_overs) not in {str(n) for n in allowed}:
        raise ValueError("Invalid scheduled overs for this format")
    return int(scheduled_overs)


def format_label(match_format, scheduled_overs=None):
    if match_format == "ListA":
        return f"List A · {resolve_scheduled_overs(match_format, scheduled_overs)} overs"
    return "First-Class" if match_format == "FC" else match_format


def get_format(match_format: Optional[str], scheduled_overs=None) -> FormatConfig:
    """
    Return the FormatConfig for the given match_format string.
    Defaults to T20 for None or unrecognised values (backward compat).
    """
    base = FORMAT_REGISTRY.get(match_format or "T20", FORMAT_REGISTRY["T20"])
    fmt = copy.deepcopy(base)
    length = resolve_scheduled_overs(base.name, scheduled_overs)
    fmt.scheduled_overs = length
    if base.name == "ListA" and length == 40:
        fmt.overs = 40
        fmt.max_bowler_overs = 8
        fmt.powerplay_phases = [Phase("PP1", 0, 7, 2)]
        fmt.middle_phase = Phase("Middle", 8, 31, 4)
        fmt.death_phase = Phase("Death", 32, 39, 5)
        fmt.par_scores = {}
        for over in range(41):
            at = over * 1.25
            lo, hi = math.floor(at), math.ceil(at)
            fmt.par_scores[over] = 0.8 * (base.par_scores[lo] +
                (base.par_scores[hi] - base.par_scores[lo]) * (at - lo))
        fmt.target_scores = {pitch: round(total * 0.8) for pitch, total in base.target_scores.items()}
        fmt.extras_per_innings = round(base.extras_per_innings * 0.8)
    return fmt


# ---------------------------------------------------------------------------
# MultiDayFormatConfig — First-Class (FC): 4/5-day, up to 2 innings per side
# ---------------------------------------------------------------------------
#
# This is a deliberately SEPARATE dataclass from FormatConfig, not more
# fields bolted onto it. Every FormatConfig field above is shaped around one
# fixed-length innings with a bowler-over quota and fielding-circle phases —
# none of that maps onto FC (no fielding circles at all, no fixed innings
# length, no single target — FC has a lead/deficit dynamic across up to 4
# innings instead). Engine code should branch on `fmt.format_family`
# ("limited_overs" vs "multi_day"), never on `match_format == "FC"` strings
# scattered through call sites.

@dataclass
class MultiDayFormatConfig:
    """
    Parameterisation of a multi-day (First-Class) cricket format.

    Attributes
    ----------
    name                 : canonical format name ("FC")
    format_family        : always "multi_day" for this class
    days                 : match length in days (4 or 5), set per-match
    overs_per_day        : scheduled overs/day (Phase 1: fixed constant, 90)
    new_ball_overs       : overs before the 2nd new ball is available
                            (Phase 2 — ball-condition modeling; not
                            load-bearing in Phase 1)
    follow_on_margin     : runs behind required for the follow-on to be
                            enforceable; set per-match (150 for 4-day
                            matches, 200 for 5-day, per MCC Law 14.1)
    min_overs_last_hour  : minimum overs in the last hour of a day
                            (Phase 2 — over-rate enforcement)
    min_overs_per_day    : overs that MUST be bowled in a full day, before
                            weather and innings-change deductions. None
                            means "the same as overs_per_day" — they are the
                            same number in every real playing condition, and
                            tying them stops a per-match overs_per_day
                            override silently leaving the minimum at 90
    innings_change_over_deduction : overs taken off the day's minimum for
                            each innings that STARTS during the day,
                            standing in for the ten minutes between
                            innings. The changeover deliberately costs no
                            clock time here — this deduction is the model
    max_overtime_minutes : extra playing time allowed past the scheduled
                            close to complete the day's minimum. Entirely
                            separate from
                            fc_weather.MAX_SAME_DAY_EXTENSION_MINUTES, which
                            buys back time lost to weather; one day can use
                            both. 0 disables overtime
    allow_consecutive_overs : MCC Law 17.2 — universal, not a limited-overs
                            convention, so it carries over (always False)
    free_hit_after_no_ball : always False — the free hit is a limited-overs
                            provision and the FC playing conditions have no
                            equivalent (the no-ball itself is unchanged)
    pitch_par_factors    : per-pitch-type multiplier used to scale the
                            declaration run-thresholds (see fc_declaration.py)
                            so "a good total" means something different on a
                            Green seamer than a Dead belter
    correct_toss_choice  : {pitch_type: "bat"|"bowl"} optimal toss decision
    correct_toss_choice_dn : D/N override (FC day/night matches are rare;
                            defaults to None, falling back to
                            correct_toss_choice)
    """
    name: str = "FC"
    format_family: str = "multi_day"
    days: int = 4
    overs_per_day: int = 90
    new_ball_overs: int = 80
    follow_on_margin: int = 150
    min_overs_last_hour: int = 15
    # The day's over obligation. The clock decides when play stops; these
    # decide whether it may. See Match._fc_minimum_overs_today().
    min_overs_per_day: Optional[int] = None
    innings_change_over_deduction: int = 2
    max_overtime_minutes: int = 30
    allow_consecutive_overs: bool = False
    # No free hit in first-class cricket — see FormatConfig's matching field.
    free_hit_after_no_ball: bool = False
    # "What counts as a good total here", scaling fc_declaration's
    # _INNINGS1_BASE_THRESHOLD / _LEAD_BASE_THRESHOLD. For FC this is the
    # ONLY consumer — GSME (the other user of pitch_par_factors) is off for
    # multi-day, see Match._fc_next_ball's _gsme_state.
    #
    # These are a real ceiling on first-innings totals, not just flavour:
    # once a surface's scoring outgrows its declaration bar, tuning the
    # scoring matrix does nothing to the average total — sides simply reach
    # the same bar with fewer wickets down and in more overs. Flat was
    # raised 1.15 -> 1.22 for exactly that reason.
    pitch_par_factors: Dict[str, float] = field(default_factory=lambda: {
        "Green": 0.85,
        "Dry":   0.90,
        "Hard":  1.00,
        "Flat":  1.22,
        "Dead":  1.30,
    })
    correct_toss_choice: Dict[str, str] = field(default_factory=lambda: {
        "Green": "bowl",  # Seam/swing on a fresh pitch → bowl first
        "Dry":   "bat",   # Wears toward spin over 4-5 days → bat first
        "Hard":  "bat",   # True surface, minimal wear → bat first
        "Flat":  "bat",   # Batting paradise, sets a big total → bat first
        "Dead":  "bat",   # Same, even more so
    })
    correct_toss_choice_dn: Optional[Dict[str, str]] = None


_FC_BASE = MultiDayFormatConfig()

MULTIDAY_FORMAT_REGISTRY: Dict[str, MultiDayFormatConfig] = {
    "FC": _FC_BASE,
}


def get_any_format(match_format: Optional[str], **overrides) -> Union[FormatConfig, MultiDayFormatConfig]:
    """
    Single dispatcher across both format families. Every other call site
    should branch on the returned object's `.format_family`, never on the
    `match_format` string itself.

    Parameters
    ----------
    match_format : "T20" | "ListA" | "FC" | None
    overrides    : `scheduled_overs` (40/50 for ListA, 20 for T20); for "FC":
                   `days` (int, 4 or 5) and `overs_per_day` (int, Phase 2 —
                   defaults to the format's standard 90 if omitted).

    Returns a fresh per-instance deep copy so per-match
    mutation (e.g. rain revisions, per-match `days`) never touches the
    shared registry singleton — the same discipline `get_format()` already
    relies on for T20/ListA.
    """
    if match_format in FORMAT_REGISTRY:
        return get_format(match_format, overrides.get("scheduled_overs"))

    if match_format in MULTIDAY_FORMAT_REGISTRY:
        resolve_scheduled_overs(match_format, overrides.get("scheduled_overs"))
        base = copy.deepcopy(MULTIDAY_FORMAT_REGISTRY[match_format])
        days = overrides.get("days") or base.days
        overs_per_day = overrides.get("overs_per_day") or base.overs_per_day
        return dataclasses.replace(
            base,
            days=days,
            overs_per_day=overs_per_day,
            follow_on_margin=200 if days >= 5 else 150,
        )

    return get_format("T20", overrides.get("scheduled_overs"))
