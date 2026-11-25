# Open-result match director — first increment

Implemented November 15, 2026. New matches with **Make Match Interesting**
use `adaptive_v1`: low-score survival across both innings. This is the first
increment of the approved plan, not the full catalogue of future storylines.

## Behaviour

The director never prescribes a score, wicket, winner, or last delivery. Every
ball goes through the existing simulation, including player skill, ground,
weather, fatigue, tactics, fielding, extras and free-hit rules.

The shared pitch/format par anchors both innings. Early support comes from
bowler-friendly conditions; after 15–35% of the innings, observed scoring
progressively replaces that prior. Scoring below 70–95% of par supports the
storyline. A high-scoring innings is released. The chase is assessed against
its actual target and current allocation rather than a predetermined finish.

Wickets lost and remaining batting depth encourage rebuilding and strike
rotation, at a boundary cost. Late first-innings play and chase urgency can
increase aggression, with increased wicket risk. Current batters and the
quality/quota of remaining bowlers influence urgency. Easy or implausible
chases receive no adjustments. The same conditions and rules apply to either
team; the scorecards are not mirrored.

The director is intentionally stateless and consumes no random draws. Its
inputs are live match state, so a restored state produces identical decisions.
The Hundred object-graph checkpoint registers the new director and rebinds its
match pointer; its exact next-delivery replay is tested. This change does not
add durable general-match checkpoint infrastructure to formats that previously
lacked it.

## Integration and limits

- Existing checkbox/request field retained; newly enabled matches choose
  `adaptive_v1`. Explicit legacy scenarios and saved legacy modes keep their
  previous engine. Historical story selection still takes precedence.
- First-Class is excluded. T10, T20, List A and Hundred use their legal-ball
  allocation. Current rain targets and revised allocations are read each time.
- The director uses its own phase rather than legacy convergence, preserving
  ordinary collapse behaviour.
- Final category probabilities can change by at most 12% relative to the
  ordinary distribution at the director layer. Extras retain their mass and
  zero-probability outcomes remain impossible. Steering is declined when it
  would amplify an already extreme psychological adjustment. Normal tactics,
  free-hit handling and existing wicket ceilings still apply afterward.
- Selection/innings transitions log at INFO; adjustments and fallback reasons
  log at DEBUG. No database migration or user/account changes.

## Validation

Run focused checks:

```sh
.venv/bin/python -m pytest -q tests/test_match_director.py tests/test_first_innings_scenario_phase.py tests/test_lista_40_conditions.py tests/test_historical_scenarios.py tests/test_free_hit_state_ordering.py tests/test_captaincy.py --no-cov
```

The regression suite covers both-innings operation, normal delivery generation,
free-hit survival, all-out ties, late/easy/impossible chases, resource sensitivity,
probability budgets, extras preservation, legacy/historical compatibility,
format-aware deadlines, and Hundred checkpoint continuation.

Reproduce synthetic comparisons without a database or user accounts:

```sh
.venv/bin/python scripts/benchmark_match_director.py --seeds 4
```

The comparison uses four formats, Green/Flat pitches, equal and stronger home
teams, both batting orders, and ordinary/legacy-last-ball-six/adaptive modes.
Four seeds produce 384 matches (128 per mode). These are calibration smoke
checks, not statistically significant evidence or real-world cricket targets.
Close means a tie, defence by at most 10 runs, or chase with at most two wickets
or six legal balls remaining. Ties stop at entry to the existing tiebreak flow.

## Later increments

Resource-driven defensive comebacks, anchor-and-tail finishes, varied chasing
finishes, and recovery-versus-recovery stories remain deferred as agreed.
Expand simulation samples and include more venue/weather profiles before
adjusting the initial conservative strength. Do not add quotas for ties or
close finishes, or make stronger teams lose their skill advantage to satisfy
an ending.

## Recorded results

Final focused run: **130 passed**, one warning. `git diff --check` passed.

| Mode | Matches | Mean first innings | Close finishes | Ties |
|---|---:|---:|---:|---:|
| ordinary | 128 | 187.53 | 37.5% | 0.0% |
| last_ball_six | 128 | 187.53 | 71.1% | 40.6% |
| adaptive_v1 | 128 | 187.42 | 39.1% | 0.0% |

ordinary, home rating bonus 0: home win rate 50.0%.

ordinary, home rating bonus 12: home win rate 65.6%.

adaptive_v1, home rating bonus 0: home win rate 50.0%.

adaptive_v1, home rating bonus 12: home win rate 65.6%.

The legacy comparison covers only the explicit last-ball-six scenario, not the old random mixture. Its high tie-entry rate is an observed legacy result; diagnosing it is outside this increment. The modest adaptive change is not evidence of a statistically significant increase in close matches.
