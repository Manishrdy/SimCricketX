"""
Story mode (historical scenario arcs with user-selected teams).

Covers:
1. Story pack loading + validation (engine/scenario_packs.py), including the
   structural rules _validate_pack enforces on beats/checkpoints
2. HistoricalScenarioEngine beat steering: bias direction, corridor
   resolution (par_pct / score_pct), innings transition, open ending past the
   last beat
3. create_scenario_engine factory dispatch
4. Routes: gallery page, /match/setup with story_id (valid, unknown,
   format-mismatched)
5. Engine smoke: story matches with generic user-style teams simulate to
   completion with the steering attached, across several seeds
"""
import io
import json
import os
import re
import sys
import contextlib
import random
import uuid
from types import SimpleNamespace

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.scenario_packs as scenario_packs
from engine.scenario_packs import _validate_pack, get_scenario_pack, list_scenario_packs
from engine.scenario_engine import (
    HistoricalScenarioEngine,
    ScenarioEngine,
    create_scenario_engine,
)

PACK_ID = "ind_pak_t20wc_2022"


# ==================== Pack loading ====================

class TestScenarioPacks:
    def test_flagship_pack_loads(self):
        pack = get_scenario_pack(PACK_ID)
        assert pack is not None
        assert pack["id"] == PACK_ID
        assert pack["format"] == "T20"
        assert "1" in pack["beats"] and "2" in pack["beats"]

    def test_flagship_pack_is_team_agnostic(self):
        pack = get_scenario_pack(PACK_ID)
        assert "teams" not in pack
        # Innings-1 corridors must adapt to any pitch via par_pct.
        assert all("par_pct" in cp for cp in pack["beats"]["1"])
        # Innings-2 corridors scale with whatever target the chase gets.
        assert all("score_pct" in cp for cp in pack["beats"]["2"])

    def test_unknown_or_malicious_pack_id_returns_none(self):
        assert get_scenario_pack("does_not_exist") is None
        assert get_scenario_pack("../../etc/passwd") is None
        assert get_scenario_pack(None) is None

    def test_list_includes_flagship(self):
        ids = [p["id"] for p in list_scenario_packs()]
        assert PACK_ID in ids


# ==================== Pack validation ====================

def _minimal_pack():
    return {
        "id": "test_pack",
        "title": "Test",
        "format": "T20",
        "beats": {
            "1": [{"at_over": 10, "par_pct": [0.7, 1.0], "wickets": [2, 4]}],
            "2": [{"at_over": 12, "score_pct": [0.4, 0.55]}],
        },
    }


class TestPackValidation:
    """_validate_pack is the gate every pack passes through on load, so its
    rejection rules are tested directly rather than only via the flagship."""

    def test_valid_minimal_pack_passes(self):
        assert _validate_pack(_minimal_pack()) is None

    @pytest.mark.parametrize("key", ["id", "title", "format", "beats"])
    def test_missing_required_key_fails(self, key):
        p = _minimal_pack()
        del p[key]
        assert _validate_pack(p) == f"missing key '{key}'"

    @pytest.mark.parametrize("bad_id", ["../../etc/passwd", "Bad_Id", "has space", ""])
    def test_invalid_pack_id_fails(self, bad_id):
        p = _minimal_pack()
        p["id"] = bad_id
        assert _validate_pack(p) == "invalid pack id"

    def test_beats_key_outside_1_and_2_fails(self):
        p = _minimal_pack()
        p["beats"]["3"] = [{"at_over": 5, "score": [20, 40]}]
        assert _validate_pack(p) is not None

    def test_non_increasing_at_over_fails(self):
        p = _minimal_pack()
        p["beats"]["1"] = [
            {"at_over": 10, "par_pct": [0.7, 1.0]},
            {"at_over": 6, "par_pct": [0.8, 1.1]},
        ]
        assert _validate_pack(p) is not None

    def test_non_integer_at_over_fails(self):
        p = _minimal_pack()
        p["beats"]["1"] = [{"at_over": "10", "par_pct": [0.7, 1.0]}]
        assert _validate_pack(p) is not None

    def test_at_over_zero_fails(self):
        # Checkpoints start counting above over 0 — a beat at the very first
        # ball would have no trajectory to steer.
        p = _minimal_pack()
        p["beats"]["1"] = [{"at_over": 0, "par_pct": [0.7, 1.0]}]
        assert _validate_pack(p) is not None

    def test_checkpoint_without_corridor_fails(self):
        p = _minimal_pack()
        p["beats"]["1"] = [{"at_over": 10, "wickets": [2, 4]}]
        assert _validate_pack(p) is not None

    @pytest.mark.parametrize(
        "corridor", [{"score": [60, 80]}, {"score_pct": [0.4, 0.55]}, {"par_pct": [0.7, 1.0]}]
    )
    def test_every_corridor_form_is_accepted(self, corridor):
        p = _minimal_pack()
        p["beats"]["1"] = [dict({"at_over": 10}, **corridor)]
        assert _validate_pack(p) is None

    def test_invalid_pack_on_disk_is_not_loaded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scenario_packs, "SCENARIO_DIR", str(tmp_path))
        broken = _minimal_pack()
        del broken["beats"]
        (tmp_path / "broken.json").write_text(json.dumps(broken))
        (tmp_path / "malformed.json").write_text("{not json")

        assert get_scenario_pack("broken") is None
        assert get_scenario_pack("malformed") is None
        assert list_scenario_packs() == []


