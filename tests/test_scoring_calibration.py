"""
Scoring Calibration Harness (Phase 0)
=====================================

The gate for all batting/bowling shape work. Three layers, each with a
different job:

1. REGRESSION BANDS  — full-match par scores per pitch x format, pinned to
   the engine's behaviour as of this commit. Seeded and fully deterministic
   (verified: identical output across repeated runs), so these bands are
   tight on purpose. They are NOT a claim that current behaviour is correct
   (see KNOWN_CALIBRATION_DRIFT below) — they exist so an unintended shift
   is loud.

2. SEMANTIC INVARIANTS — orderings that must hold under ANY tuning: a road
   outscores a green top, dot% runs the other way, etc. These survive
   recalibration and are the tests worth trusting long-term.

3. SHAPE SPECS (xfail strict) — the executable specification for the bug
   this harness was built to fix. They isolate `compute_weighted_prob` so
   the measurement is not confounded by phase, GSME or batting position.
   They fail today. When the shape layer lands they will pass, and
   strict=True turns an unexpected pass into a failure, forcing the marker
   to be removed rather than silently rotting.


Why the shape specs are isolated rather than measured from full matches
----------------------------------------------------------------------
Measuring strike rate by rating tier over full matches is confounded. In
T20 the elite tier shows SR 179.5 vs the tail's 84.7 — a +94.8 gap that
looks like ratings working. It isn't: it is GSME resource-conservatism,
death-over boosts and the new-batter penalty tracking *match state* that
correlates with rating. ListA, where 300 balls wash those confounds out,
shows the truth — a +2.0 gap, with the 70-79 tier actually OUTSCORING the
80-86 tier because it bats in the death phase. Rating is invisible; batting
position is everything.

So tier SR is reported (test_report_calibration_table) but never asserted.
The assertions live on the isolated probe.


T20 PITCH RECALIBRATION (2026-08-16)
-----------------------------------
The drift recorded here previously (Flat 227.9 mean, median 234, p75 257 —
routine 260s) has been closed. T20 pitch profiles were retuned to the bands
in engine/ball_outcome.py, and every T20 pitch now sits inside its band:

    pitch   band                  measured (16 seeds)
    Green   110-150 / 7-10 wkts   123.3 / 8.3
    Dry     110-150 / 7-10 wkts   133.2 / 7.9
    Hard    180-220 / 5-7  wkts   184.9 / 5.0
    Flat    200-230 / 3-5  wkts   212.5 / 3.5
    Dead    230+    / 1-2  wkts   249.1 / 1.8

Three levers moved, all in config/ground_conditions_defaults.yaml:
  1. Per-pitch scoring_matrix — the base shape, and the dominant lever.
     Note run_factor is NOT a run-rate control in T20: it multiplies all six
     run outcomes equally, so after normalisation it only trades run-mass
     against Wicket/Extras. It is a wicket suppressor. (Same trap as ListA —
     see the ListA run_factor note.)
  2. phase_boosts.death_overs.boundary_boost_batting_pitch 2.2 -> 1.90.
  3. game_modes.flat_track_bully four/six/dot multipliers softened. This mode
     is auto-pinned on Flat/Dead while `wickets < 3 and over < 10`, so its
     wicket suppression feeds its own trigger; at 1.22/1.35 the loop was
     self-sustaining for the entire first 10 overs.

Verified against 120 independent seeds (medians): Green 138, Dry 143,
Hard 186, Flat 218, Dead 253 — all in band, with Flat's p90 down from
265 to 243.


LISTA PITCH RECALIBRATION (2026-08-16)
-------------------------------------
Recalibrated to ODI-scale bands in the same pass. Two structural problems, not
just drift:

  1. Green and Dry were the same pitch as far as WHO took wickets went. ListA
     had no per-bowling-style wicket factors — only the scalar wicket_mult — so
     both surfaces returned ~67% pace, which is merely the share of overs pace
     bowled. engine/ground_config.get_lista_wicket_factors() and the style term
     in calculate_outcome's ListA branch are new; Green is now ~82% pace and
     Dry ~58% spin off ~35% of the overs. Guarded by
     test_green_is_a_seamers_pitch_and_dry_is_a_spinners.
  2. Dead landed within 6 runs of Flat (347.4 vs 341.6) on an identical 4.6
     wickets — two differently-named roads that played the same. Guarded by
     test_dead_is_clearly_distinct_from_flat.

    pitch   band                  measured (16 seeds)
    Green   200-240 / 7-10 wkts   235.2 / 9.2
    Dry     200-240 / 7-10 wkts   210.5 / 10.0
    Hard    280-320 / 6-8  wkts   296.1 / 7.9
    Flat    320-360 / 4-6  wkts   341.6 / 4.6
    Dead    360+    / 2-4  wkts   362.4 / 3.4

ListA `run_factor` has the same trap as T20's: it scales all six run outcomes
equally, so it only trades run-mass against Wicket/Extras. And over 300 balls
run accumulation is single-driven — cutting Four/Six on Green/Dry moved the
median by ~2 runs, because innings LENGTH (how long the side survives)
dominates. Reach for wicket_mult/wicket_factors to move ListA totals.

Two things to know before touching ListA wicket_factors:
  • They REDISTRIBUTE wickets by style; they must not cut the total. Because
    most attacks are pace-heavy (commonly 5 seamers to 2 spinners), a punitive
    Dry `default` suppresses most of the ATTACK rather than shifting wickets,
    and sides then bat out all 300 balls on a turner. Size wicket_mult so
    wicket_mult x the weighted-average factor stays near the pre-split rate.
  • Totals become genuinely squad-sensitive, which is intended: a 5-seam attack
    concedes ~266 on Dry while a balanced one concedes ~210. test_lista_format
    exercises the pace-heavy squad and carries deliberately coarse bands; the
    bands here are the authoritative ones.

REPIN NOTE (2026-08-09): T20_BASELINE/LISTA_BASELINE below were re-measured
after the fielding-mechanic change (individual fielder rating now drives
catch-drop/misfield odds instead of the team average — see engine/ball_outcome.py
_select_fielder). That change adds new random.choices() calls into the per-ball
path (a fielder is now picked on every dot/single, and earlier on every
Caught/Stumped chance), which reorders the seeded RNG sequence for the rest of
each match. Confirmed via isolation test that this is pure RNG-sequence drift,
not a probability-model bias: the calibration squad uses a flat
fielding_rating=70 for all 11 players, so individual-vs-team-average quality is
numerically identical, yet the old baseline is only reproduced by stripping the
new fielding_team argument back out.
"""

