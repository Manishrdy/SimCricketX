# Verified email changes (Feature 8)

## Workflow

Open **Admin → Identity → Email changes**, or the verified email-change link on
an account's Actions tab. Requests do not immediately rename accounts.

- Record the reason and both addresses. Both mailboxes must confirm within 24 hours.
- If the old mailbox is unavailable, explicitly select the recovery exception and
  record how ownership was established. The new mailbox always requires proof.
- Admin-account email changes remain excluded, including during impersonation.
- Review the impact, type the exact new address, and confirm within 15 minutes.
  Changed account data or evidence invalidates the preview.
- Success preserves the stable UUID, password, profile, owned records, roles,
  bans and non-email restrictions. New-mailbox proof satisfies email verification.
  All active sessions and reset, verification, pending-email and password-change
  tokens are revoked. Sign in using the new stored address.
- Completion notices go to both addresses. Failed notices remain visible and can
  be retried from the request or the CLI. Delivery is at least once: a crash after
  delivery but before its receipt can cause a duplicate notice.
- Failed verification delivery is visible. Cancel and recreate the request to
  send fresh links. Cancelling or expiring a request invalidates its proof tokens.

Proof links require an explicit CSRF-protected POST; opening a link never consumes
it. Only token hashes are stored. Admin pages are private/no-store; verification
pages have no third-party assets and send no referrer. Rate limits supplement
existing administrator authentication. Never put credentials into recovery notes.

The old immediate `update_user_email()` writer refuses changes. Existing forced
email correction remains a narrow self-service exception: an authenticated member
already flagged for mandatory correction requests a replacement, proves that
mailbox, and only then is repointed by the same transaction/backup engine. This
neither introduces ordinary self-service email changes nor starts a campaign.
A failed correction can be retried from the existing mandatory-correction form;
an administrator can finish a verified request if gameplay/backup prerequisites
previously prevented completion. A 30-day campaign remains Feature 9.

## Data and concurrency

`IdentityEmailChange` stores the request, hashed proofs and durable completion
receipt. `IdentityArchiveOwner` pins existing ZIP exports to the stable UUID and
content hash, preserving the original files without renaming or rewriting them.
Deletion nulls a pinned archive's owner, preventing a later reused email from
inheriting it. Pinned content that has changed is denied until reviewed.

Explicit FK and non-FK classifications fail closed when the schema acquires an
unclassified account reference. Current ownership links move; historical email,
actor-label and support/evidence snapshots remain historical. Stable-ID evidence
and exemptions need no repointing. Canonical checks and deletion markers apply
regardless of signup enforcement. Missing/rotated marker keys fail closed. Old
and new canonical reservations are retained; an old address is not a login alias.

Incomplete live/saved matches block changes. Completed match JSON remains an
unchanged snapshot, with access checked against current database ownership.
Regenerating an archive uses the current owner in a separate runtime copy, so
archiver database writes cannot restore the historical owner.

A nonblocking OS file lock shared across workers excludes HTTP/socket activity
during application. Busy operations return a retry response; there is no forced
interruption of a user's ongoing request. SQLite `BEGIN IMMEDIATE` serializes
plan revalidation, backup and reference updates. An audit/update failure rolls
back all DB mutations. Repeating the same valid confirmation returns its existing
receipt without another rename/backup. Socket sessions are rechecked even when
network policy enforcement is disabled.

## Deployment prerequisites and operations

The additive `add_identity_email_change` migration creates both tables; it changes
no existing users and is safe to rerun. Run the registered migration path and
restart all application workers together. Every worker must use the same SQLite
file and instance directory (and thus the same admission lock). This design is
for the existing single-host SQLite deployment, not distributed workers.

`IDENTITY_EMAIL_BACKUP_DIR` defaults to `instance/identity-email-backups`. It must
be outside served/static directories, owned by the app operator, and mode `0700`.
Backups are mode `0600`; symlink directories are refused. Keep sufficient space
for a full SQLite backup per successful or rolled-back attempted operation.
The app's configured email URL must be the public HTTPS origin; use the existing
mail configuration and verify actual delivery during deployment rehearsal.

The required SQLite backup includes committed WAL data, is taken while writes
are excluded, passes `quick_check`, and is flushed before any identity updates.
A backup failure aborts the change. Failed attempts can leave valid pre-operation
backups; these use the same 30-day retention. Backups contain the entire database
and are operator-only; there is no UI download endpoint.

Schedule these commands using the deployment's existing Flask environment:

```sh
flask --app app purge-email-change-backups
flask --app app retry-email-change-notices
```

Run backup cleanup daily and notice retries at the operator's chosen interval.
Cleanup removes only this feature's files aged at least 30 days and expires
pending proof requests. It does not alter the independent scheduled-backup policy.
No scheduler is silently installed by this feature. Completion receipts remain
available for audit; final organization-wide audit retention is a rollout policy.

Rollback before a commit is automatic. Recovery after a commit requires an
operator database restore, not per-account undo: stop all writers, preserve the
current database, verify the chosen receipt/backup, restore the SQLite database
and handle its WAL/SHM consistently, then run integrity checks and restart all
workers. A full restore also reverts unrelated intervening changes, so rehearse
and approve that recovery operationally before production use. Completed match
and ZIP bytes are not modified by this feature.

Feature 8 validation uses shared QA identities in isolated databases, with mail mocked.
No production migrations, email changes, scans, activation or scheduler setup are
part of local implementation.
