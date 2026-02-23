# SimCricketX

A full-stack cricket simulation platform built with Flask, featuring a probabilistic ball-outcome engine, real-time WebSocket updates, role-based access control, and a multi-format tournament system. The project spans ~21,700 lines of Python across a modular monolith architecture with 126 tested API routes and production-grade deployment configuration.

**Live match simulation | Tournament management | Admin panel | REST-style API | Full test suite**

---

## Why This Project

SimCricketX was built to demonstrate end-to-end software engineering capability across the full stack — from database schema design and concurrency-safe state management to probabilistic simulation engines and security-hardened admin systems. It is not a toy project: it handles real concurrent users, persists structured match data, enforces authentication and authorization at multiple layers, and ships with CI/CD configuration and a Docker deployment setup.

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | Python 3.9+, Flask 2.0 |
| **ORM / DB** | SQLAlchemy, SQLite |
| **Auth** | Flask-Login, Werkzeug (bcrypt), Flask-WTF (CSRF) |
| **Real-time** | Flask-SocketIO 5.3+, simple-websocket |
| **Rate Limiting** | Flask-Limiter |
| **Data** | Pandas, PyYAML, JSON |
| **Templating** | Jinja2, HTML5/CSS3, JavaScript |
| **Testing** | pytest, pytest-flask, pytest-cov, pytest-mock, Faker, factory-boy |
| **Code Quality** | flake8, black, isort, bandit, safety |
| **Deployment** | Docker (Python 3.9-slim), Gunicorn (1 worker, 4 threads), Waitress |
| **CI/CD** | GitHub Actions (Ubuntu + Windows, Python 3.9–3.11) |

---

## Architecture Overview

```
SimCricketX/
├── app.py                  # App factory, route registration, thread-safe match state
├── database/
│   ├── models.py           # 13 SQLAlchemy models (491 lines)
│   └── __init__.py         # db instance
├── routes/                 # Modular route registration (5,308 lines, 126 endpoints)
│   ├── auth_routes.py
│   ├── team_routes.py
│   ├── match_routes.py
│   ├── tournament_routes.py
│   ├── stats_routes.py
│   └── admin_routes.py     # 64+ admin endpoints
├── engine/                 # Simulation core (11,740 lines)
│   ├── match.py            # Ball-by-ball driver (6,500+ lines)
│   ├── ball_outcome.py     # Probabilistic outcome model
│   ├── game_state_engine.py# Momentum & par-score curves
│   ├── pressure_engine.py  # Psychological pressure factors
│   ├── scenario_engine.py  # Scripted dramatic outcomes
│   ├── tournament_engine.py# Multi-format tournament logic (2,400+ lines)
│   ├── commentary_engine.py# Ball-by-ball narrative generation
│   └── stats_service.py    # Match stat persistence & aggregation
├── auth/
│   └── user_auth.py        # Auth functions, password policy, audit logging
├── utils/                  # Config loading, logging helpers
├── tests/                  # 126 routes tested (8 test files)
├── Dockerfile
├── gunicorn.conf.py
├── pytest.ini
└── migrations/
```

### Key Architectural Decisions

**Single-worker Gunicorn with 4 threads** — Match state lives in a Python in-memory dict (`MATCH_INSTANCES`). Multiple workers would cause state divergence, so the deployment intentionally uses one worker with thread-level concurrency protected by `threading.Lock()` per match instance. This is a deliberate trade-off documented in `gunicorn.conf.py`.

**Modular route registration** — Each route module exports a `register_*_routes(app, db, limiter)` function. This dependency-injection pattern keeps modules decoupled and makes it straightforward to add or remove feature areas without modifying the app factory.

**Thread-safe match file I/O** — Per-match `threading.Lock` objects stored in `_match_file_locks` prevent race conditions when concurrent requests read/write the same match's JSON archive. Global `MATCH_INSTANCES_LOCK` guards the dict itself.

