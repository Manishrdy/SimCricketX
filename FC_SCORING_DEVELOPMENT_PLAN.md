# First-class scoring engine: development plan

Status: implemented locally. See [implementation and validation report](FC_SCORING_IMPLEMENTATION.md). The sections below retain the approved development scope.

## Objective and scope

Address all five reviewed areas: aggregate match awareness, gradual batting intent, tail protection, extras realism and accounting, and whole-match calibration. Preserve the intended pitch ladder and existing FC features: wear, ball age, declarations, weather, workload, technique, temperament, and nightwatchmen.

Implement as separate, testable changes. Keep T20, List A, and Super Over behaviour stable unless a shared accounting correction is explicitly included and covered by regression tests. Do not use a global run-rate adjustment to hide tactical or accounting problems.

## Findings confirmed during planning

| Area | Current implementation | Implication |
| --- | --- | --- |
| Match position | `Match._fc_build_match_state()` uses current score minus innings-one score for both innings two and three. | Third-innings tactical decisions can see a deficit when the side actually leads; follow-on aggregates are also incomplete. |
| Intent | Match state has hard survival thresholds; `FCPressureEngine` independently forces survival in innings two/four on the final day and also applies chase acceleration at high required rates. | Achievable chases can receive survival modifiers; incompatible tactical effects can stack. |
| Tail protection | FC pressure state includes striker traits but not non-striker strength or ball position within the over. | No explicit ability to manage strike around a weak partner. |
| Extras | `calculate_outcome()` uses fixed FC extra-type proportions; `Match.next_ball()` subsequently adds bat runs to no-balls. | My earlier claim that no-balls cannot include bat runs was incorrect. Extend and verify the existing mechanism rather than recreate it. The second roll currently suppresses every dismissal, including run-outs. |
| Calibration | `tests/test_fc_calibration.py` bands first innings; `scripts/bench_fc.py` already simulates full matches and reports draws, individual innings, extras, and partnerships. | Extend the existing benchmark into repeatable automated checks rather than build a duplicate simulator. |

These are code-inspection findings. No new simulations were run to establish numerical baselines for this plan.

## Delivery sequence

| Stage | Deliverable | Depends on | Relative effort |
| --- | --- | --- | --- |
| 0 | Reproducible baseline and benchmark instrumentation | None | Medium |
| 1 | Correct aggregate match position | Stage 0 baseline capture | Small |
| 2 | Unified, continuous FC batting intent | Stage 1 | Medium–large |
| 3 | Context-aware tail protection | Stage 2 | Medium |
| 4 | Extras resolution and accounting improvements | Stage 0; integrate with stages 2–3 | Large |
| 5 | Whole-match calibration gates and final tuning | All preceding stages | Medium–large |

Keep each stage separately reviewable. Capture distributions after each behavioural stage so the cause of changes remains visible.

## Stage 0 — Establish evidence before tuning

Primary files: `scripts/bench_fc.py`, `tests/test_fc_calibration.py`, and new focused FC test modules.

1. Record the current revision, ground configuration, format duration, squad definitions, forecast, seed, and completion status in benchmark outputs.
2. Capture both existing first-innings bands and full-match results. Preserve standard-versus-standard and elite-versus-elite fixtures; add asymmetric teams and contrasting attack compositions.
3. Extend benchmark output with structured innings endings, actual legal-ball counts, target at chase start, wickets/overs available, and tactical-state exposure. Preserve its current command-line interface and CSV output where practical.
4. Fail explicitly when a simulation reaches its delivery safeguard without finishing; never silently exclude non-completions from averages.
5. Use fixed seed sets and fixed weather inputs. Paired seeds help comparison but do not imply matching delivery sequences after a change alters random-number consumption.
6. Keep a separate holdout seed set for final validation. Avoid new random calls for diagnostic logging.

Acceptance: baseline artifacts can be reproduced with the recorded inputs, and every requested match is either counted or reported as a failure.

## Stage 1 — Correct aggregate match awareness

Primary file: `engine/match.py`. Tests: new `tests/test_fc_match_position.py`, plus existing FC format/resume tests.

### Implementation

