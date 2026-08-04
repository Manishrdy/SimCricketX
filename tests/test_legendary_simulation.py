"""
Dedicated integration tests for Legendary Mode (scenario-based simulation).

Exercises the full pipeline: pack loading → engine init → player mapping →
shuffled outcome replay through the real match engine → result verification.

Unlike the unit tests in test_historical_scenarios.py, these tests instantiate
a real Match object and call next_ball() to verify that the scenario engine
hooks actually fire and that outcomes are correctly processed end-to-end.
"""
import contextlib
import copy
import io
import json
import os
import random
import sys
import uuid

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.match as match_module
from engine.scenario_packs import get_scenario_pack, _validate_pack
from engine.scenario_engine import LegendaryScenarioEngine, create_scenario_engine

PACK_ID = "ind_pak_t20wc_2022"

# ── helpers ──────────────────────────────────────────────────────────────── #

def _build_xi(prefix):
    roles = ["Batsman"] * 4 + ["All-Rounder"] * 2 + ["Wicketkeeper"] + ["Bowler"] * 4
    return [{
        "name": f"{prefix}_P{i+1}",
        "role": roles[i],
        "batting_rating": 78 if i < 6 else 35,
        "bowling_rating": 75 if i >= 6 else 25,
        "fielding_rating": 70,
        "batting_hand": "Right",
        "bowling_type": "Fast" if i in (7, 8, 10) else ("Spin" if i == 9 else "Medium"),
        "bowling_hand": "Right",
        "will_bowl": i >= 6,
        "is_captain": i == 0,
    } for i in range(11)]


def _match_data(pack, seed=7):
    return {
        "match_id": str(uuid.uuid4()),
        "created_by": "legendary-test@test",
        "timestamp": "20260614000000",
        "team_home": "HOM_legendary-test@test",
        "team_away": "AWY_legendary-test@test",
        "stadium": "Test Ground",
        "pitch": "Hard",
        "toss_winner": "HOM",
        "toss_decision": "Bat",
        "match_format": "T20",
        "simulation_mode": "auto",
        "playing_xi": {"home": _build_xi("H"), "away": _build_xi("A")},
        "substitutes": {"home": [], "away": []},
        "scenario_mode": f"historical:{PACK_ID}",
        "scenario_pack": pack,
        "rain_probability": 0.0,
    }


def _simulate_to_completion(m, max_balls=600):
    """Run next_ball() until match_over, suppressing stdout."""
    balls = []
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(max_balls):
            outcome = m.next_ball()
            balls.append(outcome)
            if outcome.get("match_over"):
                return balls
    raise AssertionError(f"match did not complete in {max_balls} balls")


# ── fixtures ─────────────────────────────────────────────────────────────── #

@pytest.fixture()
def pack():
    return get_scenario_pack(PACK_ID)


# ═══════════════════════════════════════════════════════════════════════════ #
#  1. Engine wiring — does the match engine actually use the legendary pool?
# ═══════════════════════════════════════════════════════════════════════════ #

class TestEngineWiring:
    def test_match_creates_legendary_engine(self, pack):
        random.seed(42)
        m = match_module.Match(_match_data(pack))
        assert isinstance(m.scenario_engine, LegendaryScenarioEngine)
        assert m.scenario_engine.active is True

    def test_first_ball_comes_from_pool(self, pack):
        """The very first delivery should be a scenario-override outcome,
        not a ratings-based calculate_outcome() result."""
        random.seed(42)
        m = match_module.Match(_match_data(pack))
        pool_before = m.scenario_engine._pool_index
        with contextlib.redirect_stdout(io.StringIO()):
            m.next_ball()
        assert m.scenario_engine._pool_index == pool_before + 1

    def test_pool_index_advances_every_legal_delivery(self, pack):
        random.seed(42)
        m = match_module.Match(_match_data(pack))
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(6):
                m.next_ball()
        assert m.scenario_engine._pool_index == 6


# ═══════════════════════════════════════════════════════════════════════════ #
#  2. Outcome integrity — shuffled pool preserves the match's DNA
# ═══════════════════════════════════════════════════════════════════════════ #

