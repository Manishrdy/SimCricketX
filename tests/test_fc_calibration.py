"""
First-Class scoring calibration.

The counterpart to test_scoring_calibration.py's T20/ListA bands, which have
no FC equivalent — so FC scoring could drift silently. FC needs its own
harness rather than a branch in that one, for two reasons:

1. A first-class innings does not end on an over limit. It ends when ten
   wickets fall or the captain declares, so the meaningful measure is "how
   many runs does it cost to bowl a side out", not "what do they make in 20
   overs". Declarations are disabled here so every innings runs to ten
   wickets and the pure ball-by-ball economy is what is being measured.

2. A uniform-rated XI is useless as a first-class instrument. With no tail,
   every side bats like six No. 3s and totals land far above anything a real
   team makes. The XI below carries the spread a real one has.

The numbers these bands encode are ordinary modern first-class cricket:
roughly 3.5 runs an over on a first-day pitch, a ball every ~60 for a wicket,
a dot rate near two thirds, and a specialist top-order batter surviving
several times longer than a No. 11. (These are FIRST-innings figures on a
fresh pitch under clear skies, which is the fastest-scoring passage of a
first-class match — scripts/bench_fc.py measures the match-wide economy and
reads about 0.1 RPO lower.)

Report mode:
    pytest tests/test_fc_calibration.py::test_report_fc_calibration -s --no-cov
"""
import os
import random
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.match as match_module
from engine import fc_declaration

PITCHES = ["Green", "Dry", "Hard", "Flat", "Dead"]
# 40 innings per pitch. At 24 the per-pitch mean moved by 30-40 runs between
# otherwise-identical runs whenever an unrelated change shifted the RNG
# stream (adding commentary triggers was enough), which is more variance than
# the bands can absorb. Still under ten seconds for the whole module.
SEEDS = tuple(range(1, 41))

# (bat, bowl, technique, temperament, stamina, role, bowling_type, will_bowl)
FC_XI = [
    (74, 20, 76, 74, 55, "Batsman",      "Medium",      False),
    (72, 20, 74, 72, 55, "Batsman",      "Medium",      False),
    (78, 25, 80, 78, 55, "Batsman",      "Medium",      False),
    (76, 30, 76, 76, 55, "Batsman",      "Off spin",    False),
    (70, 20, 70, 70, 55, "Batsman",      "Medium",      False),
    (64, 30, 64, 66, 55, "Wicketkeeper", "Medium",      False),
    (58, 68, 58, 62, 68, "All-rounder",  "Fast-medium", True),
    (40, 74, 42, 50, 72, "Bowler",       "Fast",        True),
    (28, 76, 30, 45, 70, "Bowler",       "Fast-medium", True),
    (20, 72, 22, 40, 74, "Bowler",       "Off spin",    True),
    (12, 70, 14, 38, 74, "Bowler",       "Leg spin",    True),
]

# The XI a user actually picks. FC_XI's top six averages 72.3 — roughly the
# 85th percentile of the player pool, i.e. a good domestic side. Given a pool
# whose median batting rating is 45 and whose 99th percentile is 92, nobody
# builds a team that way: an all-star international XI comes out with a top six
# near 86 and an attack near 89, and it scores far more.
#
# Kept as a SECOND banded tier rather than a replacement. Benchmarking only
# FC_XI meant every documented FC number described a match almost none of our
# users play, so a perfectly healthy 469 on Hard read as a regression.
ELITE_XI = [
    # (bat, bowl, technique, temperament, stamina, role, bowling_type, will_bowl)
    (88, 25, 88, 86, 60, "Batsman",      "Medium",      False),
    (86, 25, 86, 84, 60, "Batsman",      "Medium",      False),
    (92, 30, 92, 90, 60, "Batsman",      "Medium",      False),
    (89, 35, 88, 88, 60, "Batsman",      "Off spin",    False),
    (84, 25, 84, 82, 60, "Batsman",      "Medium",      False),
    (80, 35, 78, 80, 60, "Wicketkeeper", "Medium",      False),
    (72, 84, 70, 74, 72, "All-rounder",  "Fast-medium", True),
    (52, 92, 54, 60, 76, "Bowler",       "Fast",        True),
    (36, 95, 38, 52, 74, "Bowler",       "Fast-medium", True),
    (28, 90, 30, 48, 78, "Bowler",       "Off spin",    True),
    (20, 88, 22, 45, 78, "Bowler",       "Leg spin",    True),
]