- Add one pure numeric helper for batting-side aggregate lead. Reuse it in pressure state, interval scoreboard text, and lead-before-delivery reporting where their semantics agree.
- Keep target runs needed separate from lead: in innings four, a level aggregate score still leaves one run required to win.
- Audit declaration and follow-on callers for duplicated arithmetic; replace equivalent calculations without changing declaration policy in this stage.
- Represent innings-one lead as unavailable; adapt legacy consumers explicitly instead of treating an unavailable comparison as a deficit.

### Required examples

Let A1 and B1 mean the teams' first innings, and S the current score.

| Situation | Batting-side lead | Example |
| --- | --- | --- |
| Innings two | S − A1 | A1=400, S=250 → −150 |
| Innings three, ordinary order | A1 + S − B1 | 400, 250, S=50 → +200 |
| Innings three, follow-on | B1 + S − A1 | 400, 250, S=50 → −100 |
| Innings four | S − (target − 1) | Target 201, S=200 → 0; one run needed |

Acceptance: pressure and scoreboard agree on every example, including tied aggregates, declarations, innings defeats, and resumed matches. A side leading by 200 must not enter survival because of a fictitious deficit.

## Stage 2 — Unify and smooth batting intent

Primary files: `engine/match.py`, `engine/pressure_engine.py`; proposed pure policy module `engine/fc_batting_intent.py`. Tests: new `tests/test_fc_batting_intent.py`.

### Inputs and policy

- Build one FC tactical context: aggregate position, runs needed, effective overs remaining, wickets in hand, current pair and remaining batting strength, pitch wear, ball condition, and declaration time budget.
- Return bounded survival and attacking intensities plus a readable reason. Blend between survival, neutral accumulation, and attack with weights summing to one.
- Use smooth ramps around chase feasibility and declaration urgency. Estimate achievable scoring pace with a small, explainable helper; do not call the stochastic delivery resolver recursively or pretend this estimate is a calibrated win probability.
- Distinguish an achievable chase from batting out an unreachable target. Increasing required rate can increase attack within the feasible range, then shift toward survival as the chase becomes unrealistic.
- Remove the pressure engine's independent final-day survival fallback when the unified context is supplied. Specify a neutral, conservative compatibility path for older/minimal callers.
- Make the existing high-required-rate risk term part of the same policy so it cannot add aggressive chasing to a full rearguard.
- Treat declaration urgency, approaching stumps, settling in, and partnership effects explicitly. Prevent survival and declaration acceleration from independently applying at full strength.
- Correct the mismatch between the “closing overs of a day” intent and the current final-match-day-only `last_hour` flag. Separate protecting wickets before ordinary stumps from saving the match; retain the live-chase exemption.
- Start with a deterministic policy derived from current state, avoiding persistent smoothing state. If history-based smoothing proves necessary, add it only with snapshot serialization and resume tests.
- Keep coefficients in one named, documented configuration location; validate bounds and neutral defaults. No new user-facing tuning screen is required for this scope.

### Acceptance and tests

- A feasible final-day chase is not forced into survival solely by the day number.
- A large deficit and little time produces stronger survival; a feasible declaration push increases scoring intent with a bounded risk cost.
- Sweep required rate through the old 2.8/4.2/4.5 thresholds and demonstrate continuous modifier changes. Bound one-step changes with explicit tolerances after choosing the curve.
- Zero remaining time or a completed target produces no division by zero or extra delivery.
- Probability weights remain finite and non-negative; normalized probabilities sum to one.
- High temperament improves execution without forcing a tactical choice by itself.
- Same saved state and RNG state yield the same continuation as an uninterrupted match.

## Stage 3 — Add tail protection without forced outcomes

Primary files: `engine/fc_batting_intent.py`, `engine/ball_outcome.py`, `engine/match.py`. Tests: new `tests/test_fc_tail_protection.py`.

### Implementation

