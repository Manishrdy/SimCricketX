# T20 and List A psychological risk balancing

Implemented 2026-10-17. Scope: overlapping chase desperation and collapse
penalties. T10, Hundred, Super Overs and first-class retain their existing models.

## Composition

`engine/psychological_risk.py` owns the tuning constants. Each delivery has a
fresh `WicketRisk` ledger, shared between GSME and outcome sampling. Match passes
plain source factors alongside its pressure modifiers; those factors are removed
from the legacy product and composed exactly once. Inputs are not mutated, so
no-ball re-rolls can safely reuse the same pressure effects and game state.

Two groups:

- **Urgency:** required-rate aggression, chase pressure, risk-based acceleration,
  automatic required-rate-driven aggressive mode, and a chase cluster trigger
  when no recent wicket exists.
- **Collapse:** recent wickets, consecutive wickets, fresh-partnership anxiety,
  first-innings pressure, and cluster triggers with recent wickets.

Within a group, let `S` be the strongest positive multiplier and `O` the product
of the others. The combined multiplier is:

```
S * min(O, 1.25) * max(1, O / 1.25) ** 0.30
```

This preserves isolated factors and ordinary small overlaps at full strength;
only excess overlap is discounted. For example, 1.60 × 1.27 × 1.12 × 1.25 becomes
2.22 instead of 2.84. Separate groups still multiply. Protective factors remain
at full strength outside the ledger. Scoring rewards for aggression are retained.
Skill, conditions, individual settling-in, general momentum and other patterns
are not placed in these two groups.

The extreme-desperation ladder uses the same combiner *inside* the risk layer,
so that layer cannot multiply an already desperate chase by another full 2.5.
Death escalation is `0.3 + 0.4 * phase_progress`, measured in legal over fractions.
List A's longer death phase no longer gives it a larger escalation endpoint.

At four wickets down, two overs left and 20 required per over:

| Risk-layer wicket weight | Before | After |
|---|---:|---:|
| T20 | 8.875× | 5.463× |
| List A | 11.125× | 5.740× |

These are weights, not dismissal probabilities.

## Recovery and guardrails

Collapse shock is preserved for three wicket-free legal balls, then its excess
has a twelve-ball half-life. Rebuilding removes that initial grace period and
uses a six-ball half-life. A new wicket renews the shock. Byes/leg-byes advance
recovery; wides/no-balls do not. Recovery is derived from existing ball history,
so no additional mutable match state or save migration is required. Old history
entries without `is_legal` use their labels as a fallback.

Recovery applies to collapse wicket boosts and their accompanying scoring
suppression; it does not reduce established-partnership protection. A fresh
partnership is team anxiety, while the incoming batter's rating penalty still
uses that individual batter's balls faced.

After GSME and pressure, a soft limit compares wicket **odds** against the
skill/pitch/phase baseline. This accounts for non-wicket suppression as well as
wicket boosts. Below 4× baseline odds nothing changes; above it the uplift
smoothly approaches 7×. Only wicket mass is reduced; the ratios among non-wicket
outcomes are preserved. Scenario bias passes through this guardrail too; explicit
scripted outcome overrides retain their existing bypass.

A 40% wicket-category ceiling follows the remaining weight modifiers in both
innings. This is an emergency ceiling, not a target. It is applied before
fielding and free-hit resolution, so it is not a guarantee about the final
resolved dismissal distribution. The existing T10/Hundred ceiling remains 30%.

Debug logs expose source groups, their pooled multiplier, soft-limit activation
and ceiling activation.

## Validation of the initial psychological-risk change

The comparison baseline is commit `5a6ca98`. The reusable harness is:

```
.venv/bin/python scripts/bench_psychological_risk.py --seeds 40 --start-seed 5100
```

The same balanced XI and 40 seeds were run across five pitches and two formats:
400 full games before and 400 after. No database writes or network access.

| Format/pitch | First-innings runs before → after | Chase wins before → after |
|---|---:|---:|
| T20 Green | 139.1 → 138.7 | 52.5% → 57.5% |
| T20 Dry | 139.8 → 142.3 | 50.0% → 50.0% |
| T20 Hard | 194.6 → 197.8 | 50.0% → 50.0% |
| T20 Flat | 221.6 → 221.9 | 75.0% → 80.0% |
| T20 Dead | 260.9 → 262.3 | 100% → 100% |
| List A Green | 232.5 → 238.0 | 60.0% → 60.0% |
| List A Dry | 222.8 → 226.2 | 50.0% → 55.0% |
| List A Hard | 310.0 → 313.2 | 60.0% → 52.5% |
| List A Flat | 366.6 → 370.7 | 85.0% → 77.5% |
| List A Dead | 384.1 → 385.3 | 75.0% → 77.5% |

The new ceiling activated 3 times in 159,235 sampled deliveries (0.0019%), all
on List A Dry. Collapses remain possible: the mean additional wickets in a
completed twelve-ball recovery window ranged from 0.30 to 1.09 after the change,
versus 0.27 to 1.11 before. This statistic excludes windows censored by innings
end; the harness also reports all-outs and wicket-free recovery windows.

The 40-seed samples are diagnostic, not population estimates. They also expose
pre-existing chase advantages on batting pitches, especially T20 Dead. This
change addresses repeated psychological penalties, not all format calibration.

The separate sixteen-seed scoring suite still meets every independent pitch
run/wicket target. Two T20 behavior snapshots were re-pinned (Green and Dry)
because recovery is intended to change collapse-heavy innings. The seam-heavy
List A smoke fixture moved from 312.4 to 323.6 on eight seeds, so its coarse
upper bound is now 330; the balanced-attack Hard target remains 280–320 and
measures 314.2. No pitch configurations or authoritative target bands changed.

Regression coverage includes normalized extreme-state probabilities, overlap
composition, aggression's risk/reward, legal-ball recovery, rebuilding,
individual settling-in, automatic versus pinned modes, both-innings ceilings,
first-innings cluster single-roll behavior, short formats, Super Overs, historical
scenarios, reduced List A matches, free hits, byes and first-class calibration.

## Follow-up fixes in 3.0.2

Automatic Flat/Dead assistance in T20, T10 and Hundred now fades over six legal
deliveries after wicket three, and over the final six legal balls before its
existing phase cutoff. Further wickets do not restart the fade. The two tapers
use their minimum, so they cannot restore assistance when combined. The scoring
matrix blends toward the current natural/aggressive/defensive replacement; a
chase's aggressive replacement participates in urgency pooling throughout the
transition. User-pinned modes remain full-strength. List A does not use these
mode matrices.

Wides and no-balls without bat runs leave limited-overs batter form unchanged.
Actual bat runs on a no-ball earn the usual scoring-shot form increase. Byes
and leg-byes retain their legal-ball, zero-bat-run form treatment. FC retains
its separate handling.

The before/after benchmark table above describes the initial risk changes,
before these follow-ups; it is retained as historical calibration evidence.