# Runs it should cost to bowl a side out on each surface, and the run rate
# while doing it. Wide enough to survive a re-tune; narrow enough that a
# pitch drifting out of one no longer means what its name says.
#            (runs_lo, runs_hi, rpo_lo, rpo_hi)
#
# Moved up by the 2026-08-30 scoring acceleration: the FC scoring matrices
# now carry ~12% more run-scoring mass and ~5% more wicket, which lifts the
# run rate without letting totals or batting averages run away with it.
#
# Widened at the same time. Two measurements of the SAME model taken either
# side of an RNG-stream shift (the commentary engine moving to its own
# generator) differed by up to 54 runs on a pitch, which is more than the old
# +/-45 half-width. These bands are for catching a model that has drifted, not
# for pinning one sample of it, so they now carry +/-65 runs and +/-0.32 RPO.
#
# Flat and Dead were moved up again on 2026-09-04 at the user's request:
# a first-innings total on the board (all out or declared) should read 400+
# on Flat and 500-600 on Dead, so the five surfaces span a real ladder
# instead of Flat and Dead producing the same score. Flat took a 6% wicket
# cut plus a 5% scoring lift; Dead took a 10% scoring lift with its early
# wicket factors dropped from 0.825 to 0.680 (it had been TAKING WICKETS
# MORE EASILY than Flat on a fresh pitch, which is backwards for the
# deadest surface in the game). Green/Dry/Hard are untouched.
FC_TARGET_BANDS = {
    "Green": (190, 315, 2.80, 3.45),
    "Dry":   (290, 415, 3.00, 3.65),
    "Hard":  (290, 420, 3.15, 3.80),
    "Flat":  (375, 510, 3.55, 4.20),
    "Dead":  (490, 690, 4.00, 4.70),
}

# Whole-format aggregates. Runs per wicket now sits ABOVE real first-class
# cricket's ~31, and deliberately so: two of the five surfaces were asked to
# produce 400+ and 500-600 first-innings totals, and that has to show up in
# the aggregate. Green, Dry and Hard still carry the realistic numbers.
AGG_RPO = (3.45, 4.00)
AGG_RUNS_PER_WICKET = (35.0, 47.0)
AGG_DOT_PCT = (60.0, 69.0)
# A specialist top-six batter must survive several times longer than a
# genuine No. 11. Before this calibration the ratio was 1.02 — the tail
# batted exactly like the top order, which is why nobody ever collapsed.
#
# This is a REGRESSION GUARD, not the realism check. It pools 1-6 against
# 9-11 over first innings that always run to ten wickets (declarations are
# off here), so the tail bats far more often than it would in a real match
# and the pooled ratio reads lower than the per-position picture. The actual
# realism check is the position-by-position average table in
# scripts/bench_fc.py, which tracks real first-class figures slot by slot.
TOP_ORDER_VS_TAIL_SURVIVAL = (2.0, 5.0)
# A first-class surface should not be a different sport from its neighbour.
# Widened from 2.1 to 2.6 when Dead was lifted to a 500-600 pitch while
# Green stayed at ~285: that ladder is a 2.1x spread by construction, so the
# old ceiling left no headroom for sampling noise at all (Dead's own mean
# moves 560-600 between samples — its tail is long enough that 40 innings
# does not pin it). Still far from the 5.1x caricature this guard was
# written for.
MAX_PITCH_SPREAD = 2.6


