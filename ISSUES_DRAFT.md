# Good First Issue — Drafts

Eight scoped, low-context tasks found by scanning the codebase on 2026-08-18. Each is self-contained: a contributor can complete it without understanding the rest of the system.

Suggested labels are noted per issue. All are intended to carry `good first issue`.

**Not verified by running anything** — these come from reading the code, CI config, and docs. Worth a quick confirmation before posting.

---

## 1. CI pipeline never runs the test suite

**Labels:** `good first issue`, `ci`, `bug`

### Description

`.github/workflows/ci.yml` defines exactly two jobs — `lint` (flake8 / black / isort) and `security` (safety / bandit). There is no job that runs `pytest`, so the full test suite never executes on a push or pull request.

The README's CI/CD section describes something quite different:

> **Matrix**: Ubuntu + Windows, Python 3.9 / 3.10 / 3.11
> **Steps**: checkout → setup-python → install deps → run tests → flake8 lint → bandit security scan → safety dependency audit

Neither the matrix nor the test step exists. Both existing jobs are also pinned to `python-version: '3.8'`, below the Python 3.9+ the README and `requirements.txt` target.

Add a `test` job that actually runs the suite, and bring the workflow in line with the documented matrix. `pytest.ini` already emits `coverage.xml`, so uploading it as an artifact is a natural follow-on.

### Acceptance criteria

- [ ] `ci.yml` has a `test` job running `pytest` on every push and pull request
- [ ] The job uses a matrix of Ubuntu + Windows across Python 3.9, 3.10, and 3.11
- [ ] Existing `lint` and `security` jobs no longer pin Python 3.8
- [ ] The README's CI/CD section matches what the workflow actually does

### Files involved

- `.github/workflows/ci.yml`
- `README.md` (CI/CD section)

---

## 2. `tests/README.md` tells contributors to install a file that doesn't exist

**Labels:** `good first issue`, `documentation`

### Description

The Quick Start in `tests/README.md` opens with:

```bash
pip install -r requirements-dev.txt
```

There is no `requirements-dev.txt` in the repo. All test and lint dependencies (pytest, pytest-cov, pytest-flask, faker, factory-boy, flake8, black, isort, bandit, safety) live in the main `requirements.txt` under `── Testing ──` and `── Code quality ──` headers. A new contributor following the test docs hits an error on their first command.

The same file's coverage table is also stale: it lists 8 test files, but `tests/` now holds over 40.

### Acceptance criteria

- [ ] `tests/README.md` instructs `pip install -r requirements.txt`
- [ ] The test-file inventory reflects what's actually in `tests/`, or is replaced with something that won't drift (e.g. "run `pytest --collect-only`")
- [ ] No other references to `requirements-dev.txt` remain (`grep -r "requirements-dev"`)

### Files involved

- `tests/README.md`

---

## 3. `.env.example` ends with an empty section for a removed feature

**Labels:** `good first issue`, `documentation`, `cleanup`

### Description

`.env.example` ends with a header and nothing under it:

```
# -----------------------------------------------------------------------------
# Auction UX rollout (Module A)
# -----------------------------------------------------------------------------
```

The auction feature was removed from the project — no routes, templates, or tests reference it. The trailing header suggests there are auction variables a contributor forgot to fill in, when in fact there are none.

While in the file, check the remaining entries against what the code actually reads. `GITHUB_WEBHOOK_SECRET` is documented, but it's worth confirming nothing else read via `os.environ` / `os.getenv` is missing from the example.

### Acceptance criteria

- [ ] The empty "Auction UX rollout" section is removed
- [ ] Every environment variable the app reads at startup appears in `.env.example` with a comment explaining it
- [ ] Variables the app can run without are clearly marked optional

### Files involved

- `.env.example`

---

## 4. `templates/400.html` is an unstyled stub next to designed 404 and 500 pages

**Labels:** `good first issue`, `ui`, `frontend`