# ==================== Engine steering ====================

def _stub_match(innings=1, over=0, ball=0, score=0, wickets=0, target=None,
                pitch="Hard"):
    return SimpleNamespace(
        data={},
        innings=innings,
        current_over=over,
        current_ball=ball,
        score=score,
        wickets=wickets,
        target=target,
        pitch=pitch,
        fmt=None,  # get_par_score falls back to the built-in T20 curve
    )


@pytest.fixture()
def pack():
    return get_scenario_pack(PACK_ID)


class TestHistoricalScenarioEngine:
    def test_never_scripts_outcomes(self, pack):
        engine = HistoricalScenarioEngine(pack, _stub_match(innings=2, over=19, ball=5))
        assert engine.get_override_outcome({"name": "A"}, {"name": "B"}) is None

    def test_par_pct_resolves_against_pitch_par(self, pack):
        # Innings-1 beat at over 10: par_pct [0.68, 0.99] of par.
        # T20 par at over 10 is 81 on Hard (factor 1.0) and 98.8 on Dead (1.22),
        # so the same story demands more runs on a flatter deck.
        cp = next(c for c in pack["beats"]["1"] if c["at_over"] == 10)
        hard = HistoricalScenarioEngine(pack, _stub_match(pitch="Hard"))._resolve_score_range(cp)
        dead = HistoricalScenarioEngine(pack, _stub_match(pitch="Dead"))._resolve_score_range(cp)
        assert hard is not None and dead is not None
        assert dead[0] > hard[0] and dead[1] > hard[1]
        assert abs(hard[0] - 0.68 * 81.0) < 0.01

    def test_boosts_scoring_when_behind_corridor(self, pack):
        # Innings 1 on Hard, over 8: corridor at over 10 ≈ [55, 80]. 20/2
        # projects ~25 — way behind.
        m = _stub_match(innings=1, over=8, ball=0, score=20, wickets=2)
        bias = HistoricalScenarioEngine(pack, m).get_scenario_bias({})
        assert bias.get("boundary_modifier", 1) > 1

    def test_suppresses_scoring_when_ahead_of_corridor(self, pack):
        # Innings 1 on Hard, over 8: 90/2 projects ~112 at over 10 — far above.
        m = _stub_match(innings=1, over=8, ball=0, score=90, wickets=2)
        bias = HistoricalScenarioEngine(pack, m).get_scenario_bias({})
        assert bias.get("boundary_modifier", 1) < 1

    def test_protects_batters_when_too_many_wickets(self, pack):
        # Innings 1, over 8: corridor ceiling at over 10 is 4 wickets; 6 down.
        m = _stub_match(innings=1, over=8, ball=0, score=55, wickets=6)
        bias = HistoricalScenarioEngine(pack, m).get_scenario_bias({})
        assert bias.get("wicket_modifier", 1) < 1

    def test_raises_wicket_pressure_when_collapse_is_owed(self, pack):
        # Innings 2 chasing 160, over 5: beat at over 7 wants [3, 4] wickets; 1 down.
        m = _stub_match(innings=2, over=5, ball=0, score=25, wickets=1, target=160)
        bias = HistoricalScenarioEngine(pack, m).get_scenario_bias({})
        assert bias.get("wicket_modifier", 1) > 1

    def test_score_pct_corridor_resolves_against_target(self, pack):
        # Chasing 160 at over 5 with 10/3: over-7 corridor is
        # [0.17, 0.27] * 160 = [27.2, 43.2]; 10/3 projects ~14 — behind, so boost.
        m = _stub_match(innings=2, over=5, ball=0, score=10, wickets=3, target=160)
        bias = HistoricalScenarioEngine(pack, m).get_scenario_bias({})
        assert bias.get("boundary_modifier", 1) > 1

    def test_fully_open_ending_past_last_beat(self, pack):
        # Last innings-2 beat is at over 17 — from there on, zero interference.
        m = _stub_match(innings=2, over=18, ball=2, score=140, wickets=5, target=160)
        engine = HistoricalScenarioEngine(pack, m)
        assert engine.get_scenario_bias({}) == {}
        assert engine.get_phase() == "free_play"

    def test_innings_transition_switches_to_second_innings_beats(self, pack):
        # Innings 1's last beat is at over 20, so a first-innings engine at
        # over 5 is steering toward a par_pct corridor. After the transition
        # the same engine must read innings 2's score_pct beats instead.
        m = _stub_match(innings=1, over=5, ball=0, score=40, wickets=1)
        engine = HistoricalScenarioEngine(pack, m)
        cp, _ = engine._next_checkpoint(30)
        assert "par_pct" in cp

        m.innings = 2
        m.target = 160
        m.score = 0
        m.wickets = 0
        m.current_over = 5
        engine.on_innings_transition()

        cp, cp_balls = engine._next_checkpoint(30)
        assert "score_pct" in cp
        assert cp_balls == 7 * 6

    def test_steers_first_innings_flag(self, pack):
        assert HistoricalScenarioEngine(pack, _stub_match()).steers_first_innings is True
        assert getattr(ScenarioEngine("last_ball_six", _stub_match()), "steers_first_innings", False) is False


