# Legacy email correction campaigns

Feature 9 is dormant until an administrator activates a reviewed campaign at
**Admin → Identity → Email campaign**. Startup creates four additive tables only.
There is no automatic enrollment, scheduler installation, production activation,
email delivery or deletion during migration.

## Policy and member flow

Preview identifies malformed/unsupported, plus-tag and known disposable addresses.
Admins are listed separately and excluded. Unverified status alone is not an
invalid address. Canonical collisions are shown for separate duplicate review;
use Identity's scan-preview/import workflow to open review cases. No automatic
merge or deletion follows from a collision alone.

Activation requires a fresh, actor-bound 15-minute signed preview and typing
`ACTIVATE`. Enrollment/reasons are frozen and the UTC deadline is persisted at
activation + exactly 30 days. Restarts, redeploys and later blocklist changes do
not change it. Only one unresolved campaign may exist at a time.

Affected active sessions and subsequent sign-ins can access mandatory correction,
private support and necessary security endpoints. HTTP APIs receive a 403 with
`email_correction_required`; HTML redirects to correction; Socket.IO also checks
live enrollment. A legacy unverified account can authenticate into correction
only. Bans remain enforced. The login identifier accepts stored legacy values;
replacement addresses still pass the strict policy and mailbox verification.
Verifying the old mailbox or clearing `force_email_verify` cannot release campaign
restrictions. Feature 8 commits verified correction and campaign closure in the
same SQLite transaction, preserving stable identity/data and revoking sessions.

Support is private to the member and active administrators. The dedicated form
permits unverified/muted/new members without granting ordinary Community access.
A request does not create a hold. Admins can reply, grant/renew a reasoned hold
for 1–30 days from now, or release it. At expiry of a hold, the original campaign
deadline still applies. Successful correction is possible until deletion commits;
an uncompleted proof does not postpone deletion. Existing unsafe/unfinished-match
blocks in the email-change engine still require support/operator resolution.

## Before production activation

1. Complete Feature 12's production-copy audit and rehearsal. Review collision
   groups, excluded admins, affected-account counts and data-cleanup behavior.
2. Apply migrations and restart **all** workers with the same instance/admission
   lock directory and SQLite file. Confirm trusted ingress and scoped access.
3. Configure outbound email/origin, `IDENTITY_SIGNUP_KEY`, private backup storage
   (`IDENTITY_EMAIL_BACKUP_DIR`, mode 0700), available disk space, match/archive
   and Community-media roots. Backups are mode 0600 and require operator access.
4. Rehearse restoration of the paired database and artifact backups on an isolated
   copy. Do not restore a whole database over live service for an individual user.
5. Staff correction support, review the preview and explicitly activate. Configure
   the commands below using the deployment's virtualenv/configuration; this feature
   does not install timers. Starting the clock is a production operation.
6. Enable signup identity enforcement before enabling expiry deletion. Type
   `DELETE AT EXPIRY` on the campaign screen. This separate switch defaults off.
   Pausing deletion does not reset the clock or lift correction restrictions.

## Scheduled commands

With the application's environment and `FLASK_APP=app:create_app`:

```sh
.venv/bin/flask identity-campaign-tick
.venv/bin/flask purge-identity-campaign
.venv/bin/flask purge-email-change-backups
```

Run tick at least hourly; run both purge commands daily. Tick attempts reminders
at activation, seven days remaining and one day remaining, then processes eligible
deletions and retries file cleanup. SMTP delivery is at least once; failed sends
are visible in the campaign entry and retried by tick. No reminder is sent after
the original deadline. Delivery failure does not extend the deadline. Monitor
entry errors and artifact cleanup status; a successful command is not evidence
that every candidate was deleted. Competing workers serialize database operations;
nonblocking admission contention is retryable on the next tick.

## Deletion and recovery

Deletion rechecks enrollment, original identity, administrator status, deadline,
hold and deletion enablement under an exclusive per-database admission lock and
`BEGIN IMMEDIATE`. Signup enforcement must still be enabled. A committed correction
wins; if deletion commits first, outstanding proofs cannot recreate the account.
Missing/promoted-admin accounts are closed as excluded. Unknown schema references,
unreadable files, unsafe paths, marker-key mismatch or backup failure defer deletion
and keep the account. No success receipt is committed on audit failure.

Before deleting, create and verify a consistent SQLite backup (including committed
WAL) and a ZIP of the exact selected match/archive/media files. Both are private
operator recovery artifacts. The receipt identifies both. Their retention is
30 days; scheduled general backups retain their existing separate policy.

Delete credentials/profile, sessions/tokens, owned gameplay data and dependent
rows, private threads, the user's replies in other private threads, votes and
owned uploads/exports. Other members' replies in someone else's private thread
are detached from the removed parent and preserved. Preserve public discussions
with deleted-user attribution; clear associated account metadata and recalculate
vote/comment totals. Shared owned-gameplay conflicts defer deletion for operator
review. Completed email-change audit receipts retain their existing audit policy;
pending email-change proofs are removed.

File removal occurs after the database commit through a durable cleanup queue.
Missing files count as cleaned. Changed-content/unsafe-path failures remain pending
for operator review rather than deleting an unexpected file. Pending archives and
match snapshots are denied by ownership checks; Community media loses its DB row.
Same-process match caches are evicted, and all sockets revalidate live sessions.
A deleted account's original canonical identity receives a keyed marker even when
the campaign was developed with signup enforcement disabled. Banned/merged markers
are never downgraded. Returns require the existing scoped approval workflow and
an acceptable verified address, creating a new empty account.

Campaign member/action receipts are purged 180 days after closure; unresolved file
cleanup prevents removal of the necessary queue/receipt. Keyed deletion markers
remain until explicit administrator removal. Support text/address snapshots are
cleared on correction/deletion/exclusion. No ordinary email-change or account-merge
workflow is enabled by this campaign.
