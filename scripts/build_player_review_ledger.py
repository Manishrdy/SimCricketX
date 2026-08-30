#!/usr/bin/env python3
"""Create a deterministic ledger so unreviewed players cannot enter final JSON."""

import argparse
import csv
import json
from pathlib import Path

from apply_manual_rating_overrides import MANUAL_RATINGS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    players = json.loads(args.input.read_text(encoding="utf-8"))
    fields = ["name", "role", "review_status", "review_basis"]
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for player in sorted(players, key=lambda item: item["name"].casefold()):
            reviewed = player["name"] in MANUAL_RATINGS
            writer.writerow({
                "name": player["name"],
                "role": player["role"],
                "review_status": "reviewed" if reviewed else "pending",
                "review_basis": "named manual profile" if reviewed else "",
            })
    print(json.dumps({
        "total": len(players),
        "reviewed": sum(player["name"] in MANUAL_RATINGS for player in players),
        "pending": sum(player["name"] not in MANUAL_RATINGS for player in players),
    }))


if __name__ == "__main__":
    main()