def _squad(prefix, xi=None):
    return [{
        "name": f"{prefix}_P{i+1}", "role": role,
        "batting_rating": bat, "bowling_rating": bowl, "fielding_rating": 68,
        "technique_rating": tech, "temperament_rating": temp,
        "stamina_rating": stam,
        "batting_hand": "Left" if i in (1, 4, 8) else "Right",
        "bowling_type": btype, "bowling_hand": "Right" if i % 2 else "Left",
        "will_bowl": wb, "is_captain": i == 0, "is_wicketkeeper": i == 5,
    } for i, (bat, bowl, tech, temp, stam, role, btype, wb) in enumerate(
        xi if xi is not None else FC_XI)]


def _fc_innings(pitch, seed, budget=8000, xi=None):
    """One first innings run to ten wickets, declarations disabled."""
    original = fc_declaration.should_declare
    fc_declaration.should_declare = lambda **kw: False
    try:
        random.seed(seed)
        m = match_module.Match({
            "match_id": str(uuid.uuid4()), "created_by": "calibration",
            "team_home": "HOM_cal", "team_away": "AWY_cal",
            "stadium": "Cal Ground", "pitch": pitch,
            "toss": "Heads", "toss_winner": "HOM", "toss_decision": "Bat",
            "match_format": "FC", "days": 5, "simulation_mode": "auto",
            "playing_xi": {"home": _squad("H", xi), "away": _squad("A", xi)},
            "substitutes": {"home": [], "away": []},
            "weather_forecast": "clear",
        })
        order = {p["name"]: i + 1 for i, p in enumerate(m.batting_team)}
        legal = dots = 0
        pos_balls, pos_outs, pos_runs = {}, {}, {}
        dismissals, extras = {}, {}
        for _ in range(budget):
            r = m.next_ball()
            assert "error" not in r, r.get("error")
            bd = r.get("ball_data") or {}
            is_extra, xt = bool(bd.get("is_extra")), bd.get("extra_type")
            if is_extra and xt:
                extras[xt] = extras.get(xt, 0) + 1
            # Count only wickets that actually fell: ball_data carries the
            # selected wicket_type even when the catch is then dropped, so
            # keying off it alone overstates Caught by the drop rate.
            if bd.get("batter_out") and bd.get("wicket_type"):
                wt = bd["wicket_type"]
                dismissals[wt] = dismissals.get(wt, 0) + 1
            if not (is_extra and xt in ("Wide", "No Ball")):
                legal += 1
                if (0 if is_extra else (bd.get("runs") or 0)) == 0:
                    dots += 1
                pos = order.get(bd.get("striker"))
                if pos:
                    pos_balls[pos] = pos_balls.get(pos, 0) + 1
                    pos_runs[pos] = pos_runs.get(pos, 0) + (
                        0 if is_extra else (bd.get("runs") or 0))
            if bd.get("batter_out"):
                pos = order.get(bd.get("striker"))
                if pos:
                    pos_outs[pos] = pos_outs.get(pos, 0) + 1
            if r.get("innings_end"):
                sc = r.get("scorecard_data", {}) or {}
                whole, _, part = str(sc.get("overs", "0.0")).partition(".")
                return {"runs": sc.get("total_score", 0),
                        "wkts": sc.get("wickets", 0),
                        "balls": int(whole) * 6 + int(part or 0),
                        "legal": legal, "dots": dots,
                        "pos_balls": pos_balls, "pos_outs": pos_outs,
                        "pos_runs": pos_runs,
                        "dismissals": dismissals, "extras": extras}
        raise AssertionError(f"FC/{pitch}/{seed}: innings did not end in {budget} balls")
    finally:
        fc_declaration.should_declare = original


