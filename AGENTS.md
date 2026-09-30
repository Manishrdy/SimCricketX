# Notes for AI agents

## Branching

- All development happens on the `dev` branch only. Check out `dev` before
  making changes; do not commit feature or fix work directly to `main`.
- `main` is production only. It receives changes solely by merging `dev`
  (or an approved PR into it) when the user decides to release.

## Test accounts

For any testing that needs logged-in users (community board, admin pages,
permissions), use the five shared QA accounts instead of creating users:

- Guide: `.claude/TEST_ACCOUNTS.md` (local, gitignored)
- Credentials: `.claude/test-accounts.json` (local, gitignored; never commit or share)
- Tooling: `scripts/dev_test_accounts.py`
  - `seed` creates or resets the accounts in the local DB
  - `logged_in_client(app, key)` gives a logged-in Flask test client without a password

If the JSON file is missing (fresh clone), run
`.venv/bin/python scripts/dev_test_accounts.py seed`. It generates new
credentials locally. Local/dev databases only.