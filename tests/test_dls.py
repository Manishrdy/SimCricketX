"""
Unit tests for engine/dls.py — Duckworth-Lewis Standard Edition math.

Golden values come directly from the published D/L Standard Edition
over-by-over resource table.
"""

import math
import pytest

from engine.dls import (
    ResourceLedger,
    compute_target,
    g50_from_expected_total,
    overs_from_balls,
    par_score,
    resources_remaining,
)


# ── Resource table sanity ─────────────────────────────────────────────────────

class TestResourceTable:
    def test_published_anchor_values(self):
        # Straight off the published Standard Edition table.
        assert resources_remaining(50, 0) == 100.0
        assert resources_remaining(50, 2) == 85.1
        assert resources_remaining(50, 5) == 49.0
        assert resources_remaining(40, 0) == 89.3
        assert resources_remaining(30, 0) == 75.1
        assert resources_remaining(30, 4) == 54.1
        assert resources_remaining(20, 0) == 56.6   # T20 starting resources
        assert resources_remaining(20, 5) == 38.6
        assert resources_remaining(10, 0) == 32.1
        assert resources_remaining(10, 5) == 26.1
        assert resources_remaining(5, 0) == 17.2
        assert resources_remaining(1, 9) == 2.5

    def test_zero_boundaries(self):
        assert resources_remaining(0, 0) == 0.0
        assert resources_remaining(0, 9) == 0.0
        # All ten wickets down -> no resources regardless of overs.
        assert resources_remaining(50, 10) == 0.0
        assert resources_remaining(25, 12) == 0.0

    def test_clamping(self):
        assert resources_remaining(-3, 0) == 0.0
        assert resources_remaining(80, 0) == 100.0
        assert resources_remaining(10, -1) == resources_remaining(10, 0)

    def test_monotonic_in_overs(self):
        for w in range(10):
            col = [resources_remaining(u, w) for u in range(51)]
            assert all(a <= b for a, b in zip(col, col[1:])), f"wickets={w}"

    def test_monotonic_in_wickets(self):
        for u in range(1, 51):
            row = [resources_remaining(u, w) for w in range(10)]
            assert all(a >= b for a, b in zip(row, row[1:])), f"overs={u}"

    def test_ball_interpolation(self):
        # Midpoint between 19 (54.4) and 20 (56.6) at 0 wickets.
        assert resources_remaining(19.5, 0) == pytest.approx(55.5)
        # Interpolation stays within bracketing rows.
        v = resources_remaining(43.5, 3)
        assert resources_remaining(43, 3) <= v <= resources_remaining(44, 3)

    def test_overs_from_balls(self):
        assert overs_from_balls(120) == 20.0
        assert overs_from_balls(3) == 0.5
        assert overs_from_balls(-4) == 0.0


# ── Target computation ────────────────────────────────────────────────────────