def _collect(xi=None):
    out = {}
    for pitch in PITCHES:
        rows = [_fc_innings(pitch, s, xi=xi) for s in SEEDS]
        runs = sum(r["runs"] for r in rows)
        balls = sum(r["balls"] for r in rows)
        out[pitch] = {
            "runs": runs / len(rows),
            "wkts": sum(r["wkts"] for r in rows) / len(rows),
            "rpo": runs / (balls / 6),
            "dot_pct": sum(r["dots"] for r in rows) / max(sum(r["legal"] for r in rows), 1) * 100,
            "runs_per_wkt": runs / max(sum(r["wkts"] for r in rows), 1),
            "rows": rows,
        }
    return out


@pytest.fixture(scope="module")
def fc_stats():
    """Every pitch simulated once; each test reads the same sample."""
    return _collect()


@pytest.fixture(scope="module")
def fc_stats_elite():
    """The same sample for an all-star XI — the squad users actually pick."""
    return _collect(ELITE_XI)


def _survival(stats, positions):
    balls = outs = 0
    for pitch in stats.values():
        for r in pitch["rows"]:
            for p in positions:
                balls += r["pos_balls"].get(p, 0)
                outs += r["pos_outs"].get(p, 0)
    return balls / max(outs, 1)


@pytest.mark.parametrize("pitch", PITCHES)
def test_fc_pitch_sits_in_its_target_band(fc_stats, pitch):
    lo, hi, rpo_lo, rpo_hi = FC_TARGET_BANDS[pitch]
    got = fc_stats[pitch]
    assert lo <= got["runs"] <= hi, (
        f"FC/{pitch}: {got['runs']:.0f} runs to bowl a side out, outside {lo}-{hi}")
    assert rpo_lo <= got["rpo"] <= rpo_hi, (
        f"FC/{pitch}: {got['rpo']:.2f} RPO outside {rpo_lo}-{rpo_hi}")


def test_fc_scores_at_a_first_class_rate(fc_stats):
    runs = sum(s["runs"] for s in fc_stats.values())
    wkts = sum(s["wkts"] for s in fc_stats.values())
    rpo = sum(s["rpo"] for s in fc_stats.values()) / len(fc_stats)
    dot = sum(s["dot_pct"] for s in fc_stats.values()) / len(fc_stats)
    assert AGG_RPO[0] <= rpo <= AGG_RPO[1], f"aggregate RPO {rpo:.2f}"
    assert AGG_DOT_PCT[0] <= dot <= AGG_DOT_PCT[1], f"dot rate {dot:.1f}%"
    assert AGG_RUNS_PER_WICKET[0] <= runs / wkts <= AGG_RUNS_PER_WICKET[1], (
        f"runs per wicket {runs / wkts:.1f}")


def test_fc_tail_bats_like_a_tail(fc_stats):
    """The single worst symptom of the old calibration: a No. 11 survived as
    long as a No. 3."""
    ratio = _survival(fc_stats, range(1, 7)) / _survival(fc_stats, range(9, 12))
    lo, hi = TOP_ORDER_VS_TAIL_SURVIVAL
    assert lo <= ratio <= hi, (
        f"top-six vs tail survival ratio {ratio:.2f}x outside {lo}-{hi}x")


def test_fc_pitches_are_not_caricatures(fc_stats):
    """Green once averaged 177 and Dead 897 — a 5x spread no groundsman
    could produce. Real first-class surfaces vary far less than that."""
    runs = [s["runs"] for s in fc_stats.values()]
    spread = max(runs) / min(runs)
    assert spread <= MAX_PITCH_SPREAD, f"pitch spread {spread:.2f}x"


def test_report_fc_calibration(fc_stats):
    print(f"\n{'pitch':7} {'runs':>6} {'wkts':>6} {'RPO':>6} {'dot%':>7} {'r/wkt':>7}")
    for pitch in PITCHES:
        s = fc_stats[pitch]
        print(f"{pitch:7} {s['runs']:6.0f} {s['wkts']:6.2f} {s['rpo']:6.2f} "
              f"{s['dot_pct']:7.1f} {s['runs_per_wkt']:7.1f}")
    ratio = _survival(fc_stats, range(1, 7)) / _survival(fc_stats, range(9, 12))
    print(f"top-six vs tail survival: {ratio:.2f}x")