### Description

`templates/404.html` (178 lines) and `templates/500.html` (195 lines) are fully designed pages — large gradient error codes, themed layout, helpful links. `templates/400.html` is 13 lines of inline `style="..."` attributes with no styling of its own:

```html
<div style="max-width:760px;margin:3rem auto;padding:1rem 1.25rem;">
    <h1 style="margin:0 0 .5rem;">400 - Bad Request</h1>
```

This page isn't hypothetical — it renders for real users whenever a CSRF token is missing or stale (see the `handle_csrf_error` handler in `app.py`), which typically happens after leaving a form open too long. Right now the most common way to hit it produces the least helpful screen.

Restyle it to match 404/500, and give the copy a nudge toward the usual fix (reload the page and resubmit).

### Acceptance criteria

- [ ] `400.html` uses the same visual language as `404.html` / `500.html` (no inline `style` attributes)
- [ ] The optional `reason` variable still renders when the handler passes one
- [ ] Copy suggests reloading and resubmitting, since expired CSRF tokens are the common cause
- [ ] Page is responsive and legible in both light and dark themes

### Files involved

- `templates/400.html`
- `templates/404.html`, `templates/500.html` (reference only)
- `app.py` — `handle_csrf_error`, around line 1708

---

## 5. Browser users hitting an admin route get raw JSON instead of a page

**Labels:** `good first issue`, `ui`, `frontend`

### Description

`auth/decorators.py` returns a JSON body for authenticated non-admins:

```python
return jsonify({"error": "Forbidden: Admin access required"}), 403
```

That's the right response for an API client, but a logged-in non-admin who follows a link to `/admin/...` in a browser sees bare JSON on a white page — no navigation, no way back. There is no `templates/403.html` and no `@app.errorhandler(403)` registered in `app.py`.

The 404 handler already demonstrates the pattern to follow: JSON for `/api/` paths and JSON-preferring `Accept` headers, rendered HTML otherwise.

**Keep the 403 status code.** Returning 403 rather than redirecting is a deliberate choice — it avoids leaking which admin routes exist (documented in the README's Security section). This issue is only about presentation for browser requests.

### Acceptance criteria

- [ ] A `templates/403.html` exists, styled consistently with `404.html` / `500.html`
- [ ] Browser requests to an admin route as a non-admin render that page; `/api/` and JSON-preferring requests still get the JSON body
- [ ] The status code stays 403 in both cases, and the response reveals nothing about which admin routes exist
- [ ] A test covers both branches (HTML and JSON) — `tests/test_admin_security.py` is the natural home

### Files involved

- `auth/decorators.py` — around line 32
- `app.py` — error handlers, around line 1738
- `templates/403.html` (new)
- `tests/test_admin_security.py`

---

## 6. "Custom Series" is offered for more than 2 teams, then hard-fails

**Labels:** `good first issue`, `bug`, `tournaments`

### Description

`TournamentEngine.get_available_modes()` offers Custom Series to any tournament with 2 or more teams:

```python
if num_teams >= 2:
    ...
    modes.append((self.MODE_CUSTOM_SERIES, 'Custom Series',
                  'Design your own series between two teams'))
```

But `_generate_custom_series()` rejects anything other than exactly two:

```python
if len(team_ids) != 2:
    raise ValueError("Custom series requires exactly 2 teams")
```

So a user selecting 6 teams sees "Custom Series" in the mode picker, chooses it, and hits a `ValueError` at fixture generation. The mode's own description already says "between two teams" — the availability check is simply too loose.

### Acceptance criteria

- [ ] Custom Series appears in `get_available_modes()` only when `num_teams == 2`
- [ ] The other modes' availability rules are unchanged
- [ ] A test in `tests/test_tournament_engine.py` asserts Custom Series is present for 2 teams and absent for 3+
- [ ] The UI mode picker no longer shows an option that can't be completed

### Files involved

- `engine/tournament_engine.py` — `get_available_modes`, around lines 118–156
- `tests/test_tournament_engine.py`

---

## 7. Knockout rounds beyond 16 teams get generic names, and `STAGE_KNOCKOUT_R1` is dead

**Labels:** `good first issue`, `tournaments`, `cleanup`

### Description

`_get_knockout_round_name()` maps the number of teams remaining onto a stage name:

```python
if teams_at_round == 2:    return self.STAGE_FINAL
elif teams_at_round == 4:  return self.STAGE_KNOCKOUT_SF
elif teams_at_round == 8:  return self.STAGE_KNOCKOUT_QF
elif teams_at_round == 16: return self.STAGE_KNOCKOUT_R2
else:                      return f'round_{round_num}'
```

Two problems:

1. `STAGE_KNOCKOUT_R1 = 'knockout_r1'` is defined on the class but never returned anywhere — grep confirms line 57 is its only occurrence. It's dead.
2. A 32-team knockout produces the stage string `round_1` for its opening round, mixing a naming convention (`knockout_*`) with an ad-hoc one in the same bracket. Any UI or query that filters or sorts on `stage` sees an inconsistent vocabulary.

Either wire `STAGE_KNOCKOUT_R1` into the 32-team case, or remove the constant if brackets that large are out of scope — but don't leave it defined and unreachable.

### Acceptance criteria

- [ ] `STAGE_KNOCKOUT_R1` is either returned for the appropriate round or removed entirely
- [ ] Stage names within a single bracket follow one consistent convention
- [ ] `tests/test_tournament_engine.py` covers round naming for 4-, 8-, 16-, and 32-team brackets
- [ ] Existing tournaments' stored `stage` values still resolve correctly (no rename of an existing constant's value without a migration)