class TestComputeTarget:
    def test_no_interruption_is_plain_target(self):
        assert compute_target(250, 100.0, 100.0) == 251
        assert compute_target(176, 56.6, 56.6) == 177

    def test_lista_chase_cut_to_43_overs_before_start(self):
        # Team 1: 274 in 50 (R1 = 100). Chase reduced to 43 overs before it
        # starts: R2 = R(43,0) = 92.8 -> floor(274 * 0.928) + 1 = 255.
        r2 = resources_remaining(43, 0)
        assert compute_target(274, 100.0, r2) == 255

    def test_t20_chase_cut_to_14_overs_before_start(self):
        # Team 1: 176 in 20 (R1 = R(20,0) = 56.6). Chase cut to 14 overs:
        # R2 = R(14,0) = 42.7 -> floor(176 * 42.7/56.6) + 1 = 133.
        r1 = resources_remaining(20, 0)
        r2 = resources_remaining(14, 0)
        assert compute_target(176, r1, r2) == 133

    def test_mid_chase_interruption_lista(self):
        # 50-over chase of 251 interrupted at 30 overs remaining, 2 wickets
        # down; resumes with 20 overs remaining.
        # Lost = R(30,2) - R(20,2) = 67.3 - 52.4 = 14.9 -> R2 = 85.1.
        ledger = ResourceLedger(50)
        lost = ledger.record_interruption(30, 2, 20)
        assert lost == pytest.approx(14.9)
        assert ledger.available() == pytest.approx(85.1)
        assert compute_target(250, 100.0, ledger.available()) == 213

    def test_team1_cut_short_uses_g50_branch(self):
        # Team 1 stopped outright at 20 overs remaining, 2 wickets down,
        # having made 180: R1 = 100 - R(20,2) = 100 - 52.4 = 47.6.
        # Team 2 gets its full (shortened) allocation of 30 overs:
        # R2 = R(30,0) = 75.1 > R1, so the G50 branch applies:
        # target = floor(180 + 245 * (75.1 - 47.6)/100) + 1 = floor(247.375) + 1 = 248.
        ledger1 = ResourceLedger(50)
        ledger1.record_termination(20, 2)
        r1 = ledger1.available()
        assert r1 == pytest.approx(47.6)
        r2 = resources_remaining(30, 0)
        assert compute_target(180, r1, r2, g50=245.0) == 248

    def test_g50_branch_requires_g50(self):
        with pytest.raises(ValueError):
            compute_target(180, 47.6, 75.1)

    def test_more_overs_lost_never_raises_target(self):
        targets = []
        for revised in range(50, 19, -1):
            r2 = resources_remaining(revised, 0)
            targets.append(compute_target(280, 100.0, r2))
        assert all(a >= b for a, b in zip(targets, targets[1:]))

    def test_target_never_below_one(self):
        assert compute_target(0, 100.0, 40.0) == 1

    def test_g50_scaling_from_format_totals(self):
        # A 50-over expected total is already on the 100% scale.
        assert g50_from_expected_total(245.0, 50) == pytest.approx(245.0)
        # A T20 expected total scales up by 100/56.6.
        assert g50_from_expected_total(165.0, 20) == pytest.approx(165.0 * 100.0 / 56.6)


# ── Par score ─────────────────────────────────────────────────────────────────

class TestParScore:
    def test_par_at_innings_start_is_zero(self):
        assert par_score(250, 100.0, 100.0, 50, 0) == 0

    def test_par_grows_with_resources_used(self):
        # Chasing 251 (S=250, full resources both sides). At 25 overs
        # remaining and 5 down: used = 100 - R(25,5) = 100 - 42.2 = 57.8.
        # Par = floor(250 * 0.578) = 144.
        assert par_score(250, 100.0, 100.0, 25, 5) == 144

    def test_par_with_shortened_chase(self):
        # Chase allocated 43 overs (R2 = 92.8). At 10 overs remaining and
        # 4 down: used = 92.8 - R(10,4) = 92.8 - 28.3 = 64.5.
        # Par = floor(274 * 0.645) = 176.
        r2 = resources_remaining(43, 0)
        assert par_score(274, 100.0, r2, 10, 4) == 176

    def test_par_at_full_resource_use_equals_score_ratio(self):
        # All resources used -> par = floor(S * R2/R1) = target - 1.
        r2 = resources_remaining(43, 0)
        target = compute_target(274, 100.0, r2)
        assert par_score(274, 100.0, r2, 0, 6) == target - 1


# ── Ledger persistence ────────────────────────────────────────────────────────

class TestResourceLedger:
    def test_multiple_interruptions_accumulate(self):
        ledger = ResourceLedger(50)
        ledger.record_interruption(40, 1, 35)   # 84.2 - 78.5 = 5.7
        ledger.record_interruption(20, 4, 15)   # 44.6 - 37.6 = 7.0
        assert ledger.lost == pytest.approx(12.7)
        assert ledger.available() == pytest.approx(87.3)
        assert len(ledger.interruptions) == 2

    def test_round_trip_serialization(self):
        ledger = ResourceLedger(20)
        ledger.record_interruption(10, 3, 6)
        restored = ResourceLedger.from_dict(ledger.to_dict())
        assert restored.scheduled_overs == 20
        assert restored.available() == pytest.approx(ledger.available())
        assert restored.interruptions == ledger.interruptions

    def test_t20_starting_resources(self):
        assert ResourceLedger(20).starting_resources == 56.6
        assert ResourceLedger(50).starting_resources == 100.0
