# On-demand account identity review

On `/admin/identity/account-review`, **Run identity review** opens an activity panel with the current stage, timestamped output, processed and expected account counts, finding count and elapsed time. A successful run refreshes the saved results in place. The CSRF-protected POST streams newline-delimited JSON when requested by the page. Standard form submissions run the same review and redirect to the report. GET only reads the saved report. The default view includes findings and coverage gaps, with actionable findings first. **All accounts** includes clean accounts too. Search resolves current email/stable ID, and finding/gap filters and 30-account pagination preserve the query.

The scanner uses one read-only SQLite transaction. Email normalization and current new-address policy remain separate checks; neither proves mailbox ownership. Duplicate reasons use the existing evidence grouping, ranking and 180-day device expiry rules. Every member of a matching group is flagged without materializing pairs. The original duplicate preview and confirmed case import remain separate workflows.

Device coverage means retained evidence usable by the duplicate scanner (confirmed cookie or valid fingerprint, a key identifier, valid timestamps and one existing owner). Network state is the latest retained historical classification, not a live access decision. Verification flags, active bans, pending campaign corrections and open duplicate cases describe the scan snapshot. Blocked-signup groups appear only in the report header and can represent applicants without an account.

Only the latest successful report is stored. Its header records UTC start/completion, initiating administrator's stable ID, rules/normalization/ranking versions, totals, evidence coverage and safe configuration status. Rows store stable account IDs, finding codes/reasons and explanatory timestamps/state, with no email copies, device hashes, IPs or credentials. Current labels are joined at display time. Account deletion cascades to report rows; a merge does not transfer a source's report findings to the survivor.

Device/network details and matching findings disappear from display, filters and ordering when their recorded source evidence expires. Counts in the header remain explicitly historical scan totals. Reports become unreadable exactly 30 days after successful completion. `flask purge-identity-reviews` physically deletes expired report headers and account rows alongside existing review cleanup.

## Execution and failure behavior

The request uses gevent's native thread pool for SQLite and grouping work. A dedicated nonblocking POSIX file lock at `<canonical database path>.account-review.lock` permits one scan per database across worker processes. Keep the database directory writable and use the same canonical database path on all workers. Do not delete a lock file while workers are running.

The scan has a 60-second processing deadline, periodic Python deadline checks and a SQLite progress handler; SQLite waits are limited to one second. Publication rechecks the deadline inside a short write transaction and atomically replaces only report data. Busy, timed-out, invalid-source or failed scans preserve the prior successful report and timestamp. Accounts deleted during scanning are excluded at publication. Missing or non-distinct stable IDs fail the entire scan rather than silently omitting accounts. No automatic ID repair occurs.

The live response requests disabled proxy buffering through `X-Accel-Buffering: no` and sends a heartbeat every five seconds during quiet stages. A disconnected browser can refresh the report to see whether the already-started scan saved successfully. The committed Gunicorn configuration uses a 600-second timeout. Verify streaming and request timeouts through the deployed proxy before rollout.

## Validation and rollout

Use shared QA logins with isolated SQLite databases:

```sh
.venv/bin/python -m pytest -p tests.qa_accounts_plugin tests/test_identity_account_review.py tests/test_identity_scan.py tests/test_identity_review.py tests/test_identity_hub.py --no-cov -q
```

The additive `add_identity_account_review` migration is registered in startup precheck. Rehearse it on a SQLite backup, not the source database. Check prerequisite stable IDs, successful scan runtime, concurrent normal requests, report-only writes and foreign-key checks before deploying. Restart workers consistently, keep existing evidence/report purge scheduled, then have an operator run the first live report.

November 12 local rehearsal: the supplied `cricket_sim.db` has **85 accounts**, zero device sightings, zero network observations and **one missing stable ID**. The unmodified copy correctly refuses a partial scan. On a disposable copy only, assigning the missing ID allowed an 85-account scan in **0.0782 seconds**, with 26 accounts having findings. Repeating the report migration took 0.0025 seconds. Report generation preserved all non-report data; the original file's logical contents were unchanged. Its 41 pre-existing foreign-key violations were unchanged. This is not a verification of the earlier 1,073-account production preview or production hardware/proxy performance. A current production-sized rehearsal and stable-ID prerequisite resolution remain operator rollout requirements.