# ---------------------------------------------------------------------------
# Scorecard fidelity — the details a cricket person spots first
# ---------------------------------------------------------------------------

def test_fc_dismissal_table_is_shaped_like_first_class():
    """Direct test of the weights, independent of any sample.

    FC used T20's table until this pass: it gave spin a 25% stumping rate and
    12% run-outs. In the long game the edge is the defining mode, stumpings
    are a low single-digit share, and nobody is running a risky second in the
    second session of day two."""
    from engine.ball_outcome import _get_wicket_type_by_bowling

    for style in ("Fast", "Fast-medium", "Medium-fast"):
        types, weights = _get_wicket_type_by_bowling(style, is_fc=True)
        w = dict(zip(types, weights))
        assert abs(sum(weights) - 1.0) < 1e-9
        assert w["Caught"] > 0.55, "the edge is the defining first-class mode"
        assert w.get("Stumped", 0) == 0, "you are not stumped off a quick"
        assert w["Run Out"] <= 0.03

    for style in ("Off spin", "Leg spin", "Finger spin", "Wrist spin"):
        types, weights = _get_wicket_type_by_bowling(style, is_fc=True)
        w = dict(zip(types, weights))
        assert abs(sum(weights) - 1.0) < 1e-9
        assert w["Caught"] > 0.45
        assert 0.02 <= w["Stumped"] <= 0.09, "T20's table had this at 0.25"
        assert w["Run Out"] <= 0.03, "T20's table had this at 0.12"
        assert w["LBW"] > w["Stumped"], "spin gets far more lbws than stumpings"

    # T20 and List A must be untouched by the FC table.
    t20_types, t20_weights = _get_wicket_type_by_bowling("Off spin")
    assert dict(zip(t20_types, t20_weights))["Stumped"] == 0.25


# Observed mix from the first-innings sample above. It runs catch-heavy
# compared with a whole match (a fresh pitch is bowled mostly by seam, with
# a full slip cordon), and carries no stumpings at all for the same reason —
# which is itself correct. scripts/bench_fc.py reports the match-wide mix
# across all four innings, and that is what to compare with real-world
# figures (~57% caught, ~21% bowled, ~15% lbw, ~2.5% stumped).
FC_FIRST_INNINGS_DISMISSAL_BANDS = {
    "Caught":  (58.0, 72.0),
    "Bowled":  (15.0, 26.0),
    "LBW":     (7.0, 16.0),
    "Run Out": (1.0, 5.5),
}

FC_EXTRAS_BANDS = {
    "Leg Bye": (32.0, 44.0),
    "Byes":    (20.0, 32.0),
    "No Ball": (13.0, 24.0),
    "Wide":    (13.0, 24.0),
}


def test_fc_observed_dismissal_mix_stays_in_band(fc_stats):
    counts = {}
    for pitch in fc_stats.values():
        for row in pitch["rows"]:
            for kind, n in row["dismissals"].items():
                counts[kind] = counts.get(kind, 0) + n
    total = max(sum(counts.values()), 1)
    for kind, (lo, hi) in FC_FIRST_INNINGS_DISMISSAL_BANDS.items():
        pct = counts.get(kind, 0) / total * 100
        assert lo <= pct <= hi, f"{kind}: {pct:.1f}% outside {lo}-{hi}%"
    # Stumpings can never again dominate the way T20's table made them.
    assert counts.get("Stumped", 0) < counts.get("Bowled", 0)