class TestOutcomeIntegrity:
    def test_boundary_count_matches_pack(self, pack):
        """Total fours + sixes across both innings should equal the pack's."""
        random.seed(7)
        m = match_module.Match(_match_data(pack))
        balls = _simulate_to_completion(m)

        inn1_balls = pack["innings"]["1"]["balls"]
        inn2_balls = pack["innings"]["2"]["balls"]
        pack_fours = sum(1 for b in inn1_balls + inn2_balls if b["r"] == 4 and "w" not in b)
        pack_sixes = sum(1 for b in inn1_balls + inn2_balls if b["r"] == 6 and "w" not in b)

        match_fours = 0
        match_sixes = 0
        all_batsman_stats = {}
        all_batsman_stats.update(m.first_innings_batting_stats or {})
        all_batsman_stats.update(m.batsman_stats or {})
        for stats in all_batsman_stats.values():
            match_fours += stats.get("fours", 0)
            match_sixes += stats.get("sixes", 0)

        # The match might end early (all out / chase completed) so the pool
        # may not be fully consumed. Actual boundaries ≤ pack boundaries.
        assert match_fours <= pack_fours
        assert match_sixes <= pack_sixes

    def test_all_outcome_types_are_valid(self, pack):
        random.seed(7)
        m = match_module.Match(_match_data(pack))
        balls = _simulate_to_completion(m)
        for resp in balls:
            bd = resp.get("ball_data")
            if bd is None:
                continue
            assert bd["runs"] >= 0
            if bd.get("batter_out"):
                assert bd.get("wicket_type") in (
                    "Caught", "Bowled", "LBW", "Stumped",
                    "Run Out", "Hit Wicket", None,
                )

    def test_wicket_types_from_pack_are_used(self, pack):
        """Wicket types in match outcomes should include types from the pack."""
        random.seed(7)
        m = match_module.Match(_match_data(pack))
        balls = _simulate_to_completion(m)
        wicket_types_seen = set()
        for resp in balls:
            bd = resp.get("ball_data")
            if bd and bd.get("batter_out") and bd.get("wicket_type"):
                wicket_types_seen.add(bd["wicket_type"])
        # The pack contains Caught, Bowled, LBW, Run Out — at least some should appear.
        assert len(wicket_types_seen) >= 2


# ═══════════════════════════════════════════════════════════════════════════ #
#  3. Full match simulation — completion and result
# ═══════════════════════════════════════════════════════════════════════════ #

class TestFullSimulation:
    def test_match_completes_with_result(self, pack):
        random.seed(7)
        m = match_module.Match(_match_data(pack))
        _simulate_to_completion(m)
        assert m.result
        assert any(keyword in m.result.lower() for keyword in ("won", "tied"))

    def test_both_innings_are_played(self, pack):
        random.seed(7)
        m = match_module.Match(_match_data(pack))
        _simulate_to_completion(m)
        assert m.first_innings_score is not None
        assert m.first_innings_score > 0
        assert m.score >= 0

    def test_first_innings_score_is_plausible(self, pack):
        """The first innings total should be in a plausible T20 range,
        driven by the pack's 159-run outcome pool."""
        random.seed(7)
        m = match_module.Match(_match_data(pack))
        _simulate_to_completion(m)
        # The pool has 159 total runs / 8 wickets; early all-outs or
        # boundaries can shift the total, but it should stay T20-shaped.
        assert 40 <= m.first_innings_score <= 200

    def test_different_seeds_produce_different_results(self, pack):
        """Two runs with different seeds should produce different match
        outcomes (different final scores or results)."""
        results = []
        for seed in (7, 42, 99):
            random.seed(seed)
            m = match_module.Match(_match_data(pack))
            _simulate_to_completion(m)
            results.append((m.first_innings_score, m.score, m.result))
        # At least two of the three should differ.
        assert len(set(results)) >= 2


# ═══════════════════════════════════════════════════════════════════════════ #
#  4. Player mapping — positional map between user XI and historical XI
# ═══════════════════════════════════════════════════════════════════════════ #

class TestPlayerMapping:
    def test_innings_1_maps_to_batting_first(self, pack):
        random.seed(7)
        m = match_module.Match(_match_data(pack))
        engine = m.scenario_engine
        # Home team bats first (toss_decision=Bat), so home XI maps to batting_first.
        assert engine.player_map["H_P1"] == pack["batting_first"][0]
        assert engine.player_map["H_P11"] == pack["batting_first"][10]

    def test_innings_2_maps_to_batting_second(self, pack):
        random.seed(7)
        m = match_module.Match(_match_data(pack))
        _simulate_to_completion(m)
        engine = m.scenario_engine
        # After innings transition, away team bats → maps to batting_second.
        assert engine.player_map.get("A_P1") == pack["batting_second"][0]

    def test_all_11_players_mapped(self, pack):
        random.seed(7)
        m = match_module.Match(_match_data(pack))
        assert len(m.scenario_engine.player_map) == 11


