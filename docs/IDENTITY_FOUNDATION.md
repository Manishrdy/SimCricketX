# Identity foundation (Feature 2)

This feature adds reusable email rules and additive schema. Live registration,
login, recovery and email-change routes are not switched to the new rules yet.
Signup enforcement is Feature 6; safe email changes are Feature 8. No enforcement
switch, remediation deadline, account merge or deletion is activated here.

## Email rules

`utils/identity_rules.py` separates structural normalization from new-address
policy. `normalize_email()` trims outer whitespace, lowercases ASCII local parts
and converts international domains using IDNA/UTS46. Dot-atom local parts are
supported (up to 64 characters); quoted local parts, address literals and Unicode
local parts are rejected. The normalized address is limited to 254 characters.

The stored address retains dots and plus tags. Canonical evidence combines dots,
plus suffixes and googlemail.com only for consumer Gmail domains, as approved.
Custom Workspace and other providers are not given unverified alias rules.
`stored_address_key()` never folds provider aliases: it is a future normalized
lookup helper, not a replacement for legacy stored-key lookup.

`validate_new_email()` additionally rejects every plus-tag address and known
disposable domains (including subdomains). Ordinary and unfamiliar domains are
allowed. Validation does not prove ownership or deliverability: future signup
integration must still require mailbox verification. Existing policy-invalid
addresses are not retroactively locked out by these helpers or the migration.

## Data and transactions

`users.canonical_email` has a **non-unique** index. Normalization version and
structural error code are nullable metadata; newly created legacy-path accounts
may have no metadata until the next migration/reconciliation. Existing primary
keys, stable UUIDs, password hashes and foreign-key references are preserved.

The registered `add_identity_foundation` migration holds a SQLite write lock
through schema creation and backfill. Invalid addresses remain in place, with
an error code. Multiple accounts with the same canonical identity remain intact;
startup output reports counts only, without addresses. No survivor is selected.
The migration participates automatically in `migrations.verify` via the registry.

`identity_email_reservations` has one primary-key row per occupied canonical
identity, including legacy collision groups. Rows are deliberately ownerless:
there is no approved release/merge/return lifecycle yet. Reruns add missing
reservations, never delete old reservations, and do not change settled contents.

`reserve_new_email(conn, address)` is a foundation for future integration. Call it
as the first SQL operation inside `with engine.begin() as conn`, then insert the
account on that **same connection**, recording the returned canonical and version
metadata. Allow exceptions to escape the transaction context so reservation and
account writes roll back together. The unique reservation insert obtains the
SQLite write lock; conflict yields `IdentityConflict` without account details.
Busy/IO failures must be retried as a whole transaction or reported as failure.

Until every legacy email writer is integrated, the helper checks stored account
keys as well as canonical metadata under the lock. This temporary O(number of
users) fallback detects missing/stale metadata but cannot protect against a
legacy writer inserting a collision later. Feature 6 must reconcile existing
data and integrate all relevant writers before claiming enforced uniqueness.
Final uniqueness on users must wait for verified collision resolution.

Other identity feature tables (devices, reviews, exemptions, campaigns, merge
receipts) will be added with their features once their lifecycle choices are
settled. They are not speculative placeholders in this schema.

## Disposable-domain snapshot maintenance

The bundled dataset in `utils/data/disposable_email/` comes from
[disposable-email-domains](https://github.com/disposable-email-domains/disposable-email-domains).
`source.json` records the immutable upstream commit and retrieval date; the
original list and CC0 license are retained. There are 9,917 upstream entries,
9,916 usable under our IDNA rules. The upstream emoji domain cannot be accepted
by our address parser and is skipped when loading the list.

There are no request-time HTTP/DNS calls and no scheduled updater. To refresh:

1. Select and review an upstream commit, including additions/removals and license.
2. Replace `disposable_email_blocklist.conf` and `LICENSE.txt` from that commit;
   update `source.json` with its exact SHA and retrieval date.
3. Run the identity tests, inspect the dataset diff and commit the reviewed update.
4. Deploy/restart normally to clear the process-local list cache.

A missing or empty snapshot raises an error; it never silently disables checks.
The list can be incomplete or contain false positives. Unknown domains remain
allowed by the approved policy. Admin override/appeal UX belongs to later work.