- Add striker/non-striker batting and technique ratings, balls faced, wickets remaining, and legal balls left in the over to tactical context.
- Derive protection strength from the ability gap and remaining batting resources. Do not classify every No. 8 as weak or every nightwatchman as an ordinary specialist.
- Model declining a potential early-over single as a transfer of single probability to dot probability. This preserves total probability and leaves wickets/extras unchanged by the refusal itself.
- Allow a stronger specialist to seek a late-over single through a separate bounded scoring-intent adjustment; do not guarantee that a single is available.
- Model a weaker striker's willingness to return strike to the specialist, without automatically making weak batters better at scoring.
- Ease protection when the single wins the match, during urgent feasible chases, or when both batters have similar ability. Leave boundaries available.
- Apply the adjustment once before sampling/finalizing the delivery. Never remove runs after they have reached scorecards, partnerships, or commentary.
- Base over position on legal balls. Extra deliveries do not move the tactical window; running on extras must use completed runs rather than penalty-run parity.

### Acceptance and tests

- At eight/nine wickets down, a settled specialist with a much weaker partner accepts fewer early-over singles than an evenly matched pair.
- Late-over behaviour increases expected specialist strike retention across many overs, without guaranteeing retention.
- Winning singles are never declined for protection.
- Boundaries, wickets, run-outs, wides, no-balls, and the sixth legal ball preserve correct strike and accounting.
- Neutral or missing traits fall back safely; T20/List A distribution is unchanged.
- Benchmark tail partnership lengths, specialist not-outs, tail exposure, and last-two-wicket contributions. Reject tuning that creates implausibly durable final wickets.

## Stage 4 — Improve extras and delivery accounting

Primary files: `engine/ball_outcome.py`, `engine/match.py`, and affected commentary/archive consumers. Tests: new `tests/test_fc_extras.py`, existing FC archive/statistics/resume and free-hit tests.

### A. Define the delivery contract

- Inventory all consumers of `runs`, `bat_runs`, `is_extra`, `extra_type`, and wicket fields before changing them.
- Introduce an internal resolved-delivery breakdown: total runs, bat runs, extra components, bowler-conceded runs, completed running, legal-ball status, and dismissal/end information. Preserve existing public fields through an adapter where required.
- Finalize this breakdown before commentary, pressure history, form updates, partnerships, scorecards, and target checks consume the delivery.
- Resolve a no-ball as one delivery with a penalty and compatible secondary outcome. Retain the existing bat-run capability; avoid an independent second extras event and avoid double-applying tactical effects.

### B. Correct dismissal and running handling

- The current no-ball path erases all wickets. Preserve supported lawful run-outs with the correct dismissed batter and completed runs; continue rejecting incompatible dismissals.
- Support no-ball plus bat runs and no-ball plus non-bat running with explicit component accounting. Add multi-run wides and boundary leg-byes where the resolver can represent them correctly.
- Keep FC's configured free-hit behaviour intact; add cross-format regressions around the shared code.
- Cover the winning penalty run: the resolver must respect when the match ends rather than blindly adding a subsequently sampled scoring outcome.
- Verify boundaries, completed runs, and end changes against the ruleset used by the simulator. Do not add rare dismissal types that the engine does not otherwise model.

