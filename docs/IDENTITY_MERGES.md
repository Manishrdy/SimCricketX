# Ownership-reviewed account merges

Module 10 adds `/admin/identity/merges` and mailbox confirmation at
`/identity/merge/verify/<request>/<side>/<token>`. The Identity workspace and
individual duplicate cases link to the workflow. Only active administrators,
without impersonation, can create requests, preview or confirm merges. Every
mutation uses CSRF-protected POST. GET and impact preview do not change accounts.

## Review and confirmation

1. Start from an open/investigating duplicate case. Choose source and survivor.
   Document established common ownership (20–2000 characters); matching network,
   cookie or fingerprint evidence alone is insufficient.
2. Both mailboxes confirm single-use, hashed proofs within 24 hours. A documented
   admin recovery exception may replace **source** mailbox proof only. The
   survivor must already have an acceptable verified address. Failed delivery is
   visible; cancel and recreate the request to retry. Cancelling/expiry revokes
   outstanding proofs. Verification never applies a merge by itself.
3. Preview reference counts, source/survivor conflict keys, renames, losses,
   private-history access and security effects. Confirm the review checkbox and
   type the exact survivor email. Signed previews expire after 15 minutes and
   bind the administrator, request, evidence, case, account data and artifacts.
   Changed state requires a fresh preview. Repeating the same successful
   confirmation returns its existing receipt.

Admin accounts, bans, password remediation, mandatory email correction, pending
campaign membership and active lockouts block merging until separately resolved.
Both accounts must have distinct, unchanged stable identities. A request cannot
overlap another pending merge for either account. Unfinished live/saved matches,
checkpoint files, active/paused auctions, unreadable artifacts and unclassified
references or owner-dependent uniqueness constraints fail closed.

## Data and conflicts

The survivor keeps its email/password/profile/stable UUID. Distinct gameplay
objects retain their IDs and histories. Conflicting names and team short codes
receive deterministic visible suffixes; duplicate tour/tournament creation tokens
are renamed too, preserving each object. For colliding player overrides, format
settings, stadium-adoption keys, votes and banner dismissals, the survivor's row
wins; non-conflicting source rows move. Vote counts are recalculated. Distinct
stadium assets remain even when a duplicate adoption link is discarded.

Source private Community content becomes accessible to the survivor. Current
ownership and access links move; historical address/audit snapshots remain.
The durable merge receipt records source stable identity and original per-table
row keys as provenance. Device sightings transfer, coalescing identical evidence
with earliest/latest timestamps and cookie confirmation retained. Source network
appeals/history transfer with exemptions revoked; survivor exemptions stay as-is.
The stronger Community mute survives. Network classification is evaluated again
on later access.

Both users' sessions and reset/verification/email/password-change tokens are
revoked. Pending email-change requests and canonical signup approvals/proofs are
cancelled. Source login/account is removed using SQL after ownership transfer;
ORM delete cascades are not invoked. Canonical reservations remain, and a keyed
`merged` marker prevents ordinary deleted-account return. Same-canonical merges
use the reservation and durable alias receipt rather than a marker that would
also block the survivor. No source email alias login is introduced.

Completed match JSON and exports are not rewritten. Current database match
ownership and hash-pinned archive ownership govern access. The source can no
longer access them. Current-process completed match caches are evicted; existing
HTTP and socket guards revalidate sessions and persisted ownership across workers.

## Deployment and recovery prerequisites

The additive `add_identity_merge` migration creates one table without backfill.
All workers must run the same release and share the database-specific admission
lock directory. Configure working mail, a stable `IDENTITY_SIGNUP_KEY`, enabled
`IDENTITY_SIGNUP_ENFORCE`, and private `IDENTITY_EMAIL_BACKUP_DIR` storage with
adequate free space. Keep signup enforcement enabled while retired identities
must stay blocked: the legacy disabled-enforcement signup path does not enforce
identity reservations. Existing marker keys must match the configured key.

Execution takes the shared exclusive admission lock and SQLite `BEGIN IMMEDIATE`,
revalidates the plan, creates a verified consistent SQLite/WAL backup, applies all
updates and writes the receipt/review audit in one transaction. Backup, audit,
constraint or final foreign-key-check failure aborts. Busy admission returns a
retryable error. External match/export/Community files are not changed or deleted,
so the database backup restores their previous access ownership; there are no
merge file mutations requiring an additional artifact backup.

Operation backups are mode 0600 in a private mode-0700 directory, with names
`email-merge-*.sqlite3`. Schedule the existing `purge-email-change-backups` command
for 30-day UTC retention, and `purge-merge-requests` to close expired proofs.
Durable merge receipts and source/survivor links remain; a global audit-retention
policy is still a rollout decision. Backups are operator-only, never downloadable
from the admin UI.

Before production enablement, rehearse on a production copy: validate reference
coverage, conflicting settings, private access, completed exports, session
revocation, backup failure and recovery. To recover, stop every worker and writer,
preserve the current database/WAL and artifacts as an incident recovery point,
restore the selected verified backup using the established SQLite restore
procedure, run integrity/reference checks, then restart all workers. Reconcile
writes since the backup and revoke restored authentication sessions/tokens before
reopening access. This is whole-database recovery, not an automatic per-user undo.
Production activation, mail delivery, merges and scheduler installation are
separate operator actions, not performed by local implementation/testing.
