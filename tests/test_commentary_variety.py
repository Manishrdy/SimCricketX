"""
Commentary variety, and commentary's isolation from the simulation.

Two things are under test here, both of which were real defects:

1. The tag filter narrowed the template pool DOWN to style-matched lines.
   Style tags ("pace"/"spin") are rare in the pack — one dot-ball line in
   twenty carried "pace" — so every dot ball bowled by a seamer produced the
   same sentence, word for word. Over a first-class innings, which is mostly
   dot balls, that was 27% of all commentary being one line.

2. CommentaryEngine drew from the shared `random` module. random.choice()
   consumes a bit-count that depends on len(seq), so adding lines to the pack
   reordered the RNG stream for the rest of every match — commentary could
   change who won. It now owns a generator.
"""
import os
import random
import sys
from collections import Counter

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.commentary_engine import CommentaryEngine

PACE = "Fast-medium"
SPIN = "Off spin"
STATE = {"current_over": 12, "current_ball": 2, "innings": 1}


def _context(runs, bowling_type):
    return {"type": "run", "runs": runs, "batter": "Batter", "bowler": "Bowler",
            "bowling_type": bowling_type, "batting_team": "IND",
            "bowling_team": "AUS"}


def _sample(engine, runs, bowling_type, n=600):
    return Counter(engine.get_commentary(_context(runs, bowling_type), dict(STATE))
                   for _ in range(n))


@pytest.mark.parametrize("runs", [0, 1, 2])
@pytest.mark.parametrize("bowling_type", [PACE, SPIN, "Wrist spin", "Fast", ""])
def test_no_single_line_dominates_an_outcome(runs, bowling_type):
    """The symptom: 'a lot of commentary is being repeated, especially for 0
    and 1 run'. No line may take more than a tenth of an outcome's calls."""
    counts = _sample(CommentaryEngine(), runs, bowling_type)
    top_line, top_n = counts.most_common(1)[0]
    total = sum(counts.values())
    assert top_n / total <= 0.10, (
        f"runs={runs} vs {bowling_type or 'unknown style'}: "
        f"{top_n / total:.0%} of lines were identical — {top_line!r}")
    assert len(counts) >= 12, (
        f"runs={runs} vs {bowling_type or 'unknown style'}: only "
        f"{len(counts)} distinct lines in {total} calls")


def test_a_line_does_not_come_straight_back(runs=0):
    """Uniform random picks cluster, and a cluster is what a reader notices.
    Nothing may repeat inside a window of the pool."""
    engine = CommentaryEngine()
    seen = [engine.get_commentary(_context(runs, PACE), dict(STATE))
            for _ in range(20)]
    assert len(set(seen)) == len(seen), "a line came back inside twenty balls"


def test_style_mismatched_lines_are_kept_out():
    """The tag filter still does its job: a spinner never gets a line written
    for seam, and vice versa. (Checked through the pack rather than by
    sampling, so it holds for every tagged line, not just the popular ones.)"""
    engine = CommentaryEngine()
    for key in ("dot", "single", "double", "three", "boundary_four"):
        for style_tag, wrong_tag, bowling_type in (("pace", "spin", PACE),
                                                   ("spin", "pace", SPIN)):
            wrong = {t["text"] for t in engine.events[key]
                     if wrong_tag in t.get("tags", [])}
            if not wrong:
                continue
            got = set(_sample(engine, {"dot": 0, "single": 1, "double": 2,
                                       "three": 3, "boundary_four": 4}[key],
                              bowling_type, n=400))
            assert not (got & wrong), (
                f"{key}: {bowling_type} was given a {wrong_tag} line")


@pytest.mark.parametrize("wicket_type,expected_key", [
    ("Bowled", "wicket_bowled"), ("Caught", "wicket_caught"),
    ("LBW", "wicket_lbw"), ("Run Out", "wicket_run_out"),
    ("Stumped", "wicket_stumped"), ("Hit Wicket", "wicket_hit_wicket"),
])
@pytest.mark.parametrize("bowling_type", [PACE, SPIN])
def test_wickets_have_a_pool_for_both_styles(wicket_type, expected_key,
                                             bowling_type):
    """Two separate faults here. Bowled and lbw off spin had two lines each,
    so a spinner taking four wickets said the same thing twice. And the key
    was built as f"wicket_{type.lower()}", which turned "Run Out" into
    "wicket_run out" — a key that does not exist — so every run-out and every
    hit wicket was narrated out of the CAUGHT pool."""
    engine = CommentaryEngine()
    ctx = _context(0, bowling_type)
    ctx.update({"type": "wicket", "wicket_type": wicket_type, "batter_out": True})
    assert engine._map_context_to_key(ctx) == expected_key
    counts = Counter(engine.get_commentary(dict(ctx), dict(STATE))
                     for _ in range(400))
    assert len(counts) >= 8, (
        f"{wicket_type} off {bowling_type}: only {len(counts)} distinct lines")


@pytest.mark.parametrize("extra_type,expected_key", [
    ("Wide", "wide"), ("No Ball", "noball"),
    ("Byes", "byes"), ("Leg Bye", "legbyes"),
])
def test_extras_reach_their_own_pools(extra_type, expected_key):
    """Byes and leg byes fell through to the dot-ball pool, so three byes run
    off the keeper were narrated as a solid forward defence — and the pack's
    byes/legbyes templates were unreachable. ("bye" is a substring of "leg
    bye", so the order of those two checks matters.)"""
    engine = CommentaryEngine()
    ctx = _context(1, PACE)
    ctx.update({"is_extra": True, "extra_type": extra_type})
    assert engine._map_context_to_key(ctx) == expected_key
    counts = Counter(engine.get_commentary(dict(ctx), dict(STATE))
                     for _ in range(400))
    assert len(counts) >= 8, (
        f"{extra_type}: only {len(counts)} distinct lines")


def test_finger_and_wrist_spin_are_recognised_as_spin():
    """Both are in engine/player.py's BOWLING_TYPES and both used to fall
    through the style list, counting as neither pace nor spin."""
    engine = CommentaryEngine()
    for style in ("Off spin", "Leg spin", "Finger spin", "Wrist spin"):
        assert engine._get_bowling_tags({"bowling_type": style}) == {"spin"}
    for style in ("Fast", "Fast-medium", "Medium-fast", "Medium"):
        assert engine._get_bowling_tags({"bowling_type": style}) == {"pace"}
    assert engine._get_bowling_tags({"bowling_type": None}) == set()


def test_commentary_does_not_consume_the_shared_rng():
    """The important one. Commentary must not be able to change the cricket:
    if generating a line moves the shared stream, adding a template to the
    pack changes ball outcomes, match results and every pinned scoring band
    (see the REPIN NOTEs in tests/test_scoring_calibration.py)."""
    engine = CommentaryEngine()
    random.seed(1234)
    before = [random.random() for _ in range(5)]

    random.seed(1234)
    for runs in (0, 1, 0, 4, 0, 1):
        engine.get_commentary(_context(runs, PACE), dict(STATE))
    after = [random.random() for _ in range(5)]

    assert before == after, "commentary displaced the simulation's RNG stream"


def test_a_seeded_match_still_produces_the_same_commentary():
    """Isolation must not cost reproducibility — the engine seeds its own
    generator from the shared one at construction."""
    def run():
        random.seed(99)
        engine = CommentaryEngine()
        return [engine.get_commentary(_context(r, PACE), dict(STATE))
                for r in (0, 1, 0, 0, 1)]

    assert run() == run()