class TestFactory:
    def test_classic_modes_get_scripted_engine(self):
        engine = create_scenario_engine("last_ball_six", _stub_match())
        assert isinstance(engine, ScenarioEngine)

    def test_historical_mode_gets_beat_engine(self):
        engine = create_scenario_engine(f"historical:{PACK_ID}", _stub_match())
        assert isinstance(engine, HistoricalScenarioEngine)

    def test_unresolvable_historical_pack_returns_none(self):
        assert create_scenario_engine("historical:nope", _stub_match()) is None

    def test_embedded_pack_takes_precedence(self, pack):
        m = _stub_match()
        m.data = {"scenario_pack": pack}
        engine = create_scenario_engine("historical:whatever", m)
        assert isinstance(engine, HistoricalScenarioEngine)
        assert engine.pack["id"] == PACK_ID


# ==================== Routes ====================

def _setup_payload(team_a, team_b, story_id=None):
    payload = {
        "team_home": team_a.id,
        "team_away": team_b.id,
        "stadium": "Test Ground",
        "pitch": "Hard",
        "toss": "Heads",
        "toss_winner": team_a.short_code,
        "toss_decision": "Bat",
        "simulation_mode": "auto",
        "match_format": "T20",
        "make_match_interesting": False,
    }
    if story_id is not None:
        payload["story_id"] = story_id
    return payload