import statistics

import pytest

import engine.match as match_module
from engine.ball_outcome import compute_weighted_prob, PITCH_SCORING_MATRIX

SEEDS = [4101, 4102, 4103, 4104, 4105, 4106, 4107, 4108,
         4109, 4110, 4111, 4112, 4113, 4114, 4115, 4116]

PITCHES = ("Green", "Dry", "Hard", "Flat", "Dead")

RUN_OUTCOMES = ("Dot", "Single", "Double", "Three", "Four", "Six")
ALL_OUTCOMES = RUN_OUTCOMES + ("Wicket", "Extras")

# Squad mirrors production rating distribution:
# Batsman avg 78, Wicketkeeper 79, All-rounder 75, Bowler 32.
SQUAD = [
    (86, 20, "Batsman",      "Medium",      False),
    (84, 25, "Batsman",      "Medium",      False),
    (82, 18, "Batsman",      "Medium",      False),
    (80, 30, "Wicketkeeper", "Medium",      False),
    (78, 45, "Batsman",      "Medium",      False),
    (75, 74, "All-rounder",  "Medium-fast", True),
    (70, 78, "All-rounder",  "Off spin",    True),
    (55, 82, "Bowler",       "Fast-medium", True),
    (38, 86, "Bowler",       "Fast",        True),
    (30, 84, "Bowler",       "Leg spin",    True),
    (25, 80, "Bowler",       "Fast-medium", False),
]

# Pinned to engine behaviour at Phase 0, re-pinned after the fielder-first
# catch-drop/misfield change (see REPIN NOTE above), and again after the
# 2026-08-16 T20 pitch recalibration (see T20 PITCH RECALIBRATION above).
# (mean_runs, mean_wickets, dot_pct, bdry_per_100)
T20_BASELINE = {
    "Green": (123.3, 8.3, 41.7, 11.9),
    "Dry":   (130.6, 8.2, 42.1, 13.2),
    "Hard":  (184.9, 5.0, 34.0, 20.1),
    "Flat":  (212.5, 3.5, 28.6, 23.8),
    "Dead":  (249.1, 1.8, 25.1, 30.8),
}

