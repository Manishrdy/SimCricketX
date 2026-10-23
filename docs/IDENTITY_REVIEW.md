# Admin identity reviews

Feature 5 adds **Admin → Identity** at `/admin/identity`. It is a review workspace: its actions do not ban users, grant signup exceptions, establish verified common ownership, merge accounts, change email addresses or activate the 30-day deletion campaign.

## Workflow

1. Open Identity and select **Preview duplicate scan**. This is an explicit, CSRF-protected action against the application's configured SQLite database. It uses the Feature 4 exact-match rules in one consistent read transaction. Merely opening a page never starts a scan or creates cases.
2. Review the pairs, reasons, observation dates and direct ownership counts. Confirm import to create or refresh one case per stable-ID account pair. The preview is signed, bound to the current admin, valid for 15 minutes, and revalidated under a SQLite write lock before any case write. Changed evidence, owned counts or case versions require a new preview. No browser-supplied database path or uploaded report is accepted.
3. Use the paginated queue to filter by account email, status or current evidence reason. Open a case to inspect both accounts, assign an administrator and add private notes.
4. Move Open → Investigating → Resolved or Dismissed. Open may also be dismissed directly. Closed cases may be explicitly reopened to Open. Every note, assignment and state change requires a reason and is committed with its audit entry. Resolution is a review conclusion only.

Repeated imports update an existing open pair, not duplicate it. Closed cases stay closed and are not refreshed by imports. A retry using an already-consumed preview is rejected as stale when the first import changed cases; no duplicate write occurs. Reopening requires a new explicit decision. Stale form versions are rejected. SQLite `BEGIN IMMEDIATE` serializes concurrent mutations; an audit failure rolls the whole mutation back.

The browser preview currently permits up to 200 candidate pairs. Above that limit it fails without importing anything and asks the operator to use the standalone report for investigation. No partial or unseen import occurs. The scan remains an all-account operation; large-scale batching is future work. Missing stable IDs block import until the identity foundation migration is complete.

## Screens and access

- Overview, duplicate queue/detail, account identity summary, policy status and audit history reuse the existing admin layout and authentication. Account administration links to the identity summary.
- Every route checks admin status; impersonation sessions are rejected. Every mutation uses POST and application CSRF validation. Responses are private and `no-store`.
- Evidence contains reasons, observation ranges and counts, never device hashes, raw fingerprint values, passwords or session tokens. User-entered notes are escaped; administrators should never paste secrets into notes.
- Queue and audit pages show 30 entries at a time. Case detail shows the latest 100 retained actions; the paginated audit screen provides older retained actions.
- Policy status displays current device-key configuration without showing secrets. Approved future email rules are labeled as not yet enforced by the new signup policy. No misleading inactive enforcement toggles are offered.

## Storage and migration

`migrations/add_identity_review.py` adds three tables: `identity_review_cases`, `identity_review_evidence`, and `identity_review_actions`. It is registered in precheck and the existing migration verification inventory. It does not backfill cases or change users. Case pairs use stable IDs and survive account deletion as unavailable-account references; evidence/action rows are linked to their case. Unique pair and per-case reason constraints prevent duplicates.

The standalone scanner remains read-only by default. The shared scanning function now also supports an internal, caller-owned SQLite transaction, so the admin import can revalidate the same source while holding its write lock. The scanner returns a per-reason expiry derived from actual matching-key pairs; combining sightings across rotation keys cannot extend evidence beyond a genuine shared match.

## Retention and operational setup

Evidence expires at the earlier of capture + 180 days and the underlying matched device evidence expiry. All reads immediately exclude expired evidence; refreshing a scan cannot extend old device sightings. Canonical-email snapshots have the 180-day capture limit.

Closed cases and their action history expire 180 days after closure. Existing unexpired action history receives that closing deadline when the case is closed. For open cases, individual notes/action entries expire after 180 days; the minimal case workflow record remains. Reopening does not restore purged/expired notes or evidence. Actions on a closed case do not extend its closure deadline. Exported standalone reports retain their separate 30-day limit.

Schedule this command daily using the deployment's existing environment and application configuration:

```sh
python -m flask --app 'app:create_app()' purge-identity-reviews
```

It removes expired evidence/history and cases closed for at least 180 days in one transaction and prints counts only. Import also performs retention cleanup. No scheduler is installed automatically, and normal page views do not perform deletion. The policy page cannot verify whether scheduling is installed. Backup retention is a separate rollout decision and is not changed here.

## Validation

Use shared QA accounts in isolated databases:

```sh
.venv/bin/python -m pytest -p tests.qa_accounts_plugin tests/test_identity_review.py tests/test_identity_scan.py --no-cov -q
```

Checks include authorization/CSRF, preview binding and expiry, source revalidation, repeated imports, lifecycle/assignment/reasons, escaped notes, simultaneous process edits, transactional audit rollback, pagination, expiration and cleanup, migration reruns and schema parity. Production deployment, migration rehearsal against a production copy and scheduler installation are separate operator steps.
