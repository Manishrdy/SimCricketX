"""Whole-match checks. Run the slow cohort suite for release calibration.

FC_CALIBRATION_REPORT_DIR optionally supplies reports from bench_fc.py's
current run, avoiding duplicate simulations when validating a release batch.
The test still checks the cohort inputs and seed range before using it.
"""

import json
import os
from pathlib import Path

import pytest

from scripts import bench_fc
from scripts.fc_metrics import distribution, validate_batch


def test_benchmark_never_silently_discards_noncompletion():
    with pytest.raises(RuntimeError, match="did not finish"):
        bench_fc._simulate_match("Hard", 1, limit=0)


def test_distribution_keeps_tails_visible():
    stats = distribution([100, 200, 300, 400, 900])
    assert stats["median"] == 300
    assert stats["p90"] == 700
    assert stats["mean"] == 380
    assert distribution([])["n"] == 0


@pytest.mark.slow
@pytest.mark.parametrize("days", [4, 5])
@pytest.mark.parametrize("tier", ["standard", "elite"])
@pytest.mark.parametrize("pitch", bench_fc.PITCHES)
def test_fc_full_match_distribution(days, tier, pitch, monkeypatch):
    report_dir = os.environ.get("FC_CALIBRATION_REPORT_DIR")
    if report_dir:
        word = "four" if days == 4 else "five"
        path = Path(report_dir) / f"fc-final-{word}-{tier}.csv.json"
        report = json.loads(path.read_text())
        assert (
            report["metadata"]["engine_source_hash"] == bench_fc.source_hash()
        ), "stale engine report"
        matches = [m for m in report["matches"] if m["inputs"]["pitch"] == pitch]
        # JSON changes integer map keys; the metrics validator does not
        # assume those keys retain their Python type.
        for match in matches:
            match["innings"] = {int(k): v for k, v in match["innings"].items()}
    else:
        monkeypatch.setattr(bench_fc, "HOME", bench_fc._squad("HOM", tier))
        monkeypatch.setattr(bench_fc, "AWAY", bench_fc._squad("AWY", tier))
        matches = [
            bench_fc._simulate_match(pitch, seed, days=days) for seed in range(1, 41)
        ]
    assert [m["inputs"]["seed"] for m in matches] == list(range(1, 41))
    for m in matches:
        assert m["inputs"]["days"] == days
        assert m["inputs"]["squads"]["home"] == bench_fc._squad("HOM", tier)
        assert m["inputs"]["squads"]["away"] == bench_fc._squad("AWY", tier)
    validate_batch(matches)