# Re-pinned after the 2026-08-16 ListA recalibration (see LISTA PITCH
# RECALIBRATION below).
LISTA_BASELINE = {
    "Green": (235.2, 9.2, 46.1, 4.3),
    "Dry":   (210.5, 10.0, 45.5, 4.7),
    "Hard":  (296.1, 7.9, 38.5, 6.9),
    "Flat":  (341.6, 4.6, 35.2, 9.4),
    "Dead":  (362.4, 3.4, 32.4, 10.3),
}

RUN_TOLERANCE = 0.05      # +/-5% on mean runs
DOT_TOLERANCE = 2.0       # +/-2 percentage points
BDRY_TOLERANCE = 2.0      # +/-2 boundaries per 100 balls
WKT_TOLERANCE = 0.8       # +/-0.8 wickets


# ---------------------------------------------------------------------------
# Match simulation
# ---------------------------------------------------------------------------

def _squad(prefix):
    return [{
        "name": f"{prefix}_P{i+1}", "role": role,
        "batting_rating": bat, "bowling_rating": bowl, "fielding_rating": 70,
        "batting_hand": "Right" if i % 3 else "Left",
        "bowling_type": btype, "bowling_hand": "Right" if i % 2 else "Left",
        "will_bowl": will_bowl, "is_captain": i == 0,
    } for i, (bat, bowl, role, btype, will_bowl) in enumerate(SQUAD)]


def _match_data(fmt, pitch, seed):
    import random
    random.seed(seed)
    return {
        "match_id": f"cal_{fmt}_{pitch}_{seed}", "created_by": "calibration",
        "team_home": "HOM_cal", "team_away": "AWY_cal", "stadium": "Cal Ground",
        "pitch": pitch, "toss": "Heads", "toss_winner": "HOM", "toss_decision": "Bat",
        "simulation_mode": "auto", "match_format": fmt,
        "playing_xi": {"home": _squad("H"), "away": _squad("A")},
        "substitutes": {"home": [], "away": []}, "is_day_night": False,
    }


def _overs_to_balls(overs):
    v = str(overs)
    if "." in v:
        whole, balls = v.split(".", 1)
        return int(whole) * 6 + int(balls)
    return int(v) * 6


def _simulate_first_innings(fmt, pitch, seed, budget=3000):
    data = _match_data(fmt, pitch, seed)
    rating_of = {p["name"]: p["batting_rating"]
                 for side in data["playing_xi"].values() for p in side}
    match = match_module.Match(data)

    legal = dots = 0
    per_rating = {}
    for _ in range(budget):
        resp = match.next_ball()
        bd = resp.get("ball_data") or {}
        extra_type, is_extra = bd.get("extra_type"), bool(bd.get("is_extra"))

        # Wides and no-balls are not deliveries the batter faced.
        faced = not (is_extra and extra_type in ("Wide", "No Ball"))
        # Byes/leg-byes are faced, but the runs are not the batter's.
        bat_runs = 0 if is_extra else (bd.get("runs") or 0)

        if faced:
            legal += 1
            if bat_runs == 0:
                dots += 1
            slot = per_rating.setdefault(rating_of.get(bd.get("striker"), 0), [0, 0])
            slot[0] += 1
            slot[1] += bat_runs

        if resp.get("innings_end") and resp.get("innings_number") == 1:
            sc = resp.get("scorecard_data", {})
            players = sc.get("players", [])
            return {
                "runs": match.first_innings_score,
                "wkts": sc.get("wickets", 0),
                "balls": _overs_to_balls(sc.get("overs", "0.0")),
                "fours": sum(p.get("fours") or 0 for p in players),
                "sixes": sum(p.get("sixes") or 0 for p in players),
                "dots": dots, "legal": legal, "per_rating": per_rating,
            }
    raise AssertionError(f"{fmt}/{pitch}/{seed}: innings did not end within {budget} balls")


def _aggregate(fmt, pitch):
    rows = [_simulate_first_innings(fmt, pitch, s) for s in SEEDS]
    legal = max(sum(r["legal"] for r in rows), 1)
    return {
        "runs": statistics.mean(r["runs"] for r in rows),
        "wkts": statistics.mean(r["wkts"] for r in rows),
        "dot_pct": sum(r["dots"] for r in rows) / legal * 100,
        "bdry_per_100": sum(r["fours"] + r["sixes"] for r in rows) / legal * 100,
        "rows": rows,
    }


