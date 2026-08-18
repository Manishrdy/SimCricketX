# Contributing to SimCricketX

Thanks for your interest in SimCricketX. This guide covers everything you need to get a change from a fresh clone to an open pull request.

New here? Browse the [good first issues](https://github.com/ManishYelam/SimCricketX/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) — they are scoped to be self-contained and need little context about the rest of the codebase.

---

## Quick Setup

**Prerequisites**: Python 3.9+ and Git.

```bash
git clone https://github.com/ManishYelam/SimCricketX.git
cd SimCricketX

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt

python app.py
# → http://127.0.0.1:5000
```

`requirements.txt` contains runtime, test, lint, and security-scanning dependencies — there is no separate dev requirements file.

The app creates and migrates a local `cricket_sim.db` SQLite database on first run, so no database setup is required.

Optional environment configuration lives in `.env`. Copy `.env.example` to `.env` if you need the optional integrations (GitHub exception reporting, inbound webhooks); the app runs fine without it.

---

## Running the Tests

Run the whole suite:

```bash
pytest
```

`pytest.ini` turns on coverage by default (HTML, terminal, and XML reports) and stops after 5 failures.

Before shipping a small Flask change, the focused route suite is a fast sanity check:

```bash
pytest tests/test_core_routes.py tests/test_auth_routes.py tests/test_team_routes.py
```

Other useful invocations:

```bash
# A single file
pytest tests/test_tournament_routes.py

# Tests matching a name pattern
pytest -k "test_login"

# By marker — see the `markers` list in pytest.ini
pytest -m admin
pytest -m "auth or security"

# Full suite with an HTML coverage report in htmlcov/
pytest --cov=. --cov-report=html
```

Tests run against an in-memory SQLite database created per session by the app factory, so they never touch your local `cricket_sim.db`. Shared fixtures (`client`, `authenticated_client`, `admin_client`, `test_team`, `test_tournament`, and others) live in `tests/conftest.py` — reuse them rather than building app state by hand.

Please add or update tests for any behavior change.

---

## Code Style

The CI pipeline runs flake8, black, and isort. Run all three locally before opening a PR:

```bash
black .
isort .
flake8 . --max-line-length=127 --exclude=venv,.venv,env,__pycache__,*.egg-info
```

Notes:

- There is no `pyproject.toml` or `setup.cfg`, so `black` and `isort` run with their default settings. Don't add project-wide formatter config as a drive-by change in an unrelated PR.
- CI invokes flake8 twice: once blocking on real errors (`--select=E9,F63,F7,F82`) and once as an advisory full run with `--max-line-length=127` and `--max-complexity=10`. Matching those flags locally, as shown above, keeps your output consistent with CI.
- Keep formatting churn out of feature PRs. If a file needs a broad reformat, do it in its own commit or its own PR.
- Match the conventions of the file you're editing — comment density, naming, and structure vary by module, and consistency with the surrounding code matters more than any global preference.

---

## Branches and Pull Requests

Branch off `main` using a type prefix:

| Prefix | Use for |
|---|---|
| `fix/` | Bug fixes — `fix/knockout-bracket-seeding` |
| `feature/` | New functionality — `feature/super-over-commentary` |
| `docs/` | Documentation only — `docs/contributing-guide` |
| `test/` | Test-only additions — `test/webhook-routes-coverage` |
| `chore/` | Tooling, config, dependencies — `chore/bump-flask` |

```bash
git checkout -b fix/knockout-bracket-seeding
```

**Every PR should reference an issue.** Open one first if a matching issue doesn't exist — it keeps the discussion about *what* to change separate from the review of *how* you changed it. Link it in the PR description with `Fixes #123` or `Refs #123` so it closes or cross-links automatically.

A good PR:

- Does one thing. Split unrelated changes into separate PRs.
- Explains *why* in the description, not just what — the diff already shows what.
- Includes tests for changed behavior, and passes `pytest` locally.
- Is formatted and linted (`black .`, `isort .`, `flake8`).
- Notes anything you couldn't verify, and how you tested manually if applicable.

---

## Where to Look for a First Task

Three directories are the most approachable, because changes there are self-contained and covered by tests:

**`engine/`** — The simulation core. Individual modules are narrowly scoped and testable in isolation:
`tournament_engine.py` (formats, fixtures, standings, playoff progression), `cricket_math.py` (run rates, NRR, DLS helpers), `dls.py`, `weather.py`, `toss.py`, `format_config.py`. Engine changes are pure logic — no request context, no database session in most paths — which makes them easy to unit test.

**`routes/`** — Each module registers one feature area via a `register_*_routes(app, db, limiter)` function, so you can read a single file and understand the whole surface. The smaller modules (`core_routes.py`, `scenario_routes.py`, `webhook_routes.py`, `support_routes.py`, `admin_issue_routes.py`) are good entry points.

**`tests/`** — Several route modules have no dedicated test file yet. Adding one is a genuinely useful first contribution and teaches you the fixture setup along the way. Model new files on an existing suite such as `tests/test_core_routes.py`.

Also worth a look: `templates/` and `static/` for UI polish, and `config/` plus the docs for configuration and documentation gaps.

If something in this guide is wrong or unclear, that's a valid issue too — please open one.

---

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE) that covers this project.