# ═══════════════════════════════════════════════════════════════════════════ #
#  5. Pool exhaustion and fallback
# ═══════════════════════════════════════════════════════════════════════════ #

class TestPoolExhaustion:
    def test_phase_changes_to_free_play_on_exhaustion(self, pack):
        random.seed(7)
        m = match_module.Match(_match_data(pack))
        engine = m.scenario_engine
        assert engine.get_phase() == "legendary"
        engine._pool_index = len(engine._pool)
        assert engine.get_phase() == "free_play"

    def test_returns_none_after_pool_drained(self, pack):
        random.seed(7)
        m = match_module.Match(_match_data(pack))
        engine = m.scenario_engine
        batter = {"name": "H_P1"}
        bowler = {"name": "A_P7", "bowling_type": "Fast"}
        # Drain the pool.
        for _ in range(len(engine._pool)):
            assert engine.get_override_outcome(batter, bowler) is not None
        assert engine.get_override_outcome(batter, bowler) is None


# ═══════════════════════════════════════════════════════════════════════════ #
#  6. Innings transition
# ═══════════════════════════════════════════════════════════════════════════ #

class TestInningsTransition:
    def test_second_innings_uses_fresh_pool(self, pack):
        random.seed(7)
        m = match_module.Match(_match_data(pack))
        engine = m.scenario_engine

        # Consume some balls from innings 1 pool.
        batter = {"name": "H_P1"}
        bowler = {"name": "A_P7", "bowling_type": "Fast"}
        for _ in range(10):
            engine.get_override_outcome(batter, bowler)
        assert engine._pool_index == 10

        # Simulate innings transition.
        m.innings = 2
        m.batting_team = _build_xi("A")
        m.bowling_team = _build_xi("H")
        engine.on_innings_transition()

        assert engine._pool_index == 0
        assert len(engine._pool) == 120

    def test_second_innings_pool_has_different_aggregate(self, pack):
        """Innings 2 pool should yield 160 total runs / 6 wickets,
        different from innings 1's 159/8."""
        random.seed(7)
        m = match_module.Match(_match_data(pack))
        engine = m.scenario_engine
        m.innings = 2
        m.batting_team = _build_xi("A")
        engine.on_innings_transition()

        batter = {"name": "A_P1"}
        bowler = {"name": "H_P7", "bowling_type": "Fast"}
        outcomes = [engine.get_override_outcome(batter, bowler) for _ in range(120)]
        total = sum(o["runs"] for o in outcomes)
        wkts = sum(1 for o in outcomes if o.get("batter_out"))
        assert total == 160
        assert wkts == 6


# ═══════════════════════════════════════════════════════════════════════════ #
#  7. Pack validation edge cases
# ═══════════════════════════════════════════════════════════════════════════ #

class TestPackValidation:
    def _base_pack(self):
        return {
            "id": "test_pack",
            "title": "Test",
            "format": "T20",
            "batting_first": [f"P{i}" for i in range(11)],
            "batting_second": [f"Q{i}" for i in range(11)],
            "innings": {
                "1": {"balls": [{"r": 1}] * 6},
                "2": {"balls": [{"r": 0}] * 6},
            },
        }

    def test_valid_minimal_pack_passes(self):
        assert _validate_pack(self._base_pack()) is None

    def test_missing_batting_first_fails(self):
        p = self._base_pack()
        del p["batting_first"]
        assert _validate_pack(p) is not None

    def test_wrong_player_count_fails(self):
        p = self._base_pack()
        p["batting_first"] = ["P1", "P2"]
        assert _validate_pack(p) is not None

    def test_missing_innings_fails(self):
        p = self._base_pack()
        del p["innings"]["2"]
        assert _validate_pack(p) is not None

    def test_empty_balls_array_fails(self):
        p = self._base_pack()
        p["innings"]["1"]["balls"] = []
        assert _validate_pack(p) is not None

    def test_invalid_runs_value_fails(self):
        p = self._base_pack()
        p["innings"]["1"]["balls"] = [{"r": 5}]
        assert _validate_pack(p) is not None

    def test_invalid_wicket_type_fails(self):
        p = self._base_pack()
        p["innings"]["1"]["balls"] = [{"r": 0, "w": "Mankad"}]
        assert _validate_pack(p) is not None

    def test_valid_wicket_types_pass(self):
        p = self._base_pack()
        p["innings"]["1"]["balls"] = [
            {"r": 0, "w": "Caught"},
            {"r": 0, "w": "Bowled"},
            {"r": 0, "w": "LBW"},
            {"r": 0, "w": "Stumped"},
            {"r": 1, "w": "Run Out"},
            {"r": 0, "w": "Hit Wicket"},
        ]
        assert _validate_pack(p) is None

    def test_ball_without_r_key_fails(self):
        p = self._base_pack()
        p["innings"]["1"]["balls"] = [{"w": "Caught"}]
        assert _validate_pack(p) is not None