def test_fc_extras_are_byes_country(fc_stats):
    """First-class extras are byes and leg-byes, not wides: bowlers attack
    the stumps and the channel all day, so the ball beating the bat and
    running away is far more common than one called wide."""
    counts = {}
    for pitch in fc_stats.values():
        for row in pitch["rows"]:
            for kind, n in row["extras"].items():
                counts[kind] = counts.get(kind, 0) + n
    total = max(sum(counts.values()), 1)
    for kind, (lo, hi) in FC_EXTRAS_BANDS.items():
        pct = counts.get(kind, 0) / total * 100
        assert lo <= pct <= hi, f"{kind}: {pct:.1f}% outside {lo}-{hi}%"
    assert counts.get("Leg Bye", 0) > counts.get("Wide", 0)


def test_fc_ball_condition_scales_scoring_not_just_wickets():
    """A new ball is not a purely hostile event: it takes more edges AND
    comes onto the bat. An old reversing one is hard work at both ends."""
    from engine.ground_config import get_fc_ball_condition_outcome_factors as f

    new_ball = f("Fast", ball_overs_bowled=3, new_ball_overs=80)
    assert new_ball["Wicket"] > 1.0
    assert new_ball["Four"] > 1.0, "a hard new ball races away"
    assert new_ball["Extras"] > 1.0, "and beats bat and keeper alike"

    middle = f("Fast", ball_overs_bowled=40, new_ball_overs=80)
    assert middle == {}, "the middle overs are the ball doing nothing special"

    reverse = f("Fast", ball_overs_bowled=70, new_ball_overs=80)
    assert reverse["Wicket"] > 1.0
    assert reverse["Four"] < 1.0, "a scuffed reversing ball is hard to get away"
    assert reverse["Dot"] > 1.0

    # Fast-medium must reverse it too — restricting this to express pace left
    # most attacks with no old-ball threat at all.
    assert f("Fast-medium", ball_overs_bowled=70, new_ball_overs=80)["Wicket"] > 1.0
    # ...but a spinner gets nothing from either window.
    assert f("Off spin", ball_overs_bowled=3, new_ball_overs=80)["Wicket"] == 1.0

    # Scoring terms are style-independent: the ball's hardness carries it to
    # the rope whoever is running in.
    assert (f("Off spin", ball_overs_bowled=3, new_ball_overs=80)["Four"]
            == new_ball["Four"])


def test_new_batters_are_vulnerable_enough_to_produce_ducks():
    """Scaling effective batting alone under-produced ducks at every
    position by about 40%: it also makes a new batter worse at SCORING,
    which keeps him on strike longer and cancels much of the effect. The
    dismissal odds are raised directly instead."""
    from engine.ball_outcome import _FC_NEW_BATTER_WICKET_BOOST as boost

    assert boost[0] > 1.0, "a batter who has just walked in must be vulnerable"
    assert boost[0] == boost[2], "the first three balls are the dangerous ones"
    assert boost[0] > boost[5], "and it must ease as he gets his eye in"
    assert boost.get(6) is None, "settled in — no boost after five balls"


def test_fc_duck_rate_is_first_class(fc_stats):
    """Roughly one first-class innings in ten ends without a run. Each
    position bats once per innings, so a duck is that slot being dismissed
    with pos_runs still at zero."""
    innings = ducks = 0
    for pitch in fc_stats.values():
        for row in pitch["rows"]:
            for pos in range(1, 12):
                out = row["pos_outs"].get(pos, 0)
                if not (out or row["pos_balls"].get(pos, 0)):
                    continue                      # did not bat
                innings += 1
                if out and not row["pos_runs"].get(pos, 0):
                    ducks += 1
    assert innings > 500, "not enough sample to judge"
    rate = ducks / innings * 100
    assert 7.5 <= rate <= 15.0, f"duck rate {rate:.1f}% outside 7.5-15%"