**Graceful Socket.IO degradation** — The app imports `flask-socketio` inside a try/except. If the package is absent, Socket.IO features are silently disabled and all routes serve over standard HTTP.

---

## Simulation Engine

The engine is the core intellectual contribution of this project. It models a cricket match at the individual delivery level using a multi-layer probability pipeline.

### Ball Outcome Pipeline

Each delivery passes through four sequential modifier stages:

```
Base probability matrix
  (batting_rating × bowling_rating × pitch_type)
        ↓
Phase multipliers
  (powerplay / middle overs / death overs)
        ↓
Game state vector
  (momentum, par score delta, RRR pressure)
        ↓
Pressure factors
  (toss advantage, wickets lost, target pressure)
        ↓
Final outcome
  (Dot | 1 | 2 | 3 | 4 | 6 | Wicket | Wide | NoBall | Byes | LegBye)
```

**Momentum model** (`game_state_engine.py`) — Tracks the last 18 deliveries with exponential decay (factor: 0.88). Computes a "game state vector" that encodes whether the batting team is ahead or behind par score. Multipliers are clamped to [0.35, 3.00] to prevent runaway probabilities.

**Par score curves** — Pre-computed expected scores for all 20 overs per pitch type (Green, Dead, Balanced, High-scoring). Used to calculate whether a batting team is under- or over-performing.

**Scenario engine** (`scenario_engine.py`) — Supports scripted dramatic templates: last-ball-six, win-by-1-run thriller, and super-over climax. These are applied as probability overrides when conditions match.

### Tournament Engine

Supports 7+ tournament formats:

- Round-robin (single and double)
- Knockout bracket
- IPL-style (group stage → Eliminator → Qualifier → Final)
- Custom series

Standings are recalculated after each fixture using points (W=2, T=1, L=0) and Net Run Rate. Playoff qualification threshold is configurable per tournament.

---

## Database Design

13 SQLAlchemy models across two categories:

**Business tables** — `users`, `teams`, `players`, `matches`, `match_scorecards`, `match_partnerships`, `tournaments`, `tournament_teams`, `tournament_fixtures`, `tournament_player_stats_cache`

**Security tables** — `admin_audit_log`, `failed_login_attempts`, `blocked_ips`, `active_sessions`, `login_history`, `ip_whitelist`, `site_counters`

Notable design choices:
- `User.email` as primary key (legacy) + `User.stable_id` (UUID) as a forward-compatible identity anchor for future email-change flows
- Transactional SQL for email updates to prevent orphaned records
- Cascade deletion on foreign keys (team delete → player delete, etc.)
- Indexes on `user_id`, `tournament_id`, `ip_address`, `timestamp` for query performance
- `TournamentPlayerStatsCache` avoids expensive per-request aggregation of career stats

---

## Security Implementation

Authentication and authorization are not bolted on — they are integrated at every layer.

**Authentication**
- bcrypt password hashing via Werkzeug
- Password policy: 8+ characters, uppercase, lowercase, digit (enforced at registration and admin reset)
- Flask-Login session management with `ActiveSession` table for server-side session tracking
- `LoginHistory` records every login and logout event

**Authorization**
- `@login_required` and `@admin_required` decorators on all protected routes
- Admin-only routes return 403 (not redirect) to avoid information leakage

**Rate Limiting**
- Flask-Limiter: 30 req/10s for regular users, 90 req/10s for admins
- Adaptive proof-of-work challenges on repeated failed logins
- `FailedLoginAttempt` table tracks attempts per IP with configurable thresholds

**IP-level Controls**
- `BlockedIP` table for admin-managed IP bans
- `IPWhitelistEntry` for maintenance mode access restriction
- Trusted IP prefix bypass for internal tooling

**Audit Trail**
- `AdminAuditLog` persists every admin action with actor email, action type, target, and timestamp
- Indexed for efficient querying across large audit histories

**CSRF**
- Flask-WTF CSRF protection on all state-changing POST endpoints

---

## API Surface (126 Routes)

