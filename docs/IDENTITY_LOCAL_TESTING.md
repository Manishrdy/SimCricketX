# Local acceptance testing: identity modules 1–11

Start with the automated acceptance suite below. It uses isolated temporary
SQLite databases and the five shared QA identities, and mocks outbound identity
mail/classifier operations. It does not require modifying your `.env`, enabling
production policy or starting a campaign in the development database.

From the repository root:

```sh
.venv/bin/python -m pytest -p tests.qa_accounts_plugin \
  tests/test_client_ip.py \
  tests/test_identity_foundation.py tests/test_identity_devices.py \
  tests/test_identity_scan.py tests/test_identity_review.py \
  tests/test_identity_signup.py tests/test_identity_network.py \
  tests/test_identity_email_change.py tests/test_identity_campaign.py \
  tests/test_identity_merge.py tests/test_identity_hub.py \
  tests/test_admin_workspaces.py --no-cov -q
```

Migration verification is a separate test on temporary databases:

```sh
.venv/bin/python -m pytest tests/test_migration_verify.py --no-cov -q
```

On macOS, migration rehearsal may require ordinary terminal process-inspection
permissions; a sandbox `sysctl` error is not a migration success. No QA seeding,
campaign deletion or merge test should target production.

## Browser preparation

1. Use the existing local dev launcher and a backed-up development environment.
   Start it once to run the registered schema precheck. A normal development
   launch with `SIMCRICKETX_TEST_MODE=1` still uses the repository database unless
   an explicit test database URI was set; that variable alone is not isolation.
2. Use `.claude/TEST_ACCOUNTS.md` and the existing local credentials in
   `.claude/test-accounts.json`. Do not create additional users for testing.
   For ordinary non-destructive browser checks, reset the shared QA accounts:

   ```sh
   .venv/bin/python scripts/dev_test_accounts.py seed
   ```

   This writes the local database. Do not use it on a campaign/merge fixture that
   you want to preserve: resetting users does not undo identity receipts, aliases,
   reservations or campaign membership.
3. Open Admin → Identity with the shared admin account. First inspect the overview,
   policy, reviews, activity, approvals, exemptions, email changes, campaign and
   merge screens with enforcement disabled. Filters and GET pages must not apply
   account changes or send mail.
4. For enforcement tests, use a disposable development checkout/environment with
   isolated database **and artifact directories**. Copying only SQLite is not full
   isolation: gameplay, exports and Community uploads also live on disk. Use the
   automated isolated fixtures for merge/deletion scenarios rather than deleting
   or renaming the persistent shared QA users.

## Configuration to verify

The identity options are now listed in `.env.example`; real secrets belong only
in your local ignored environment. Device collection requires a dedicated
`IDENTITY_DEVICE_KEY`; signup requires that key plus a stable, separate
`IDENTITY_SIGNUP_KEY`. Both must be at least 32 bytes. Generate values locally,
keep them private and do not regenerate them on every restart.

- `IDENTITY_SIGNUP_ENFORCE=true` enables signup checks. Leave it false for initial
  screen inspection. Merge execution and campaign deletion require enforcement.
- Device cookies are Secure/HttpOnly. Use local HTTPS for cookie-roundtrip
  acceptance; do not disable cookie protections to make a test pass.
- `IDENTITY_NETWORK_ENFORCE=true` requires the stable signup key and a configured
  ProxyCheck key. Loopback/private IPs do not demonstrate real VPN detection.
  The automated suite supplies controlled clean/flagged/unknown classifications;
  it verifies restrictions without sending visitor IPs to a provider.
- `IDENTITY_EMAIL_BACKUP_DIR` may select a private mode-0700 directory. Omit the
  variable for the default; do not set it to an empty string. Account operations
  require successful backups and sufficient disk space.
- Email links come from `email.app_url` in `config/config.yaml`, not the browser
  request host. Confirm the local origin before any manually authorized delivery.
  `SIMCRICKETX_TEST_MODE` does **not** suppress real email or classifier calls.
  QA `example.com` addresses cannot receive mail. Use the automated mailbox-proof
  fixtures for those flows; a missing Resend key should show a delivery failure,
  not successful verification. Do not weaken proof checks to test a merge.

## What to check

| Module | Local acceptance focus |
|---|---|
| 1 | Forwarded-IP spoofing/trust and IPv4/IPv6 tests; live ingress is still module 12. |
| 2 | Accepted/rejected email formats, aliases and canonical collision handling. |
| 3 | Disclosure, cookie roundtrip, basic fingerprint collection and expiry. |
| 4–5 | Read-only scan preview, explicit import, case filters/assignment/history and stale decisions. |
| 6 | Same-device/fingerprint blocks, exact-IP cap, private appeal, scoped single-use approvals and concurrent signup tests. |
| 7 | Flagged member becomes Community-only; direct APIs/sockets remain blocked; clean/unknown/exempt cases follow the approved rules. |
| 8 | Both-mailbox proof, safe email preview, stale/duplicate confirmation, session revocation and backup failure. |
| 9 | Preview versus activation, fixed deadline, private support, holds, notices, correction and expiry deletion/cleanup in isolated fixtures. |
| 10 | Ownership proofs, survivor selection, private history, conflicts, linked squad/auction players, preserved snapshot ratings, stale/rollback behavior and source login retirement. |
| 11 | Workload counts, filters/pagination, expiry/retention, safe receipts, retired-account links and shared navigation. |

Use separate browser profiles when comparing device identities. Separate profiles
may still have identical basic fingerprints; that is an intended review trigger.
A new IP alone does not bypass a matching device/fingerprint. Switching among QA
accounts in one profile intentionally creates shared-device evidence.

The merge preview now lists squad/auction pool links that must move before a
conflicting override is discarded. Snapshot ratings/history remain unchanged;
third-party ownership or an unclassified dependent table blocks the merge.

Record any unexpected result with the screen/action, expected versus actual
behavior and relevant non-secret error text. Production migrations, live ingress,
restore rehearsal, scheduling and activation remain module 12.
