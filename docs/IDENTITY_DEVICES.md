# Device evidence (Feature 3)

Feature 3 collects evidence; it does not block registration, restrict login, ban
accounts or merge identities. Those remain separate features in the local plan.

## Collection and storage

When enabled, signup/login pages issue a signed, random first-party
`__Host-scx_device` cookie: Secure, HttpOnly, SameSite=Lax, Path=/, no Domain,
rolling 180-day Max-Age. Authenticated HTML responses also renew it. The cookie
requires HTTPS (or a browser's trusted localhost exception). There is no insecure
production fallback and no localStorage resurrection. Cookie state is cached
only within the current request.

The browser sends exactly language, timezone, screen dimensions and platform.
The server validates their types/lengths, normalizes case and screen orientation,
and computes a versioned HMAC. No canvas/WebGL, installed fonts, raw IP or other
signals are added by this feature. Missing/malformed fingerprints and unconfirmed
cookies are recorded explicitly. Basic fingerprints are easy to spoof and can
collide; neither a fingerprint nor a signed device cookie authenticates a person.

Successful signup/login records evidence after the account/session commit. Failed
login attempts never attach evidence to the account they name. A CSRF-protected,
rate-limited POST `/api/identity/device` handles existing authenticated sessions;
a signed-session marker suppresses repeat collection until a new login session
or expiry. The browser retries on a later page if storage fails. Admin
impersonation is excluded so an admin's device is not attributed to another user.
Existing guards still protect the collection endpoint; evidence is never a way
around a restriction. Evidence collection failures do not undo successful auth.

`identity_device_sightings` links a stable user UUID to key ID, keyed cookie hash,
keyed fingerprint hash, availability/confirmation flags and first/last observation
times. It contains no raw cookie/browser values, credentials, session tokens or
email addresses. Duplicate observations upsert the same row. Schema creation is
registered in precheck and automatically included in migration verification;
`db.create_all()` creates the same schema. It has no historical device backfill.

The notice on signup/login links to `/privacy/device-data`, a public privacy
policy section. It explains purpose, signals, retention and limitations. This is
specific to device evidence, not a replacement for a site-wide privacy policy.

## Enablement and key rotation

Collection is **disabled by default**. Set `IDENTITY_DEVICE_KEY` to a dedicated,
cryptographically random secret of at least 32 bytes in protected server config.
Do not reuse a password, commit the key or put it in logs. It is intentionally
independent of Flask's session secret. Deploy/restart using the existing server
procedure only after provisioning the key and retention cleanup. This change
has not provisioned keys or activated production collection.

For rotation, retain the old key as `IDENTITY_DEVICE_PREVIOUS_KEY`, set a fixed
UTC ISO timestamp in `IDENTITY_DEVICE_PREVIOUS_UNTIL` (for example the timestamp
chosen by the operator, ending in `Z`), and install the new current key. The
previous-key deadline must be no more than 180 days from configuration validation;
never reset that fixed deadline on restart. During overlap, old signatures are
accepted, cookies are re-signed by the current key, and observations are hashed
under both keys. Remove the old key at its deadline. Expired old keys are ignored
at request time even without a restart. Emergency key removal intentionally
breaks recognition of affected old cookies; do not claim uninterrupted device
matching after key loss or removal. Non-returning devices cannot be rehashed
without their original signals, which are deliberately not stored.

## Retention and cleanup

Evidence is valid strictly less than 180 days after its last observation.
`live_sightings()` excludes expired rows immediately; future matching code must
use this expiry-aware interface. Each collection purges expired evidence, and
an explicit CLI handles quiet periods:

```sh
.venv/bin/flask --app app:create_app purge-device-evidence
```

Run that command daily using the deployment's working directory/environment
(and its actual venv path), including while collection is disabled. No scheduler
is installed automatically. Cleanup deletes only expired device-evidence rows;
it never deletes users. Exact expiry affects matching immediately, while physical
purge can lag until the next collection or daily job. Backup retention is a
separate unresolved operational policy; do not claim this job erases backups.

This feature does not establish permanent device bans, return tombstones, audit
retention or signup enforcement. Production ingress acceptance from Feature 1
and later account-policy rollout remain separate.

## Validation

Run focused tests with the shared QA plugin and isolated test databases:

```sh
.venv/bin/python -m pytest -p tests.qa_accounts_plugin tests/test_identity_devices.py --no-cov -q
```

The test suite covers signed cookie flags/expiry/tampering, basic signal parsing,
missing signals, real signup/login hooks, failed-login isolation, per-session
collection, rotation, retention boundaries, idempotent schema, CSRF and evidence
storage failure. Broader migration verification may require OS process-inspection
permission for an existing unrelated migration's process-holder check.