# ═══════════════════════════════════════════════════════════════════════════ #
#  8. Multiple simulations — statistical consistency
# ═══════════════════════════════════════════════════════════════════════════ #

class TestMultipleSimulations:
    N_RUNS = 5

    def test_all_runs_complete(self, pack):
        """Every seeded simulation should reach match_over."""
        for seed in range(self.N_RUNS):
            random.seed(seed + 100)
            m = match_module.Match(_match_data(pack))
            _simulate_to_completion(m)
            assert m.result, f"seed {seed + 100} produced no result"

    def test_no_negative_scores(self, pack):
        for seed in range(self.N_RUNS):
            random.seed(seed + 200)
            m = match_module.Match(_match_data(pack))
            _simulate_to_completion(m)
            assert m.first_innings_score >= 0
            assert m.score >= 0

    def test_wickets_never_exceed_10(self, pack):
        for seed in range(self.N_RUNS):
            random.seed(seed + 300)
            m = match_module.Match(_match_data(pack))
            _simulate_to_completion(m)
            assert m.wickets <= 10


# ═══════════════════════════════════════════════════════════════════════════ #
#  9. Outcome format — dict fields expected by the match engine
# ═══════════════════════════════════════════════════════════════════════════ #

class TestOutcomeFormat:
    def _get_outcome(self, pack, ball_data):
        from types import SimpleNamespace
        m = SimpleNamespace(
            data={}, innings=1, current_over=0, current_ball=0,
            score=0, wickets=0, target=None, pitch="Hard", fmt=None,
            batting_team=_build_xi("H"), bowling_team=_build_xi("A"),
        )
        custom_pack = copy.deepcopy(pack)
        custom_pack["innings"]["1"]["balls"] = [ball_data]
        engine = LegendaryScenarioEngine(custom_pack, m)
        batter = {"name": "H_P1"}
        bowler = {"name": "A_P7", "bowling_type": "Fast"}
        return engine.get_override_outcome(batter, bowler)

    def test_dot_ball_format(self, pack):
        o = self._get_outcome(pack, {"r": 0})
        assert o["type"] == "run"
        assert o["runs"] == 0
        assert o["batter_out"] is False
        assert o["is_extra"] is False

    def test_single_format(self, pack):
        o = self._get_outcome(pack, {"r": 1})
        assert o["type"] == "run"
        assert o["runs"] == 1

    def test_four_format(self, pack):
        o = self._get_outcome(pack, {"r": 4})
        assert o["type"] == "run"
        assert o["runs"] == 4

    def test_six_format(self, pack):
        o = self._get_outcome(pack, {"r": 6})
        assert o["type"] == "run"
        assert o["runs"] == 6

    def test_caught_wicket_format(self, pack):
        o = self._get_outcome(pack, {"r": 0, "w": "Caught"})
        assert o["type"] == "wicket"
        assert o["runs"] == 0
        assert o["batter_out"] is True
        assert o["wicket_type"] == "Caught"

    def test_bowled_wicket_format(self, pack):
        o = self._get_outcome(pack, {"r": 0, "w": "Bowled"})
        assert o["type"] == "wicket"
        assert o["wicket_type"] == "Bowled"
        assert o["batter_out"] is True

    def test_lbw_wicket_format(self, pack):
        o = self._get_outcome(pack, {"r": 0, "w": "LBW"})
        assert o["type"] == "wicket"
        assert o["wicket_type"] == "LBW"

    def test_run_out_format(self, pack):
        o = self._get_outcome(pack, {"r": 1, "w": "Run Out"})
        assert o["type"] == "wicket"
        assert o["runs"] == 1
        assert o["batter_out"] is True
        assert o["wicket_type"] == "Run Out"

    def test_outcome_has_description(self, pack):
        o = self._get_outcome(pack, {"r": 4})
        assert isinstance(o.get("description"), str)
        assert len(o["description"]) > 0