@pytest.fixture(autouse=True)
def _mute_match_print(monkeypatch):
    monkeypatch.setattr(match_module, "print", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# 1. Regression bands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pitch", PITCHES)
def test_t20_par_regression_band(pitch):
    exp_runs, exp_wkts, exp_dot, exp_bdry = T20_BASELINE[pitch]
    got = _aggregate("T20", pitch)

    assert abs(got["runs"] - exp_runs) <= exp_runs * RUN_TOLERANCE, (
        f"T20/{pitch} mean runs moved: {got['runs']:.1f} vs pinned {exp_runs} "
        f"(+/-{exp_runs * RUN_TOLERANCE:.1f})"
    )
    assert abs(got["wkts"] - exp_wkts) <= WKT_TOLERANCE, (
        f"T20/{pitch} mean wickets moved: {got['wkts']:.1f} vs pinned {exp_wkts}"
    )
    assert abs(got["dot_pct"] - exp_dot) <= DOT_TOLERANCE, (
        f"T20/{pitch} dot% moved: {got['dot_pct']:.1f} vs pinned {exp_dot}"
    )
    assert abs(got["bdry_per_100"] - exp_bdry) <= BDRY_TOLERANCE, (
        f"T20/{pitch} boundaries/100b moved: {got['bdry_per_100']:.1f} vs pinned {exp_bdry}"
    )


@pytest.mark.parametrize("pitch", PITCHES)
def test_lista_par_regression_band(pitch):
    exp_runs, exp_wkts, exp_dot, exp_bdry = LISTA_BASELINE[pitch]
    got = _aggregate("ListA", pitch)

    assert abs(got["runs"] - exp_runs) <= exp_runs * RUN_TOLERANCE, (
        f"ListA/{pitch} mean runs moved: {got['runs']:.1f} vs pinned {exp_runs}"
    )
    assert abs(got["wkts"] - exp_wkts) <= WKT_TOLERANCE, (
        f"ListA/{pitch} mean wickets moved: {got['wkts']:.1f} vs pinned {exp_wkts}"
    )
    assert abs(got["dot_pct"] - exp_dot) <= DOT_TOLERANCE, (
        f"ListA/{pitch} dot% moved: {got['dot_pct']:.1f} vs pinned {exp_dot}"
    )
    assert abs(got["bdry_per_100"] - exp_bdry) <= BDRY_TOLERANCE, (
        f"ListA/{pitch} boundaries/100b moved: {got['bdry_per_100']:.1f} vs pinned {exp_bdry}"
    )


# ---------------------------------------------------------------------------
# 1b. Target bands — the pitch spec, as an executable assertion
# ---------------------------------------------------------------------------
# Unlike T20_BASELINE (which pins whatever the engine does today so drift is
# loud), these are the bands the engine is SUPPOSED to hit. They are wider and
# survive re-pinning: a future tuning pass may move the baseline, but moving
# outside these bands means the pitch no longer means what it says it means.
#
# (runs_lo, runs_hi, wkts_lo, wkts_hi). runs_hi is None for an open-ended band.
T20_TARGET_BANDS = {
    "Green": (110, 150, 7.0, 10.0),
    "Dry":   (110, 150, 7.0, 10.0),
    "Hard":  (180, 220, 5.0, 7.0),
    "Flat":  (200, 230, 3.0, 5.0),
    "Dead":  (230, None, 1.0, 2.5),
}

# ListA bands are ODI-scale: the same pitch character over 300 balls, not a
# scaled copy of the T20 numbers.
LISTA_TARGET_BANDS = {
    "Green": (200, 240, 7.0, 10.0),
    "Dry":   (200, 240, 7.0, 10.0),
    "Hard":  (280, 320, 6.0, 8.0),
    "Flat":  (320, 360, 4.0, 6.0),
    "Dead":  (360, None, 2.0, 4.0),
}

TARGET_BANDS = {"T20": T20_TARGET_BANDS, "ListA": LISTA_TARGET_BANDS}


@pytest.mark.parametrize("fmt", ("T20", "ListA"))
@pytest.mark.parametrize("pitch", PITCHES)
def test_pitch_sits_in_its_target_band(fmt, pitch):
    lo, hi, wlo, whi = TARGET_BANDS[fmt][pitch]
    got = _aggregate(fmt, pitch)

    assert got["runs"] >= lo, (
        f"{fmt}/{pitch}: mean {got['runs']:.1f} is below the band floor {lo}"
    )
    if hi is not None:
        assert got["runs"] <= hi, (
            f"{fmt}/{pitch}: mean {got['runs']:.1f} is above the band ceiling {hi}"
        )
    assert wlo <= got["wkts"] <= whi, (
        f"{fmt}/{pitch}: mean wickets {got['wkts']:.1f} outside {wlo}-{whi}"
    )


def test_dead_is_clearly_distinct_from_flat():
    """The two roads must not collapse into the same pitch.

    ListA Dead used to land within 6 runs of Flat (347.4 vs 341.6) on an
    identical 4.6 wickets — two differently-named surfaces that played the same.
    """
    for fmt, margin in (("T20", 15), ("ListA", 12)):
        flat, dead = _aggregate(fmt, "Flat"), _aggregate(fmt, "Dead")
        assert dead["runs"] - flat["runs"] >= margin, (
            f"{fmt}: Dead ({dead['runs']:.1f}) must outscore Flat "
            f"({flat['runs']:.1f}) by at least {margin}"
        )
        assert flat["wkts"] - dead["wkts"] >= 0.5, (
            f"{fmt}: Dead ({dead['wkts']:.1f} wkts) must yield clearly fewer "
            f"wickets than Flat ({flat['wkts']:.1f})"
        )


# ---------------------------------------------------------------------------
# 1c. Pitch character shows up in WHO takes the wickets
# ---------------------------------------------------------------------------

_PACE = ("Fast", "Fast-medium", "Medium-fast", "Medium")
_SPIN = ("Off spin", "Leg spin", "Finger spin", "Wrist spin")


def _strike_rates(fmt, pitch):
    """(pace, spin) wickets per 100 balls bowled by that style, over all SEEDS.

    Raw wicket SHARE is the wrong measure here: the calibration squad carries
    only two spinners against four seamers, so share mostly reports who bowled
    the overs. Wickets per ball is what "this pitch suits spin" actually means.
    """
    import engine.match as match_module

    wkts = {"pace": 0, "spin": 0}
    balls = {"pace": 0, "spin": 0}
    for seed in SEEDS:
        data = _match_data(fmt, pitch, seed)
        type_of = {p["name"]: p["bowling_type"]
                   for side in data["playing_xi"].values() for p in side}
        match = match_module.Match(data)
        for _ in range(6000):
            resp = match.next_ball()
            bd = resp.get("ball_data") or {}
            bt = type_of.get(bd.get("bowler"), "")
            key = "pace" if bt in _PACE else "spin" if bt in _SPIN else None
            if key:
                balls[key] += 1
                wt = bd.get("wicket_type")
                if wt and wt != "Run Out":
                    wkts[key] += 1
            if resp.get("innings_end") and resp.get("innings_number") == 1:
                break
    return (100 * wkts["pace"] / max(balls["pace"], 1),
            100 * wkts["spin"] / max(balls["spin"], 1))


@pytest.mark.parametrize("fmt", ("T20", "ListA"))
def test_green_is_a_seamers_pitch_and_dry_is_a_spinners(fmt):
    """Pitch type must decide WHO takes wickets, not just how many.

    ListA had no per-style wicket factors before 2026-08-16 — only a scalar
    wicket_mult — so Green and Dry both returned ~67% of wickets to pace,
    purely reflecting how many overs each attack bowled. The surface had no say
    at all in who profited from it.
    """
    green_pace, green_spin = _strike_rates(fmt, "Green")
    dry_pace, dry_spin = _strike_rates(fmt, "Dry")

    # Measured ratios when pinned: T20 Green 2.75, T20 Dry 1.80,
    # ListA Green 1.91, ListA Dry 2.82. 1.5 leaves room without being vacuous —
    # a neutral Flat track sits at 1.27, so this genuinely separates them.
    assert green_pace > green_spin * 1.5, (
        f"{fmt}/Green: seamers must strike far more often than spinners — got "
        f"{green_pace:.2f} vs {green_spin:.2f} wickets/100 balls"
    )
    assert dry_spin > dry_pace * 1.5, (
        f"{fmt}/Dry: spinners must strike far more often than seamers — got "
        f"{dry_spin:.2f} vs {dry_pace:.2f} wickets/100 balls"
    )


# ---------------------------------------------------------------------------
# 2. Semantic invariants — must hold under any tuning
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ("T20", "ListA"))
def test_batting_pitches_outscore_bowling_pitches(fmt):
    agg = {p: _aggregate(fmt, p) for p in PITCHES}
    for road in ("Flat", "Dead"):
        for green in ("Green", "Dry"):
            assert agg[road]["runs"] > agg[green]["runs"], (
                f"{fmt}: {road} ({agg[road]['runs']:.0f}) must outscore "
                f"{green} ({agg[green]['runs']:.0f})"
            )
        assert agg[road]["runs"] > agg["Hard"]["runs"], (
            f"{fmt}: {road} must outscore Hard"
        )