def test_calibration_squads_match_the_bench_script():
    """scripts/bench_fc.py keeps its own copy of both XIs so it stays runnable
    as a standalone file on a box with no test deps. Copies drift — the
    tournament-stats migration script silently fell 17 columns behind its
    model exactly this way — so pin them to each other."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_bench_fc", os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "scripts", "bench_fc.py"))
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)

    assert bench.FC_XI == FC_XI, "bench_fc.FC_XI drifted from the calibration copy"
    assert bench.ELITE_XI == ELITE_XI, "bench_fc.ELITE_XI drifted from the calibration copy"


# ---------------------------------------------------------------------------
# Squad quality: level vs gap
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pitch", PITCHES)
def test_fc_elite_squad_sits_in_the_same_band(fc_stats_elite, pitch):
    """An all-star XI scores no more than a good domestic one, and the SAME
    bands hold for both.

    This is not a coincidence, it is the shape of the model: skill_frac is
    batting/(batting+bowling), a RATIO. Lifting a whole match from a top six
    of 72 to one of 86 lifts the attacks with it, so the contest is unchanged.
    Scores move when the two sides are unequal, not when both are good.
    """
    lo, hi, rpo_lo, rpo_hi = FC_TARGET_BANDS[pitch]
    got = fc_stats_elite[pitch]
    assert lo <= got["runs"] <= hi, (
        f"FC-elite/{pitch}: {got['runs']:.0f} runs, outside {lo}-{hi}")
    assert rpo_lo <= got["rpo"] <= rpo_hi, (
        f"FC-elite/{pitch}: {got['rpo']:.2f} RPO outside {rpo_lo}-{rpo_hi}")


def test_fc_scoring_tracks_the_rating_gap_not_the_rating_level(fc_stats, fc_stats_elite):
    """Raising both sides equally must not move the aggregate.

    A regression guard on the property above: if some future change makes an
    absolute rating matter on its own (a threshold, a cap, a non-linear
    transform on one side only), elite-vs-elite will start diverging from
    standard-vs-standard and this fails.
    """
    std = sum(s["runs"] for s in fc_stats.values()) / len(fc_stats)
    elite = sum(s["runs"] for s in fc_stats_elite.values()) / len(fc_stats_elite)
    assert abs(elite - std) <= 65, (
        f"elite squads averaged {elite:.0f} vs standard {std:.0f} — the model "
        f"has started reading the rating level, not the gap between sides")


def test_fc_a_mismatched_attack_is_what_moves_the_score(fc_stats):
    """The flip side, stated as a test so the property is not mistaken for
    'ratings do nothing'. An elite top six against a domestic attack DOES
    score far more — that gap is the lever, and it is why a user's all-star
    XI beating a weaker side posts totals no single-tier bench ever shows."""
    mixed = [_fc_innings("Hard", s, xi=None) for s in SEEDS[:12]]
    strong_bat = []
    for s in SEEDS[:12]:
        random.seed(s)
        m = match_module.Match({
            "match_id": str(uuid.uuid4()), "created_by": "calibration",
            "team_home": "HOM_cal", "team_away": "AWY_cal",
            "stadium": "Cal Ground", "pitch": "Hard",
            "toss": "Heads", "toss_winner": "HOM", "toss_decision": "Bat",
            "match_format": "FC", "days": 5, "simulation_mode": "auto",
            # Elite batting, ordinary attack — the mismatch case.
            "playing_xi": {"home": _squad("H", ELITE_XI), "away": _squad("A")},
            "substitutes": {"home": [], "away": []},
            "weather_forecast": "clear",
        })
        original = fc_declaration.should_declare
        fc_declaration.should_declare = lambda **kw: False
        try:
            for _ in range(8000):
                r = m.next_ball()
                if r.get("innings_end") or r.get("match_over"):
                    break
        finally:
            fc_declaration.should_declare = original
        strong_bat.append(m.fc_innings_totals.get(1, {}).get("score", m.score))

    even = sum(r["runs"] for r in mixed) / len(mixed)
    gapped = sum(strong_bat) / len(strong_bat)
    assert gapped > even * 1.15, (
        f"an elite top six against a domestic attack scored {gapped:.0f} vs "
        f"{even:.0f} for an even contest — the rating gap should matter")