### Files involved

- `engine/tournament_engine.py` — stage constants around lines 57–60; `_get_knockout_round_name` around line 480
- `tests/test_tournament_engine.py`

---

## 8. No test file for `routes/webhook_routes.py`

**Labels:** `good first issue`, `tests`

### Description

`routes/webhook_routes.py` (209 lines) handles inbound GitHub `issues` webhooks: it verifies an HMAC SHA-256 signature, deduplicates deliveries by `X-GitHub-Delivery`, and flips `ExceptionLog.resolved` when an issue is closed or reopened. Nothing in `tests/` references the module.

This is security-relevant code — signature verification is the only thing standing between a public endpoint and arbitrary writes to `ExceptionLog` — and it's a good first test to write, because the whole surface is one POST endpoint driven entirely by headers and a JSON body. No simulation state, no match fixtures.

The module's docstring already specifies the expected behavior for each GitHub action, which reads almost directly as a list of test cases.

### Acceptance criteria

- [ ] A `tests/test_webhook_routes.py` exists, following the structure of `tests/test_core_routes.py`
- [ ] Tests cover: valid signature accepted, invalid/missing signature rejected, replayed `X-GitHub-Delivery` handled idempotently, and non-`issues` events ignored
- [ ] Tests cover the `closed` → `resolved=True` and `reopened` → `resolved=False` transitions
- [ ] Tests use the existing fixtures in `tests/conftest.py` and require no live network access

### Files involved

- `routes/webhook_routes.py`
- `tests/test_webhook_routes.py` (new)
- `tests/conftest.py` (fixtures, reference only)
- `services/github_issues.py` — `verify_webhook_signature`

---

## Also considered

Scoped out as too large for a first issue, but worth tracking separately:

- **`routes/player_pool_routes.py` (778 lines) has no test file.** Real coverage gap, but too broad for a newcomer — better split per endpoint group once someone knows the module.
- **`routes/admin_issue_routes.py` (211 lines) and `routes/support_realtime.py` (216 lines) have no test files.** Similar to issue #8 in shape; hold them back until #8 establishes the pattern.
