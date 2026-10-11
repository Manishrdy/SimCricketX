# Captaincy model 1

Captaincy belongs to the team's format profile, rather than the individual player.
The Hundred uses the T20 profile. Changing the captain keeps these settings.
Create Team and Manage Squad expose the same presets and seven integer ratings
(0–100, neutral default 50). Aggression sets risk preference; the other five
ratings improve decision quality. There is no player-rating bonus or points budget.

## Data and compatibility

`TeamProfile.captaincy_config` is an additive JSON column. The migration precheck
runs `migrations/add_captaincy.py` idempotently and backfills neutral configurations.
The shared parser rejects malformed attributes with field-specific messages;
explicit zero is valid. Existing clients that omit captaincy during squad edits
preserve the stored configuration.

New match creation freezes `captaincy_model_version: 1`, both configurations, and
captain attribution from the authoritative squad profiles. Later profile edits
cannot change that match. Matches without the model version keep legacy behavior,
including First-Class temperament-based declaration risk and random consumption.
Captain replacement changes attribution; an absent captain falls back to the
first active XI player.

## Policy and delivery effects

`engine/captaincy.py` owns the typed interfaces, scoring, adaptation, legality
filtering, state, tactical events, and model-1 outcome coefficients. Match remains
the orchestrator and existing format managers remain the eligibility authority.

The policy evaluates at innings start, over boundaries (five-ball sets in The
Hundred), wickets, target/length revisions, interruptions, phase transitions, and
First-Class condition boundaries. It reads observable score, conditions, fatigue,
matchups, and recent legal-ball performance. Its window is 18 balls, or 15 in The
Hundred. Adaptability controls score smoothing and the specified change threshold;
composure damps isolated evidence and pressure uncertainty. Non-forced changes
hold for at least one over/set. New-ball timing and declaration/follow-on inputs
reuse the existing First-Class mechanisms and forecast budget.

Batting intents are rebuild, balanced, attack, and all-out. Bowling plans are
contain, balanced, and attack. Situation dominates personality during collapses,
easy chases, and final-delivery urgency. Bowlers are scored inside the legal pool
using existing effective abilities, conditions, matchups, spell performance,
containment/strike preference, and late-phase reservations. Reservations yield
when completion requires a bowler. Every automatic selection goes through the
policy, including death phases and eligible shootout selections.

The specified batting and bowling coefficients transform boundary/wicket weights
once in the outcome layer. Combined coefficients are clamped to 0.75–1.30 and
non-extra mass is renormalized without changing extras probability. Existing
free-hit and format safeguards remain downstream. First-Class batting adjusts
its existing attack/survival intent instead of applying the limited-overs table.
Quality attributes do not multiply delivery weights.

Manual legal bowler selections remain authoritative. Advice is cached at its
boundary, so refreshing a prompt cannot reroll the recommendation. Status reads
are read-only. Committed events contain identifiers, attribution, innings,
legal-ball position, previous/current actions, trigger, reason, and evidence.
Match panels and commentary explain meaningful choices; archive JSON includes
the frozen settings and events.

Captain state, events, and the match random stream are included in the existing
First-Class, Hundred, and shootout checkpoints. Commentary generator state is
also restored so the next delivery's explanation remains consistent. This does
not introduce main-innings restart recovery for other formats.

## Verification and calibration

Automated coverage includes defaults/zero/invalid values; profile creation,
editing, copying and captain reassignment; authoritative frozen snapshots;
chases/collapses/urgency; quality selection accuracy; adaptation; legal complete
allocations; revised rain allocations; manual authority and cached advice;
First-Class risk and forecast budget; normalized weights and extras; and exact
next-delivery recovery for First-Class, Hundred, super overs and super fives.
JavaScript coverage includes format draft preservation and populated-squad draft
restoration. Browser checks used the shared QA account on an isolated temporary
database, including preset changes, zero ratings, format isolation, restored
squad drafts, and 390-pixel layouts with no horizontal overflow.

The aggregated seeded batting/bowling decision suite checks that strong quality
selects the scenario-preferred action more often than weak quality, with the
95% independent-proportions confidence interval excluding zero. Neutral quality
falls between the two. This is a decision-accuracy claim, not a win-rate claim.

640 neutral first innings (64 seeds per pitch/format) stayed in all ten existing
T20/List A scoring and wicket bands:

| Pitch | T20 runs / wickets | List A runs / wickets |
| --- | --- | --- |
| Green | 123.09 / 7.86 | 220.77 / 9.12 |
| Dry | 130.55 / 7.66 | 224.28 / 9.52 |
| Hard | 186.78 / 5.91 | 304.77 / 7.48 |
| Flat | 215.00 / 4.81 | 355.41 / 4.83 |
| Dead | 265.86 / 1.62 | 377.92 / 3.05 |

240 complete limited-overs matches (four formats, three pitches, five profiles,
four seeds) and 30 complete First-Class matches (three pitches, five profiles,
two seeds) completed without allocation deadlocks. Attacking/conservative
profiles produce different choices and outcomes without a universal winner.
These small full-match batches are smoke/balance checks, not statistically
powered win-rate estimates. The existing calibrated bands cover T20 and List A;
no new empirical calibration bands are claimed for the other formats.

Maximum measured policy-evaluation p95 was 0.073 ms on this machine, excluding
existing forecast work, below the 5 ms target. Timing depends on hardware.

Reproduce with:

```sh
.venv/bin/python -m pytest tests/test_captaincy.py tests/test_captaincy_routes.py --no-cov
node --test tests/js/*.test.cjs
.venv/bin/python scripts/bench_captaincy.py --seeds 64 --formats T20 ListA --profiles neutral --output /tmp/captaincy-neutral.json
.venv/bin/python scripts/bench_captaincy.py --seeds 4 --formats T10 T20 ListA Hundred --pitches Green Hard Dry --full-matches --output /tmp/captaincy-matches.json
.venv/bin/python scripts/bench_captaincy.py --seeds 2 --formats FC --pitches Green Hard Dry --full-matches --output /tmp/captaincy-fc.json
```

The benchmark creates no users, accesses no database, and writes no archives.

Verification runs: 44 captaincy tests passed, 284 selected engine/stadium/route
regression tests passed, 164 Hundred/T10/captaincy regression tests passed, and
153 JavaScript tests passed. These runs overlap; their counts must not be added.
`git diff --check` and compilation of the changed Python modules also passed.

The older `tests/test_team_routes.py` suite remains incompatible with the ongoing
stadium changes: its create forms omit the now-required stadium selection, and
an older edit expectation also fails. The run stopped at five failures (14 tests
passed before the stop). Captaincy route tests use valid stadium-aware forms and
pass. The stadium implementation and its contracts were preserved; no broader
claim that the entire repository suite passes is made.