| Module | Endpoints | Notes |
|---|---|---|
| Core | 6 | Home, ground conditions CRUD |
| Auth | 7 | Register, login, logout, display name |
| Teams | 4 | List, create, edit, delete |
| Matches | 20 | Setup, toss, ball simulation, scorecard, export |
| Tournaments | 5 | Create, view, re-simulate fixture, delete |
| Statistics | 8 | Player comparison, CSV/JSON export, partnership analysis |
| Admin | 64+ | User management, DB backup/restore, IP controls, audit log, system monitoring |

The admin panel alone covers: user CRUD, ban/unban, password reset, email change, database integrity checks, live system metrics (via `psutil`), configuration editing, maintenance mode toggle, session management, and user impersonation for support workflows.

---

## Testing

```
tests/
├── conftest.py               # Fixtures: app, client, authenticated_client, admin_client,
│                             #           regular_user, admin_user, banned_user,
│                             #           test_team, test_team_2, test_tournament
├── test_auth_routes.py       # 7 auth endpoints
├── test_core_routes.py       # 6 core endpoints
├── test_team_routes.py       # 4 team endpoints
├── test_match_routes.py      # 20 match endpoints
├── test_tournament_routes.py # 5 tournament endpoints
├── test_stats_routes.py      # 8 stats endpoints
├── test_admin_routes.py      # 64+ admin endpoints
└── test_admin_security.py    # Authorization boundary tests
```

**Test tooling**: pytest with pytest-flask, pytest-cov (HTML + terminal report), pytest-mock, Faker for synthetic data, factory-boy for ORM fixtures.

**Test database**: In-memory SQLite spun up per test session via app factory. Each test gets a clean state through fixture teardown.

**Coverage targets**: Overall 80%+, critical routes 90%+, core business logic 95%+.

```bash
# Run full suite with coverage
pytest --cov=. --cov-report=html

# Run by marker
pytest -m admin
pytest -m "auth or security"
```

---

## CI/CD

GitHub Actions pipeline runs on every push:

- **Matrix**: Ubuntu + Windows, Python 3.9 / 3.10 / 3.11
- **Steps**: checkout → setup-python → install deps → run tests → flake8 lint → bandit security scan → safety dependency audit
- **Coverage**: `coverage.xml` generated for downstream reporting

---

## Running Locally

**Prerequisites**: Python 3.9+, Git

```bash
git clone https://github.com/ManishYelam/SimCricketX.git
cd SimCricketX

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt

python app.py
# → http://127.0.0.1:5000
```

**With Docker:**

```bash
docker build -t simcricketx .
docker run -p 7860:7860 simcricketx
```

**Run tests:**

```bash
pytest
```

---

## Project Scale

| Metric | Count |
|---|---|
| Total Python lines | ~21,700 |
| Simulation engine lines | 11,740 |
| Route handler lines | 5,308 |
| SQLAlchemy models | 13 |
| API endpoints | 126 |
| Admin-only endpoints | 64+ |
| Test files | 8 |
| Tournament formats supported | 7+ |
| CI matrix configurations | 6 (2 OS × 3 Python versions) |

---

## Engineering Concepts Demonstrated

- **Concurrency**: Thread-safe in-memory state with `threading.Lock`, per-resource locking granularity
- **System Design**: Modular monolith with clean separation of concerns (engine / routes / models / auth)
- **Database**: Relational schema design, ORM usage, indexing strategy, migration scripts
- **Security**: Defense-in-depth (rate limiting + PoW + IP controls + CSRF + audit logging)
- **Testing**: Fixture-driven test isolation, parametrized test clients, mocking, coverage enforcement
- **Deployment**: Dockerized with Gunicorn, environment-aware configuration, graceful feature degradation
- **API Design**: RESTful route structure, consistent error handling, 126-endpoint surface area
- **Data Engineering**: Probabilistic simulation, time-series momentum tracking, NRR computation

---

## License

MIT — see [LICENSE](LICENSE)
