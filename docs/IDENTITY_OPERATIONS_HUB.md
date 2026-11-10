# Identity operations hub

Module 11 connects the existing identity workflows without adding tables,
policy mutations, background scans or new permission types.

## Entry points

- `/admin/identity`: workload counts, delivery/cleanup attention, campaign
  deadlines, configured enforcement and recorded classifier health.
- `/admin/identity/accounts/<stable_id>`: persisted restrictions, latest unexpired
  device sightings, recent network observations, campaign membership/holds,
  duplicate reviews, account activity and durable merge links. Admin Users links
  here. A retired merged identity remains inspectable through its receipt.
- `/admin/identity/audit`: unified activity and receipts for duplicate reviews,
  signup/return decisions, network decisions, email changes, campaigns and merges.
- `/admin/identity/approvals`: existing verified signup/return and network requests.
- `/admin/identity/exemptions`: existing scoped grants, including active, expired,
  revoked and used states. Administrator role exemptions are described separately.
- `/admin/identity/policy`: read-only adopted rules and configuration status, with
  links to existing IP whitelist/blocklist administration.

All identity screens share navigation. Existing review, proof, ownership,
reason, CSRF, expiry and confirmation checks remain in their owning workflow.
The hub links to those actions rather than implementing alternative writers.
Active administrator authorization is revalidated against the database;
impersonation is rejected. Responses use private/no-store caching, no-referrer
and noindex headers on hub routes.

## Reading the screens

Counts are current database projections, not new evidence. Blocked-attempt counts
refer to retained deduplicated groups, not individual applicants. Network events
are historical observations, not a live count of restricted users. Classifier
credentials being configured does not establish working connectivity. Feed
freshness uses the existing 48-hour policy. Pages perform no provider requests,
mail delivery, scans, filesystem inspection, cleanup or enforcement changes.

Activity is ordered by event time with deterministic tie-breaking. Email-change
and merge requests have recorded creation and completion events; their current
status is shown separately. The hub does not invent cancellation/expiry event
timestamps where the underlying workflow never recorded them. Completion actor
comes from the execution receipt, not necessarily the request creator. Missing
actors, pruned details and missing receipts are identified honestly.

Activity filters accept workflow, action, account email/stable ID, and inclusive
UTC date ranges. Approval/exemption dates filter request creation time. Account
association uses retained stable IDs and recorded/current addresses; this is not
an inferred ownership reconstruction for historical unlinked records. Displayed
pages contain at most 30 items; SQL applies filters, ordering and pagination
before results are loaded. Only the displayed page's receipt data is fetched,
in bounded batches. Dashboard counts are database aggregates.

Grant states are calculated at read time, so expired approvals are not presented
as active while waiting for scheduled cleanup. An approved grant with no expiry
is shown as unavailable. The hub does not repair or extend grants. Signup grants
remain seven-day, email-bound, single-use and scoped; network exemptions remain
seven-day, account-bound and network-only. Open the original workflow for the
recorded evidence, reason, actor, decision and revocation controls.

## Retention and sensitive data

Review evidence/actions and closed review cases honor their existing 180-day
limits. Signup and network records honor their workflow visibility and action
expiry predicates. Closed campaign member history expires after 180 days once
file cleanup finishes; pending cleanup keeps its operational history visible.
Campaign-level events remain while the campaign has retained members. Existing
email-change and merge receipts remain durable; no retention extension or new
copy of audit data is introduced.

Raw cookie/fingerprint hashes, mailbox proofs, credential values and arbitrary
receipt fields are never projected into the hub. Backup references are restricted
to recognized operation basenames, never paths or downloads. A reference does
not assert that a 30-day backup still exists. Recovery remains operator-managed.

## Operational boundaries

Policy is read-only. Domain overrides, editable scoring/thresholds, browser
activation switches, blanket grants and marker-removal controls remain outside
this module. Existing IP management does not imply an identity-policy exemption.
Configuration presence does not verify marker-key continuity, mail delivery,
backup capacity, worker consistency or installed schedules. Keep existing cleanup
and reminder jobs configured according to their owning feature documentation.

Module 12 owns production-copy rehearsal and rollout. No production activation,
account modification, notification, scheduler installation or deployment is
performed by the hub implementation or its tests.
