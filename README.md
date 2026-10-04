# SimCricketX

**Build a squad. Read the pitch. Play every ball. Run an entire cricket season.**

SimCricketX is a browser-based cricket simulation platform that connects ball-by-ball gameplay with squad management, tournaments, multi-format tours, and persistent statistics. Underneath the scoreboard is a Python simulation engine that models player ability, conditions, match pressure, and tactical decisions.

[Play SimCricketX](https://simcricketx.app/) · [Report an issue](https://github.com/ManishYelam/SimCricketX/issues) · [MIT License](LICENSE)

[![CI](https://github.com/ManishYelam/SimCricketX/actions/workflows/ci.yml/badge.svg)](https://github.com/ManishYelam/SimCricketX/actions/workflows/ci.yml)

For developers, this repository brings together probabilistic modelling, cricket-specific state machines, recoverable delivery requests, relational data modelling, and the practical constraints of serving a stateful game over HTTP and WebSockets.

## The game

### Five formats, different tactical problems

| Format | Match structure | What changes in play |
| --- | --- | --- |
| **T20** | 20 overs per side | Powerplay, consolidation, death-over acceleration, four-over bowling quotas, and Impact Player substitutions. |
| **T10** | 10 overs per side | A dedicated short-format preset, three-over powerplay, two-over bowling quotas, and completion-aware bowling selection. |
| **List A** | 40 or 50 overs per side | Longer innings construction, format-specific ratings, fielding phases, and eight- or ten-over bowling quotas. |
| **The Hundred** | 100 legal balls per side | Five-ball sets, ten-ball end changes, a 25-ball powerplay, 20-ball bowling quotas, and an optional manual fielding timeout. |
| **First-Class** | Four- or five-day matches, up to two innings per team | Sessions, declarations, follow-ons, new balls, bowler workload, pitch deterioration, weather, and draws. |

Limited-overs formats support automatic and manual selection modes. First-Class uses automatic captaincy. The Hundred draws its squad and ratings from T20; T10 has a separate squad profile that can be initialized from an existing T20 squad.

### Build teams with an identity

Create teams, manage format-specific squads, arrange batting orders, and select a playing XI and bowling options. Players carry batting, bowling, and fielding ratings alongside their cricket roles and bowling styles.

The player pool separates shared master players from personal custom players and rating overrides. Search and filter the pool, import players from JSON or CSV, and reuse player identities across squads without treating a shared identity as shared ownership. Team profiles keep format-specific selections and career records separate.

### Shape the match, then watch it unfold

Choose from **Green, Dry, Hard, Flat, and Dead** pitches, configure ground conditions, and select day or day/night play. Conditions feed into scoring and wicket probabilities; pitch wear and dew can change the contest as the match progresses.

The live dashboard combines delivery commentary with batting and bowling figures, partnerships, a wagon wheel, Manhattan charts, and innings comparison curves. Manual selection lets you make supported batting and bowling choices, while automatic selection handles the decisions for you. Tied limited-overs games use format- and competition-specific outcomes, including Super Overs and Hundred Super Fives where applicable.

Rain can reduce allocations, revise targets, or produce a no result. First-Class weather consumes playing time and affects the available path to a result.

For a different kind of match, enable dramatic scenarios or select a historical story pack. The included India–Pakistan 2022 T20 World Cup pack recreates a pressure arc with your chosen teams. Story packs guide score and wicket corridors rather than replaying an exact historical scorecard.

### Turn individual matches into competitions

The tournament engine supports seven structures:

- Single and double round-robin leagues.
- Knockout brackets, including byes.
- Single and double round-robin leagues followed by semifinals and a final.
- IPL-style playoffs with Qualifier 1, Eliminator, Qualifier 2, and Final.
- Custom two-team series.

Fixtures, standings, qualification, and player statistics connect to the match engine. Re-simulating a fixture reverses its recorded contributions and handles dependent playoff fixtures, so a changed result can propagate through the competition.

**Tours** bring multiple series between a host and visitor into one ordered schedule. Combine formats, validate squad readiness, reorder series before play, and track results and leaders across the tour.

### Keep the story after the final ball

Match history and detailed scorecards sit alongside player profiles, team statistics, head-to-head records, partnership analysis, and cross-format player comparisons. Statistics distinguish the format actually played from the squad used to play it; Hundred matches do not become T20 career appearances.

List A statistics can be filtered by the original scheduled length—40 or 50 overs—even after a rain reduction. Match archives, statistical exports, and scorecard image exports make results available outside the live dashboard.

The surrounding platform includes account and session management, guided walkthroughs, and a community board with posts, comments, votes, mentions, notifications, image uploads, search, and moderation.

### Simulation boundaries

The rules and probability models are simulator implementations. Rain targets use an embedded **D/L Standard Edition approximation**, not professional DLS. Hundred competitions use the simulator's custom tournament structures; the official three-team Hundred playoff preset is not implemented. Fielding restrictions influence probabilities rather than simulating physical player positions.

See the [T10 implementation notes](docs/t10.md) and [Hundred rules and limitations](docs/HUNDRED.md) for detailed coverage. The 40-over List A option shares the existing List A rain and tie policies, including the 20-over minimum for a rain-affected result; it is not a complete recreation of an ECB recreational ruleset.

## Engineering the simulation

### A delivery is a state transition

The core loop lives in [engine/match.py](engine/match.py). It coordinates batting order, bowling selection, innings transitions, weather, scorecards, commentary, and format-specific decisions. Delivery probabilities live in [engine/ball_outcome.py](engine/ball_outcome.py), with separate modules for pressure, game state, conditions, and captaincy.

Conceptually, each delivery connects these concerns:

```text
Format rules + player ratings + pitch and conditions
                         |
              Delivery outcome weights
                         |
       Phase, pressure, momentum and tactical modifiers
                         |
                  Sample an outcome
                         |
        Apply runs, extras, wickets and legal-ball rules
                         |
      Update match state, commentary and live presentation
                         |
        Persist checkpoints and completed match records
```

Ratings change the shape of the scoring distribution: dots, singles, and boundaries respond differently to the batter–bowler contest. Match-state modifiers account for factors such as wickets in hand, recent scoring, partnerships, and required rate. Format-specific bounds and calibration tests help catch feedback loops in which pressure produces wickets that produce still more pressure.

[engine/format_catalog.py](engine/format_catalog.py) defines public format identities, squad sources, and available controls. [engine/format_config.py](engine/format_config.py) defines playing parameters and creates per-match configurations. Original scheduled length remains separate from rain-revised allocation.

Bowling legality is also a planning problem. The short-format managers check whether a selection leaves a legal way to finish the innings, rather than checking only the selected bowler's remaining quota. Cricket arithmetic uses legal deliveries to avoid treating notation such as `19.5` as a decimal number of overs.

### First-Class captaincy looks ahead

The First-Class engine models a different objective: balancing runs, wickets, and remaining time across several innings.

[engine/fc_forecast.py](engine/fc_forecast.py) uses a fitted scoring/risk model and dynamic programming to estimate innings outcomes. [engine/fc_captain.py](engine/fc_captain.py) weighs win and draw probabilities when evaluating declarations, follow-ons, and batting tempo. These forecasts are deterministic and do not consume the delivery random stream.

The forecast coefficients are fitted from the ball engine through the calibration tooling. Decision budgets and cached forecasts constrain the cost of live decisions. Under gevent, expensive pure computation is offloaded to its thread pool so the server can continue serving requests and WebSocket heartbeats.

### One match, two delivery transports

HTTP and Socket.IO share the same advancement path. [services/match_delivery.py](services/match_delivery.py) serializes delivery requests with a per-match lock and rotating tokens. Recent tokenized responses are cached: retrying a lost response can return the original delivery without bowling another ball. Stale tokens produce a conflict that directs the client to recover its state.

Live match objects remain in an in-process registry. Persistence combines SQL records with match JSON and format-specific snapshots. First-Class, Hundred, and Super Over recovery have dedicated paths; Hundred checkpoints also preserve the random stream and pending decisions. Ordinary T20, T10, and List A innings do **not** provide equivalent durable recovery after process loss.

### Statistics preserve cricket semantics

The database separates team and pool identity, match records, scorecard rows, partnerships, competition fixtures, and cached tournament aggregates. That separation supports several subtle requirements:

- Player identity can span formats while statistics remain scoped to the owning user's matches.
- Super Over and Super Five contributions remain distinguishable from regular innings.
- Hundred legal balls, sets, and bowling rates retain explicit units.
- Tournament result reversal can undo recorded points and net-run-rate contributions.
- Historical List A filters use scheduled length rather than the final rain-revised length.

See [database/models.py](database/models.py), [engine/stats_service.py](engine/stats_service.py), and [engine/player_comparison.py](engine/player_comparison.py).

## Application architecture

SimCricketX is a **Flask modular monolith**. The app factory wires route modules and shared dependencies; the browser uses Jinja templates, CSS, and JavaScript without a separate frontend build step.

| Layer | Implementation |
| --- | --- |
| Web application | Flask, Jinja2, browser JavaScript, Chart.js |
| Simulation and analysis | Python, NumPy, Pandas, YAML/JSON configuration |
| Persistence | Flask-SQLAlchemy, SQLAlchemy, SQLite, match files |
| Live updates | Flask-SocketIO, simple-websocket, gevent WebSocket worker |
| Authentication and request protection | Flask-Login, Werkzeug password hashing, Flask-WTF, Flask-Limiter |
| Testing | pytest and coverage tooling; Node test runner for JavaScript regressions |
| Runtime and delivery | Gunicorn, Docker, GitHub Actions, systemd deployment script |

```text
app.py                 Application factory, shared runtime and transport wiring
engine/                Delivery model, match lifecycle, formats, competitions, stats
routes/                Gameplay, teams, tours, accounts, community and admin endpoints
services/              Delivery recovery, community workflows and issue integration
database/              SQLAlchemy models and database initialization
auth/                  Authentication and authorization helpers
middleware/            Request timing and session log capture
utils/                 Configuration, email, diagnostics and CPU offloading
templates/             Jinja pages and reusable partials
static/                Browser interactions, charts and styles
config/                Application settings and ground-condition defaults
migrations/            Startup migrations, historical repairs and verification
scripts/               Calibration, data maintenance, local QA and deployment tools
tests/                 Engine, route, persistence, security and browser regressions
docs/                  Format rules and operational notes
match_archiver.py      Match archive generation
```

**The deployment is intentionally single-worker.** [gunicorn.conf.py](gunicorn.conf.py) configures one gevent WebSocket worker because live matches are held in memory. Per-match locks protect mutations, but they do not share objects across processes. Horizontal scaling would require a shared match-state design and coordinated delivery ownership.

Administration covers users, sessions, player pools, configuration, backups, maintenance access, community moderation, and diagnostic workspaces. Security mechanisms include ownership checks, CSRF protection, rate limits, failed-login controls, optional Turnstile, and authentication event records.

Exceptions can be grouped by fingerprint and optionally linked to GitHub issues through a background queue. Request timing records request IDs, response duration, database time, and query counts without logging request payloads. The [request timing guide](docs/operations/request-timing.md) explains proxy correlation and diagnosis.

## Run locally

Use Python 3.11 for the local workflow below. Dependencies are listed in [requirements.txt](requirements.txt); Node.js is needed only for JavaScript tests and related tooling.

```bash
git clone https://github.com/ManishYelam/SimCricketX.git
cd SimCricketX
python3.11 -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Open [localhost:7860](http://127.0.0.1:7860). Local startup also attempts to open a browser. The application uses `cricket_sim.db` in the repository root and runs startup migration checks.

Configuration comes from [config/config.yaml](config/config.yaml), environment variables, and optional `.env` values. [.env.example](.env.example) documents GitHub issue and webhook integration. Email verification and password-reset delivery use `RESEND_API_KEY` with the email settings in YAML.

For your own deployment, configure a private application secret. The current loader gives YAML `app.secret_key` precedence over `FLASK_SECRET_KEY`; set the YAML value to `change_me` to use the environment variable or generated persistent local secret. Keep real credentials out of commits.

For local testing that requires authenticated accounts, follow [AGENTS.md](AGENTS.md) and the local `.claude/TEST_ACCOUNTS.md` guide. Use the five shared QA identities through `scripts/dev_test_accounts.py`; if their local credential file is missing, initialize them against the local/dev database:

```bash
.venv/bin/python scripts/dev_test_accounts.py seed
```

The generated credentials are local and gitignored.

## Verification and calibration

The test suite covers more than route responses: legal bowling allocations, free hits, rain interruptions, innings transitions, snapshot restoration, cross-format statistics, fixture reversal, delivery transport parity, and authentication boundaries all have regression coverage.

```bash
# Full Python suite; pytest.ini enables coverage reports by default.
python -m pytest

# Focused rules and delivery-recovery checks.
python -m pytest -o addopts='' \
  tests/test_cricket_math.py tests/test_match_delivery.py \
  tests/test_t10.py tests/test_hundred.py

# JavaScript regression tests.
node --test tests/*.test.cjs tests/js/*.test.cjs
```

Seeded benchmarks complement example-based tests by measuring distributions across pitches and lighting conditions. They assess scoring bands, wicket rates, phase behaviour, and chase balance rather than assuming that one plausible scorecard proves the model is calibrated.

```bash
python scripts/bench_t10.py --seeds 120
python scripts/bench_lista_lengths.py --out reports/lista_lengths.json
```

Additional tools cover List A, dew, and First-Class calibration, including fitting the First-Class forecast model. Benchmark bands describe engineering acceptance criteria for this simulator, not independently validated predictions of real matches.

The current [CI workflow](.github/workflows/ci.yml) runs lint, formatting, import-order, and security checks on Python 3.11. These checks are configured as advisory. It does **not** currently run the Python or JavaScript test suites; run the relevant suites locally before submitting changes.

## Deployment and database evolution

Run the configured production server with:

```bash
gunicorn -c gunicorn.conf.py app:app
```

It binds to `127.0.0.1:5000` for a reverse proxy. The [Dockerfile](Dockerfile) exposes port 7860 and starts Gunicorn with a bind override:

```bash
docker build -t simcricketx .
docker run --rm -p 7860:7860 simcricketx
```

That command is an ephemeral container run. Configure persistent storage for the SQLite database and generated data when retaining user state. The Dockerfile currently uses Python 3.9, while the local workflow above and CI use 3.11.

The [production deployment workflow](.github/workflows/deploy.yml) is manually triggered. It invokes [scripts/deploy.sh](scripts/deploy.sh) on the configured OCI host to update code, install changed dependencies, snapshot the database, apply migrations, restart the service, and check health, with rollback handling for migration and health-check failures.

Startup migrations are registered centrally and designed to be idempotent. For a read-only check of an existing database, use:

```bash
python -m migrations.precheck --check --db ./cricket_sim.db
```

Keep `--check`: omitting it applies migrations. The verifier rehearses changes on a disposable SQLite snapshot and reports schema drift, pending changes, and repairs requiring manual review. Destructive migrations also have runtime revision guards to detect older processes still holding the database. See the [migration verification guide](migrations/README.md) before database maintenance.

## Contributing

Choose the layer closest to the behaviour you want to change: `engine/` for simulation rules, `routes/` and `services/` for application workflows, or `templates/` and `static/` for the interface.

For engine changes, add regressions for the affected rule and use seeded calibration when changing scoring probabilities. For persistence changes, include migration verification. For live interactions, consider both HTTP and WebSocket delivery, retries, and resume behaviour. Keep generated match data, credentials, database files, and reports out of unrelated commits.

Open an [issue](https://github.com/ManishYelam/SimCricketX/issues) or a pull request with the behaviour being changed and the validation performed.

## License

SimCricketX is available under the [MIT License](LICENSE).
