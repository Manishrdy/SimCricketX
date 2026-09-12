"""Fit the FC rate/risk frontier from scripts/fc_sweep.py's dataset.

Weighted least squares on log rates. This is curve fitting against our own
ball engine, not machine learning: the design matrix is fixed and readable,
the solve is numpy.linalg.lstsq, and the output is a small coefficient table
checked into the repo at engine/data/fc_frontier.json.

Run from project root:  python scripts/fc_fit_frontier.py
"""
import argparse
import hashlib
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PITCHES = ["Green", "Dry", "Hard", "Flat", "Dead"]
N_AGE_BUCKETS = 4

# Cells thinner than this are mostly noise in log space; the weighting would
# mostly ignore them anyway, and dropping them keeps the fit diagnostics
# honest about what was actually used.
MIN_BALLS = 30


def design_row(cell):
    """Readable, fixed design matrix.

    Multiplicative in the engine's own style, so the fit is linear in log
    space. Pitch x aggression interactions are not optional: the sweep shows
    the wicket cost of attacking is roughly 2.7x on Green and 1.2x on Dead,
    and a shared slope cannot represent both.
    """
    pitch = cell["pitch"]
    aggression = cell["aggression"]
    row = [1.0 if pitch == p else 0.0 for p in PITCHES]
    row += [aggression if pitch == p else 0.0 for p in PITCHES]
    row.append(cell["wear"])
    row.append(cell["strength_diff"] / 100.0)
    # Ball-age bucket 0 (the new ball) is the reference level. The effect is
    # not monotone in age — new ball and reversing old ball are both
    # dangerous, the middle is quiet — so dummies, not a linear term.
    row += [1.0 if cell["ball_age_bucket"] == b else 0.0
            for b in range(1, N_AGE_BUCKETS)]
    return row


FEATURE_NAMES = (
    [f"pitch[{p}]" for p in PITCHES]
    + [f"aggression[{p}]" for p in PITCHES]
    + ["wear", "strength_diff_per_100"]
    + [f"ball_age[{b}]" for b in range(1, N_AGE_BUCKETS)]
)


def weighted_fit(rows, targets, weights):
    """Weighted least squares, returning coefficients and weighted R^2."""
    matrix = np.asarray(rows, dtype=float)
    target = np.asarray(targets, dtype=float)
    weight = np.sqrt(np.asarray(weights, dtype=float))
    coefficients, *_ = np.linalg.lstsq(matrix * weight[:, None],
                                       target * weight, rcond=None)
    predicted = matrix @ coefficients
    residual = np.sum(weight**2 * (target - predicted) ** 2)
    mean = np.sum(weight**2 * target) / np.sum(weight**2)
    total = np.sum(weight**2 * (target - mean) ** 2)
    return coefficients, 1.0 - residual / total if total else 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="reports/fc_frontier_dataset.json")
    parser.add_argument("--out", default="engine/data/fc_frontier.json")
    args = parser.parse_args()

    raw = open(args.dataset, "rb").read()
    dataset = json.loads(raw)
    cells = [c for c in dataset["cells"] if c["legal_balls"] >= MIN_BALLS]
    if not cells:
        raise SystemExit("no cells survived the minimum-sample filter")

    rows = [design_row(c) for c in cells]
    weights = [c["legal_balls"] for c in cells]
    overs = [c["legal_balls"] / 6.0 for c in cells]

    rate_targets = [math.log(max(c["runs"], 0.5) / o) for c, o in zip(cells, overs)]
    # Continuity correction keeps wicketless cells in the fit instead of
    # discarding exactly the quiet passages the model needs to represent.
    wicket_targets = [math.log((c["wickets"] + 0.5) / o) for c, o in zip(cells, overs)]

    rate_coefficients, rate_r2 = weighted_fit(rows, rate_targets, weights)
    wicket_coefficients, wicket_r2 = weighted_fit(rows, wicket_targets, weights)

    model = dict(
        features=FEATURE_NAMES,
        pitches=PITCHES,
        n_age_buckets=N_AGE_BUCKETS,
        ball_age_edges=dataset["metadata"]["ball_age_edges"],
        log_rate=dict(zip(FEATURE_NAMES, rate_coefficients.tolist())),
        log_wicket_rate=dict(zip(FEATURE_NAMES, wicket_coefficients.tolist())),
        fit=dict(cells_used=len(cells), cells_total=len(dataset["cells"]),
                 legal_balls=sum(weights),
                 weighted_r2_rate=rate_r2, weighted_r2_wicket_rate=wicket_r2),
        provenance=dict(
            engine_source_hash=dataset["metadata"]["engine_source_hash"],
            dataset_sha256=hashlib.sha256(raw).hexdigest(),
            dataset_path=args.dataset),
    )

    path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(model, handle, indent=2, sort_keys=True)

    print(f"Wrote {path}")
    print(f"cells used {len(cells)}/{len(dataset['cells'])}  "
          f"balls {sum(weights)}")
    print(f"weighted R^2  rate {rate_r2:.3f}   wicket rate {wicket_r2:.3f}\n")
    print(f"{'feature':<26} {'log rate':>10} {'log wkt rate':>13}")
    for name, a, b in zip(FEATURE_NAMES, rate_coefficients, wicket_coefficients):
        print(f"{name:<26} {a:>10.4f} {b:>13.4f}")

    print(f"\n{'pitch':<7} {'aggr':>5} {'RPO':>7} {'w/over':>8}   (wear 0.3, even squads, old ball)")
    for pitch in PITCHES:
        for aggression in (-1.0, 0.0, 1.0):
            cell = dict(pitch=pitch, aggression=aggression, wear=0.3,
                        strength_diff=0, ball_age_bucket=2)
            row = np.asarray(design_row(cell))
            print(f"{pitch:<7} {aggression:>+5.1f} "
                  f"{math.exp(row @ rate_coefficients):>7.2f} "
                  f"{math.exp(row @ wicket_coefficients):>8.3f}")


if __name__ == "__main__":
    main()
