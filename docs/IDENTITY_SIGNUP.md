# Signup enforcement and private review

Feature 6 adds local, opt-in enforcement. It does not activate production policy,
restrict existing logins, classify VPNs, merge users or start a deletion campaign.

## Operator configuration

`IDENTITY_SIGNUP_ENFORCE=true` enables signup checks. Default is disabled.
Provision `IDENTITY_SIGNUP_KEY` (a dedicated random secret of at least 32 bytes)
and the existing `IDENTITY_DEVICE_KEY` before enabling. Never commit either key.
Use HTTPS so the Secure host-only device cookie can be returned. Complete the
trusted-proxy deployment in `deploy/nginx/IDENTITY_INGRESS.md` first: an unconfigured
proxy may make all visitors appear to use the same IP. Preserve the stable signup
key in secret backups. It hashes exact IP counters and deletion-email identities;
changing it is not a supported rotation procedure. A mismatch with existing
marker key IDs refuses mutations rather than silently ignoring those markers.
Device-key rotation remains the separate documented Feature 3 process.

Rehearse additive `add_identity_signup` migration and the acceptance suite on an
isolated database first. The normal migration precheck registers it. Before
production activation verify device-cookie round trips, email delivery, human
verification, the public appeal flow and admin access. No deployment, scheduled
job or key provisioning is performed by this feature.

For rollback, disable `IDENTITY_SIGNUP_ENFORCE` and restart. This deliberately
returns signup/deletion to legacy behavior and stops creating new markers; it is
an emergency policy rollback, not continuing identity protection. Preserve all
new tables and secrets. Do not delete policy history to recover service.

## Enforced behavior

Public signup validates new email syntax/IDNA, rejects new plus tags and known
disposable domains, and reserves canonical identity. Existing stored-address
login/recovery semantics are unchanged. Unknown ordinary domains remain allowed.
Canonical uniqueness, the account insert, exact-IP 24-hour success counter,
device sightings and any approval use share one SQLite write transaction.
A created but unverified account consumes the signup cap. Refusals do not.
Exact IPv6 addresses are used, not prefixes. Legacy successful signups are not
backfilled from untrusted historical IP data.

A returned server-signed cookie and valid basic fingerprint are required. Live
confirmed-cookie or exact valid-fingerprint matches block pending admin review.
Device evidence expires after 180 days; invalid/future/orphan observations do not
block. IP/subnet/name similarity is not a permanent identity ban. Missing keys or
storage failures cannot silently bypass checks. Existing Turnstile, proof of work
and signup request limits remain in place.

The authenticated, non-impersonating admin-create route bypasses device/IP checks
only. It retains valid email, canonical uniqueness, tombstones and unverified
account creation. Other callers cannot claim the bypass. The existing email-change
helper also reserves a new canonical address and checks deletion markers within
its write transaction when enforcement is on; complete reference repointing and
mailbox-proof redesign remain Feature 8. Old canonical reservations are retained
until reviewed lifecycle resolution. Do not infer a completed Feature 8 from this
narrow collision guard.

## Appeals and approval

`/signup-review` is a private, no-login form with CSRF, Turnstile, proof of work,
a honeypot and a persisted cap of five requests per exact IP per hour. The ordinary
Flask limiter adds an early request cap. Email delivery uses the configured
canonical application URL, not the request Host header. Delivery failure is
visible and remains retryable within the cap. There is no outbound email in tests.

One verified open request exists per canonical email. A one-hour, one-use mailbox
link requires a confirming POST; email scanner GETs do not consume it. After
verification, a signed browser session can view status/use its email-bound grant
for 30 minutes. Request a new verification link to check status later. Raw tokens
are never stored. The link is a mailbox capability; do not share it.

After the signup key is configured, admin review can be inspected before
enforcement activation. Admin → Identity → Signup reviews includes verified requests, paginated status
filters, refused attempts, current matched-account evidence and decision history.
Review uses a required reason/ownership-evidence note and confirmation, version
checks and atomic audit. Approvals expire after seven days, apply only to selected
exceptions, and are consumed only by a successful signup. Exceptions: device,
fingerprint, missing signals, exact-IP cap, deleted-account return. Approval cannot
bypass invalid email policy, a live canonical account, or a banned/merged marker.
A wrong disposable-domain classification can be appealed, but this feature has no
domain override editor; classification changes require the existing reviewed
blocklist update process. A rejected applicant sees only status, not private notes
or another account's identity. Admin impersonation cannot act on reviews.

## Deletion and retention

Existing user deletion records a keyed canonical-email marker in the same
transaction when enforcement is on. It records `deleted` or `banned`, a generation
identifier and timestamp. No historical deletion data is invented. The existing
owned-data deletion behavior is unchanged. A deleted identity needs an approval
bound to that marker generation; another deletion invalidates old approvals.
Approved return creates a fresh empty account and never restores deleted data.
Markers remain until an explicitly authorized admin removal operation; there is
no automatic marker expiry or removal button in this delivery. Banned/merged
markers cannot use the return shortcut. Backup retention remains a rollout decision.

Run the following daily using the application's configured environment, alongside
`purge-device-evidence` and `purge-identity-reviews`:

```sh
flask --app 'app:create_app()' purge-signup-evidence
```

The command removes expired counters (24 hours), mailbox links/messages (one hour),
attempt evidence (180 days or earlier underlying device expiry), and closed
appeals/action history (180 days after closure/use/expiry). Open requests retain
workflow content; individual old actions expire after 180 days. Expired content
is hidden/ignored immediately, even before physical cleanup. Signup mutations
also run cleanup. Repeated attempts update the same identity/reason record.
Minimal deletion markers are not purged. No scheduler is installed automatically.

## Validation

Run with shared QA identities in disposable test databases:

```sh
.venv/bin/python -m pytest -p tests.qa_accounts_plugin tests/test_identity_signup.py tests/test_identity_review.py tests/test_identity_devices.py tests/test_identity_foundation.py --no-cov -q
.venv/bin/python -m pytest tests/test_migration_verify.py --no-cov -q
```

The signup suite covers canonical/device/IP/approval races in independent
processes, required signals, expiry boundaries, scoped grants, verification,
refusals without accounts, audit rollback, admin/public separation, CSRF,
impersonation, retention, schema reruns and disabled-mode behavior.