class TestStoryRoutes:
    def test_gallery_requires_login(self, client):
        resp = client.get("/scenarios")
        assert resp.status_code in (301, 302)

    def test_gallery_lists_flagship_and_links_to_setup(self, authenticated_client):
        resp = authenticated_client.get("/scenarios")
        assert resp.status_code == 200
        assert b"The Kohli Chase" in resp.data
        assert f"/match/setup?story={PACK_ID}".encode() in resp.data

    def test_setup_page_story_picker_is_disabled(self, authenticated_client):
        """Story Mode is switched off in the UI for now. The picker still
        renders, but disabled, so no one can start a story match by accident."""
        resp = authenticated_client.get("/match/setup")
        assert resp.status_code == 200
        assert b'<select id="story-select" disabled>' in resp.data

    def test_disabled_picker_ignores_story_query_param(self, authenticated_client):
        """A disabled <select> still reports whatever option is marked selected,
        so /match/setup?story=<id> must not preselect one — otherwise the page
        would post a story_id and start a story match while the control looks
        switched off."""
        resp = authenticated_client.get(f"/match/setup?story={PACK_ID}")
        assert resp.status_code == 200
        block = re.search(
            r'<select id="story-select" disabled>(.*?)</select>',
            resp.data.decode(), re.DOTALL,
        )
        assert block, "story picker missing or no longer disabled"
        options = block.group(1)
        assert f'value="{PACK_ID}"' in options, "flagship story option not rendered"
        # The empty "unavailable" option is the only preselected one.
        assert options.count("selected") == 1
        assert '<option value="" selected>' in options

    def test_home_story_mode_tile_is_disabled(self, authenticated_client):
        resp = authenticated_client.get("/")
        assert resp.status_code == 200
        assert b"Story Mode" in resp.data
        assert b'class="menu-item is-disabled"' in resp.data
        # Not a link any more — unclickable and unreachable by keyboard.
        assert b'href="/scenarios"' not in resp.data

    def test_setup_with_null_story_id_is_a_normal_match(self, authenticated_client,
                                                        test_team, test_team_2, app):
        """With the picker gone the page still posts story_id: null on every
        setup, so an explicit null must read as 'no story', not as an error."""
        payload = _setup_payload(test_team, test_team_2)
        payload["story_id"] = None
        resp = authenticated_client.post("/match/setup", json=payload)
        assert resp.status_code == 200, resp.get_json()

        from app import PROJECT_ROOT
        path = os.path.join(PROJECT_ROOT, "data", "matches",
                            f"match_{resp.get_json()['match_id']}.json")
        with open(path) as f:
            match_data = json.load(f)
        assert match_data["scenario_mode"] is None
        assert "scenario_pack" not in match_data
        os.remove(path)

    def test_setup_with_story_wires_scenario(self, authenticated_client, test_team, test_team_2, app):
        resp = authenticated_client.post(
            "/match/setup",
            json=_setup_payload(test_team, test_team_2, story_id=PACK_ID),
        )
        assert resp.status_code == 200, resp.get_json()
        match_id = resp.get_json()["match_id"]

        from app import PROJECT_ROOT
        path = os.path.join(PROJECT_ROOT, "data", "matches", f"match_{match_id}.json")
        assert os.path.isfile(path)
        with open(path) as f:
            match_data = json.load(f)
        assert match_data["scenario_mode"] == f"historical:{PACK_ID}"
        assert match_data["scenario_pack"]["id"] == PACK_ID
        # The user's own teams, not provisioned historical squads.
        assert match_data["team_home"].startswith(test_team.short_code)
        os.remove(path)

    def test_setup_with_unknown_story_rejected(self, authenticated_client, test_team, test_team_2):
        resp = authenticated_client.post(
            "/match/setup",
            json=_setup_payload(test_team, test_team_2, story_id="not_a_story"),
        )
        assert resp.status_code == 400
        assert "story" in resp.get_json()["error"].lower()

    def test_setup_with_format_mismatched_story_rejected(self, authenticated_client, test_team, test_team_2):
        payload = _setup_payload(test_team, test_team_2, story_id=PACK_ID)
        payload["match_format"] = "ListA"
        resp = authenticated_client.post("/match/setup", json=payload)
        assert resp.status_code == 400

    def test_setup_without_story_unchanged(self, authenticated_client, test_team, test_team_2, app):
        resp = authenticated_client.post(
            "/match/setup",
            json=_setup_payload(test_team, test_team_2),
        )
        assert resp.status_code == 200
        match_id = resp.get_json()["match_id"]
        from app import PROJECT_ROOT
        path = os.path.join(PROJECT_ROOT, "data", "matches", f"match_{match_id}.json")
        with open(path) as f:
            match_data = json.load(f)
        assert match_data["scenario_mode"] is None
        assert "scenario_pack" not in match_data
        os.remove(path)