@pytest.mark.parametrize("fmt", ("T20", "ListA"))
def test_dot_and_boundary_rates_track_pitch_character(fmt):
    agg = {p: _aggregate(fmt, p) for p in PITCHES}
    for road in ("Flat", "Dead"):
        for green in ("Green", "Dry"):
            assert agg[road]["dot_pct"] < agg[green]["dot_pct"], (
                f"{fmt}: {road} dot% ({agg[road]['dot_pct']:.1f}) must be below "
                f"{green} ({agg[green]['dot_pct']:.1f})"
            )
            assert agg[road]["bdry_per_100"] > agg[green]["bdry_per_100"], (
                f"{fmt}: {road} boundary rate must exceed {green}"
            )


@pytest.mark.parametrize("fmt", ("T20", "ListA"))
def test_bowling_pitches_take_more_wickets(fmt):
    agg = {p: _aggregate(fmt, p) for p in PITCHES}
    for green in ("Green", "Dry"):
        for road in ("Flat", "Dead"):
            assert agg[green]["wkts"] > agg[road]["wkts"], (
                f"{fmt}: {green} ({agg[green]['wkts']:.1f} wkts) must take more "
                f"than {road} ({agg[road]['wkts']:.1f})"
            )


# ---------------------------------------------------------------------------
# 3. Shape specs — the bug, as an executable specification
# ---------------------------------------------------------------------------

