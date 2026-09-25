# The Hundred

The played format is `Hundred` (displayed as **The Hundred**); squad and rating sources are `T20`. No Hundred player profiles are created. Existing matches retain six-ball defaults. The additive `add_hundred_metadata` migration stores format metadata without converting historical matches.

## Rule coverage checklist

Baseline: [ECB playing conditions effective 1 April 2026](https://resources.ecb.co.uk/ecb/document/2026/03/24/f8005182-b39c-4c25-83bd-1ffff0415b7a/The-Hundred-2026.pdf). The entries below describe simulator behavior, including its abstractions and exclusions.

| Clause | Status | Implementation |
| --- | --- | --- |
| 1.1–1.2 | Supported with stricter setup | Eleven distinct players and five designated bowlers; selected players and ratings are saved with the match. Reuses T20 profiles. Nine-/ten-player exceptions are excluded. |
| 1.3, 24 | Excluded | Concussion replacements, absence administration, and tactical Impact Player substitutions. |
| 2–10 | Inherited abstraction / excluded | Existing pitch and lighting models; physical equipment, ground preparation, officials and DRS are excluded. |
| 11–12 | Partial | Existing innings transitions and deterministic weather; real-time schedules, reserve days and penalties are excluded. |
| 12.5, 13 | Supported / approximation | Rain cuts occur at complete-set boundaries. Minimum chase allocation is 25 balls unless the result is already reached. Resources use the existing D/L Standard table through consistent ball conversion. This is a **D/L approximation**, not professional DLS. |
| 12.8 | Supported abstraction | One optional manual fielding timeout per innings after ball 25. Resumable pause with commentary; no wall-clock delay or performance bonus. Automatic play skips it. |
| 13.3 | Supported | Twenty-ball normal quota; reduced allocations, including remainder slots, use completion-safe eligibility shared by manual and automatic selection. Previous deliveries survive revisions. |
| 14–15 | Not applicable | No follow-on, declaration or forfeiture controls. |
| 16 | Supported with competition deviations | Friendly/league ties stand. Knockouts use repeated five-ball, two-wicket Super Fives without boundary countback. Unlimited rounds and replay for an abandoned knockout are custom competition choices, not ECB administration. |
| 16.9 | Supported | Four points for a win; two for a tie/no result. Standings reversal uses the same contributions. NRR uses five-ball units, rain-adjusted contributions and full allocation for all-out innings. |
| 17.1–17.4 | Supported | Five-ball sets, ten-ball end changes, maximum two consecutive sets, including across an end change. The captain decides after every set. |
| 18–23 | Inherited | Run scoring, boundaries, strike rotation, wides, byes, leg-byes and existing free-hit dismissal restrictions. Illegal deliveries do not consume allocation. No-ball is one penalty run, with free hit carried through illegal deliveries (21.4 and MCC Law 21.15). |
| 25, 27–39 | Inherited abstraction | Batting order, wicketkeeper, existing dismissal outcomes and fielding attribution. Physical field positions and appeals are not individually simulated. |
| 28.4 | Supported abstraction | Two outside during powerplay, five afterwards. Exact reduced-powerplay boundaries drive format phases even inside a set. |
| 26, 40–42; appendices | Excluded | Practice, timed-out administration, discipline, real-time penalties and competition administration. |

## Units and persistence

`balls_per_over` is retained as an internal compatibility field; for Hundred it denotes a five-ball **set**, never a fractional T20 over. Saved/live state also carries explicit legal balls, scheduled/revised limits, set position, bowling end, consecutive sets and `ECB-2026-men` rule version. Stats and innings termination derive from legal deliveries. Run-driven strike rotation remains separate from end changes.

Hundred checkpoints preserve object references, player snapshots, bowling quotas, pending manual decisions, timeout, weather ledger, free-hit state and the random stream. Restoring uses an explicit class whitelist. HTTP and WebSocket delivery share the same advancement path. The archive stores innings allocations and reversible NRR contributions.

## Simulator configuration

Hundred starts from a separate copy of T20 delivery settings. Its phases are balls 1–25, 26–80 and 81–100; the death phase is a simulator tuning choice. Ground conditions have an independent Hundred configuration.

Day/night dew affects only the chase, rising from ball 50 to ball 90 on the original scheduled clock. Initial coefficients are half List A's: extras +20%, wickets −7.5%, fours +5% at full intensity. Rain does not compress the ramp. The night toss preference is bounded at 65%; daytime behavior retains the existing pitch model. These are simulation assumptions, independent of rain-result eligibility.

## Statistics

Match scorecards, not the source squad profile, define the statistical format. Hundred skips writes to shared T20 career counters. Super Fives remain marked separately and are excluded from regular statistics. Hundred tournament views aggregate authoritative scorecard rows; the cache also stores the correct Hundred economy, but is bypassed because its schema lacks some dedicated totals.

Bowling economy is `runs conceded × 100 / legal balls` (Econ/100); live scoring rates are runs per ball; NRR is runs per five balls. Comparisons normalize economy to runs per six balls. Undefined rates are displayed as an em dash. CSV/TXT exports identify format and units. See [ACS guidance](https://acscricket.com/wp-content/uploads/The-Hundred-ACS-Guidance.pdf).

## Validation

`tests/test_hundred.py` covers all reduced lengths, completion-safe allocations, set/end transitions, manual continuation, illegal-ball counts, timeout, deterministic checkpoints, late rain, day/night dew and seven tied Super Fives. Fifty seeded matches cover five pitches in both lighting modes. `tests/test_hundred_integration.py` uses shared QA identities to cover T20-only setup, restoration, archive/scoreboard, separate statistics/exports, tours, points, NRR and reversal. Existing format, statistics, weather and tournament regression suites are also run before delivery.
