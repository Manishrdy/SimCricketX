# First-class scoring implementation

Implemented all five workstreams from `FC_SCORING_DEVELOPMENT_PLAN.md`. Changes are local and uncommitted. No database migration, historical-match rewrite, or global scoring-factor adjustment was required.

## What changed

1. **Aggregate match position:** one numeric helper now serves pressure, declaration checks and scoreboards. It handles ordinary third innings, follow-ons, and fourth-innings scores level separately from runs needed to win.
2. **Batting intent:** a deterministic smooth policy blends survival, accumulation and attack using remaining time, wickets, batting strength, pitch/ball conditions and declaration budget. Final-day chases no longer receive automatic survival; stumps caution works each day. Temperament still changes execution, not the tactical objective.
3. **Tail protection:** a specialist can decline early singles and seek late rotation around a weaker partner. Adjustments transfer probability between dots and singles, leaving boundary, wicket and extras mass intact at that stage. Winning singles are exempt, and legal-ball position drives the policy.
4. **Extras and accounting:** resolved FC deliveries carry bat runs, individual extras, bowler concessions, completed running and legality. No-ball run-outs remain valid, penalties do not rotate strike, extra runs are recorded before innings-ending returns, and the winning penalty ends play before another contact roll. Keeper fielding is an explicit keeping proxy; style/fatigue shape the conditional extras mix. Striker form follows bat runs and the actual dismissed player.
5. **Calibration:** the benchmark now records exact delivery metrics, inputs, engine source hash, completion, chase context, intent exposure, collapses and tail contributions. It exports JSON alongside CSV, refuses accidental output overwrites, and raises on non-completion. New whole-match tests verify independent cohorts, distributions and accounting. Cached reports must match the current engine source hash.

## Verification

- Baseline: 23 existing FC calibration checks passed; 200 five-day standard matches recorded before engine changes.
- Final integration suite: **1,051 passed**, one skipped, six expected failures, in 212 seconds.
- Final focused tests: 54 passed, including all 23 existing FC calibration bands, delivery accounting, tail behavior, probability bounds and deterministic resume.
- Full-match cohorts: 800 matches (40 seeds × five pitches × two durations × two squad tiers). All 20 cohort gates passed.
- Independent holdout: 300 matches, seeds 1001–1060 on each pitch. All cohort gates passed.
- Scenario checks: 50 elite-vs-standard pink-ball matches with showers and 50 spin-heavy-attack matches in clear weather. All completed; all score totals reconciled.
- All 1,200 final validation matches satisfy total runs = batter runs + recorded extras. Syntax/static error checks and `git diff --check` pass.

## Measured change

Five-day standard squads, the same 40 seed inputs per pitch. First innings include declarations. Changing outcome logic changes subsequent random draws, so these are cohort comparisons, not delivery-for-delivery matches.

| Pitch | Baseline first innings | Final first innings | Baseline draws / 40 | Final draws / 40 | Final match RPO |
| --- | ---: | ---: | ---: | ---: | ---: |
| Green | 282 | 271 | 7 | 5 | 3.10 |
| Dry | 311 | 305 | 6 | 5 | 3.17 |
| Hard | 319 | 302 | 3 | 7 | 3.38 |
| Flat | 441 | 463 | 18 | 16 | 3.66 |
| Dead | 506 | 515 | 23 | 20 | 4.04 |

The intended pitch ladder remains: Green is the lowest-scoring surface and Flat/Dead support larger totals. No first-innings bands were widened. Draw changes are mixed across surfaces, and 40-match samples are too small to attribute individual differences confidently to one mechanic. Four-day cohorts draw more frequently; weather and duration are kept separate in the data.

## Representative results

### Hard, seed 4: wickets

| Innings | Score | Overs | Ending |
| --- | ---: | ---: | --- |
| 1 | 309/10 | 86 | all_out |
| 2 | 315/10 | 101.5 | all_out |
| 3 | 97/10 | 30.2 | all_out |
| 4 | 92/6 | 27.4 | target |

### Dead, seed 3: drawn

| Innings | Score | Overs | Ending |
| --- | ---: | ---: | --- |
| 1 | 638/9 | 165 | declared |
| 2 | 709/10 | 161.4 | all_out |
| 3 | 270/8 | 64.5 | time |

## Reproduce

```sh
.venv/bin/python scripts/bench_fc.py --per-pitch 40 --days 5 --tier standard --out /tmp/new-fc-report.csv
.venv/bin/python scripts/bench_fc.py --per-pitch 60 --seed-start 1001 --out /tmp/new-fc-holdout.csv
.venv/bin/python -m pytest tests/test_fc_match_calibration.py --no-cov
```

The whole-match test suite generates its 800 matches by default. For reports just produced by this run, set `FC_CALIBRATION_REPORT_DIR=/tmp/fc-release` to validate them without repeating simulations. The expected names are `fc-final-{four,five}-{standard,elite}.csv.json`; the tests check source hash, squads, duration and seeds.

The full suite uses `--no-cov` to avoid replacing existing coverage artifacts. Disabling the verbose engine.match, engine.ball_outcome, engine.game_state_engine, engine.pressure_engine, engine.bowler_manager and engine.fc_bowler_workload loggers avoids excessive per-delivery logging without skipping tests.

## Compatibility and limits

- New tactical state is derived rather than persisted. Existing v1/v2 FC snapshots remain readable; same snapshot plus same RNG state produces the same scoring continuation in regression tests. This does not introduce a match-local RNG or promise deterministic replay across unrelated global RNG use.
- T20/List A/Super Over retain their existing behavior; shared paths are guarded by FC format checks.
- Keeper fielding remains a proxy; there is no new specialist keeping attribute. Rare dismissal types not otherwise simulated remain outside scope.
- Numerical whole-match bands are broad product regression guards. They are not a claim of fitting an independently sourced first-class dataset.
- [Detailed validation data](reports/fc_scoring_validation.json) contains cohort summaries, defaults, input arguments and representative scorecards. Large raw match reports are in `/tmp/fc-release`.