def _isolated_profile(batting, bowling, pitch="Hard", batter_runs=0, balls_faced=10):
    """Normalised outcome distribution from one delivery's raw weights.

    Deliberately bypasses Match/GSME/phase boosts so the measurement reflects
    ONLY what batting and bowling rating do to the distribution.
    """
    matrix = PITCH_SCORING_MATRIX[pitch]
    weights = {
        o: compute_weighted_prob(
            o, matrix[o], batting, bowling, 60, pitch, "Fast",
            {"boundaries": 0}, batter_runs, balls_faced, None, None,
        )
        for o in ALL_OUTCOMES
    }
    total = sum(weights.values())
    return {o: w / total for o, w in weights.items()}


def _strike_rate(profile):
    return 100 * (profile["Single"] + 2 * profile["Double"] + 3 * profile["Three"]
                  + 4 * profile["Four"] + 6 * profile["Six"])


@pytest.mark.xfail(strict=True, reason=(
    "Phase 1 target: all six run outcomes share one blended_frac in "
    "compute_weighted_prob, so the run-block shape normalises to the base "
    "matrix for every rating. Remove this marker when the shape layer lands."
))
@pytest.mark.parametrize("pitch", ("Green", "Hard", "Flat"))
def test_batting_rating_shapes_the_run_distribution(pitch):
    elite = _isolated_profile(95, 70, pitch)
    tail = _isolated_profile(25, 70, pitch)

    elite_run_mass = sum(elite[o] for o in RUN_OUTCOMES)
    tail_run_mass = sum(tail[o] for o in RUN_OUTCOMES)
    elite_dot = elite["Dot"] / elite_run_mass
    tail_dot = tail["Dot"] / tail_run_mass
    elite_bdry = (elite["Four"] + elite["Six"]) / elite_run_mass
    tail_bdry = (tail["Four"] + tail["Six"]) / tail_run_mass

    assert elite_dot < tail_dot - 0.05, (
        f"{pitch}: a 95-rated batter must play materially fewer dots than a "
        f"25-rated one. Got {elite_dot:.4f} vs {tail_dot:.4f}"
    )
    assert elite_bdry > tail_bdry * 1.5, (
        f"{pitch}: a 95-rated batter must find far more boundaries. "
        f"Got {elite_bdry:.4f} vs {tail_bdry:.4f}"
    )