# ==================== Engine smoke ====================

def _build_xi(prefix):
    return [{
        "name": f"{prefix}_P{i+1}",
        "role": "Bowler" if i >= 6 else "Batsman",
        "batting_rating": 75 if i < 6 else 30,
        "bowling_rating": 80 if i >= 6 else 20,
        "fielding_rating": 70,
        "batting_hand": "Right", "bowling_type": "Medium", "bowling_hand": "Right",
        "will_bowl": i >= 6, "is_captain": i == 0,
    } for i in range(11)]


def _story_match_data(pack):
    return {
        "match_id": str(uuid.uuid4()), "created_by": "story@test",
        "timestamp": "20260612000000",
        "team_home": "AAA_story@test", "team_away": "BBB_story@test",
        "stadium": "Test Ground", "pitch": "Hard",
        "toss_winner": "AAA", "toss_decision": "Bat",
        "match_format": "T20", "simulation_mode": "auto",
        "playing_xi": {"home": _build_xi("A"), "away": _build_xi("B")},
        "substitutes": {"home": [], "away": []},
        "scenario_mode": f"historical:{PACK_ID}",
        "scenario_pack": pack,
        "rain_probability": 0.0,
    }


def _simulate_story_match(pack, seed, max_balls=1000):
    """Run a seeded story match to its end. Returns (match, last_outcome)."""
    import engine.match as match_module

    random.seed(seed)
    m = match_module.Match(_story_match_data(pack))
    outcome = None
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(max_balls):
            outcome = m.next_ball()
            if outcome.get("match_over"):
                break
    return m, outcome


class TestStoryMatchSmoke:
    def test_story_match_simulates_to_completion(self, pack):
        # Deterministic: an unseeded run can end in a tie, which parks the
        # match in the super-over flow (driven by separate routes).
        m, outcome = _simulate_story_match(pack, seed=7)
        assert isinstance(m.scenario_engine, HistoricalScenarioEngine)
        assert outcome and outcome.get("match_over"), "story match did not complete"
        assert m.result

    @pytest.mark.parametrize("seed", [100, 101, 102, 103, 104])
    def test_story_match_invariants_hold_across_seeds(self, pack, seed):
        """Beat steering pushes hard on boundary and wicket probabilities, so
        every seed must still land on a legal, resolved match — not a stalled
        innings or an impossible scoreline."""
        m, outcome = _simulate_story_match(pack, seed=seed)

        assert m.result, f"seed {seed} produced no result"
        # "Resolved" is the engine reporting match_over, or a tie parked in the
        # super over (innings 4), which reports no match_over of its own. Don't
        # assert on `innings` alone: not every completion path advances it, so
        # it isn't a reliable finished-marker.
        resolved = bool(outcome and outcome.get("match_over")) or m.innings == 4
        assert resolved, f"seed {seed} did not resolve (innings={m.innings})"
        assert m.first_innings_score is not None and m.first_innings_score >= 0
        assert m.score >= 0
        assert m.wickets <= 10

    def test_steering_produces_varied_endings(self, pack):
        """The story shapes the arc but never scripts it — different seeds must
        still reach different scorelines."""
        results = set()
        for seed in (7, 42, 99):
            m, _ = _simulate_story_match(pack, seed=seed)
            results.add((m.first_innings_score, m.score, m.result))
        assert len(results) >= 2