Rules reference: [MCC No ball](https://www.lords.org/mcc/the-laws/no-ball) permits run-outs on no-balls and specifies allocation of penalty and additional runs. [MCC The result](https://www.lords.org/mcc/the-laws/the-result) governs match completion. Pin the applicable rules edition in tests rather than relying on section numbers across editions.

### C. Make extras depend on players and conditions

- Replace fixed FC type proportions with bounded modifiers around the existing baseline, using bowler style/fatigue, ball condition, and keeper quality where causally relevant.
- Use the designated keeper's existing fielding rating as an explicit proxy if no keeping attribute exists. Do not silently use the team average; retain a neutral fallback for incomplete squads.
- Better keeper quality should reduce byes and/or their severity, not leg-byes or no-ball penalties. Transfer saved bye outcomes to dots so bat-run scoring is not inflated.
- Avoid applying new-ball extras effects twice: retain a documented distinction between total extras incidence and the conditional type mix.
- Calibrate both event frequency and runs conceded by type. A four-bye event is one event but four extra runs.

### Acceptance and tests

- Every delivery satisfies total runs = bat runs + all extra components.
- Team totals, batter totals, bowler concessions, extras, and partnerships reconcile after each delivery and after archive/resume.
- Byes/leg-byes are not charged to the bowler; illegal deliveries do not advance the legal-ball counter; batter-ball and maiden conventions are explicitly tested.
- Deterministic cases include no-ball + four, no-ball + odd completed running, lawful no-ball run-out, multi-run wide, four leg-byes, and a target reached on an extra.
- A keeper-rating sweep reduces byes monotonically in expectation while leaving unrelated extra penalties stable.
- Updated live commentary and archived scorecards describe the same resolved event.

## Stage 5 — Whole-match calibration and release gates

Extend `scripts/bench_fc.py` and add `tests/test_fc_match_calibration.py`. Reuse the existing squads and collectors, extracting small shared helpers only where duplication would otherwise grow.

### Metrics

| Metric | Required breakdown |
| --- | --- |
| Scores and pace | Median, lower/upper quantiles, mean RPO, runs per wicket; by pitch, innings and duration |
| Results | Draws, wins, innings victories, ties; weather-separated and balanced/asymmetric teams |
| Chases | Success and survival by target, available overs and wickets; report denominators |
| Individual innings | Ducks, 50s, 100s, 200s, dismissal/not-out status and batting position |
| Century conversion | Hundreds divided by innings reaching 50, with completed/dismissed innings and censored not-outs separately identified |
| Collapses | Explicit windows such as at least three wickets in 30 legal balls; distinguish overlapping windows from separate episodes |
| Partnerships | Runs/balls quantiles by wicket number, with unfinished stands identified |
| Tail protection | Specialist share of strike, final-two-wicket runs and partnership lengths |
| Extras | Events and runs per 100 deliveries, component mix, keeper/bowler effects |
| Integrity | Non-completions, accounting failures and invalid probability/state values |

### Sampling and thresholds

- Retain current first-innings guards for all five pitches and both squad tiers.
- Start routine full-match regression batches at 40 seeds per pitch for each four-/five-day balanced fixture tier, then measure runtime and sampling error before assigning CI cadence.
- Use smaller focused scenario fixtures for red-ball/pink-ball, clear/rain-affected games, mismatches, follow-ons, reachable chases and rear guards. Run a larger offline holdout batch before accepting the combined change.
- Do not impose one draw-rate target across every surface and duration. Preserve the previously intended higher first-innings scores on Flat and Dead.
- Establish numerical bands from baseline variation and documented product expectations. Where a band claims real-world realism, attach a suitable competition/date-specific source; existing benchmark comments alone are not evidence.
- Use uncertainty estimates with whole matches as the sampling unit. Do not treat every ball or batter from one match as an independent sample.
- Compare distribution shape as well as means. Explain intentional shifts caused by the lead and final-day intent fixes rather than widening every failing band.
- Keep probability-level and accounting tests fast and deterministic. Mark large distribution suites `slow` and use a dedicated scheduled/manual regression run if runtime exceeds ordinary CI capacity.

Acceptance: all correctness tests pass; existing pitch/squad relationships remain defensible; whole-match shifts are measured, explained, and within reviewed bands on holdout seeds. Baseline preservation alone is insufficient for known-bug scenarios.

## Compatibility, integration, and review deliverables

- Review legacy FC snapshots with missing new fields. Prefer derived tactical state and optional delivery fields; version persistent payloads only if their interpretation changes.
- Archive integration must preserve per-innings extras and player figures without rewriting historical matches. A database migration is needed only if the consumer audit proves the existing schema cannot represent the required aggregates.
- Run targeted tests after each stage, then the complete existing suite after integration. Include FC end-to-end, resume, archive/statistics, and shared limited-overs/free-hit regressions.
- No existing generated reports, database files, or user configuration should be overwritten by benchmark output.
- Deliver separate change summaries, test results, baseline-versus-final tables, representative match scorecards, and any deliberate calibration changes with rationale.
- Proposed review order: approve the overall scope; implement baseline and correctness; review tactical behaviour and extras contracts; review final calibration evidence. This document itself does not authorize deployment or publish any change.

## Decisions proposed for this review

1. Adopt all five workstreams in the sequence above, with no initial global scoring-factor change.
2. Use probabilistic tail protection driven by player ability, not forced strike retention.
3. Use the existing keeper fielding rating as a documented proxy rather than introduce a new player attribute and migration now.
4. Preserve current pitch targets as the starting product intent, while reviewing genuine distribution changes from correctness fixes.
5. Keep new tactical tuning internal initially; extend the Ground Conditions UI only if a later product requirement calls for it.