@pytest.mark.xfail(strict=True, reason=(
    "Phase 1 target: batting rating currently moves strike rate by ~2 points "
    "across the entire 25-95 range; the skill signal lands only on survival."
))
def test_batting_rating_produces_a_real_strike_rate_spread():
    elite = _strike_rate(_isolated_profile(95, 70))
    tail = _strike_rate(_isolated_profile(25, 70))
    assert elite - tail > 40, (
        f"Expected a wide SR spread between a 95- and 25-rated batter, "
        f"got {elite:.1f} vs {tail:.1f} (gap {elite - tail:.1f})"
    )


@pytest.mark.xfail(strict=True, reason=(
    "Phase 1 target: the graduated confidence curve (batter_runs >= 50 -> "
    "x1.20) feeds effective_batting, which cancels out of the run block. A "
    "set batter currently has the same scoring shape as one on nought."
))
def test_set_batter_accelerates():
    fresh = _strike_rate(_isolated_profile(85, 70, batter_runs=0, balls_faced=1))
    set_bat = _strike_rate(_isolated_profile(85, 70, batter_runs=85, balls_faced=50))
    assert set_bat - fresh > 10, (
        f"A batter on 85* should score faster than one on 0*. "
        f"Got {set_bat:.1f} vs {fresh:.1f} (gap {set_bat - fresh:.1f})"
    )


@pytest.mark.xfail(strict=True, reason=(
    "Phase 1 target (bowling mirror): bowling rating only sizes the wicket "
    "column. Every bowler concedes the same shape, so economy rate is not a "
    "real property and a containment bowler cannot be expressed."
))
def test_bowling_rating_shapes_what_is_conceded():
    elite = _isolated_profile(75, 95)
    weak = _isolated_profile(75, 35)

    elite_run_mass = sum(elite[o] for o in RUN_OUTCOMES)
    weak_run_mass = sum(weak[o] for o in RUN_OUTCOMES)
    elite_bdry = (elite["Four"] + elite["Six"]) / elite_run_mass
    weak_bdry = (weak["Four"] + weak["Six"]) / weak_run_mass
    elite_dot = elite["Dot"] / elite_run_mass
    weak_dot = weak["Dot"] / weak_run_mass

    assert elite_bdry < weak_bdry * 0.8, (
        f"A 95-rated bowler must concede materially fewer boundaries than a "
        f"35-rated one. Got {elite_bdry:.4f} vs {weak_bdry:.4f}"
    )
    assert elite_dot > weak_dot + 0.03, (
        f"A 95-rated bowler must build more dot-ball pressure. "
        f"Got {elite_dot:.4f} vs {weak_dot:.4f}"
    )


# ---------------------------------------------------------------------------
# Reporting — never asserts, run with `pytest -s` to read the table
# ---------------------------------------------------------------------------

def test_report_calibration_table():
    """Print the live calibration picture. Informational only.

    Tier strike rates are reported but NOT asserted — see the module
    docstring for why they are confounded by phase and batting position.
    """
    for fmt in ("T20", "ListA"):
        print(f"\n=== {fmt} first innings ({len(SEEDS)} seeds) ===")
        print(f"{'pitch':<7} {'runs':>8} {'wkts':>7} {'dot%':>7} {'bdry/100b':>11}")
        tiers = {"elite (80+)": [0, 0], "mid (70-79)": [0, 0], "tail (<70)": [0, 0]}
        for pitch in PITCHES:
            agg = _aggregate(fmt, pitch)
            print(f"{pitch:<7} {agg['runs']:8.1f} {agg['wkts']:7.1f} "
                  f"{agg['dot_pct']:6.1f}% {agg['bdry_per_100']:11.1f}")
            for row in agg["rows"]:
                for rating, (balls, runs) in row["per_rating"].items():
                    key = ("elite (80+)" if rating >= 80
                           else "mid (70-79)" if rating >= 70 else "tail (<70)")
                    tiers[key][0] += balls
                    tiers[key][1] += runs
        print(f"  -- SR by rating tier (confounded by phase; informational) --")
        for label, (balls, runs) in tiers.items():
            if balls:
                print(f"     {label:<14} balls={balls:>6,} SR={runs / balls * 100:6.1f}")
