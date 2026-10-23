# Read-only duplicate scan

Run as an authorized operator on a consistent SQLite copy. This standalone command does not load Flask, run migrations, create review cases, restrict users or merge/delete accounts.

```sh
.venv/bin/python scripts/scan_duplicate_accounts.py /secure/path/consistent-copy.db
.venv/bin/python scripts/scan_duplicate_accounts.py /secure/path/consistent-copy.db --format json --output /secure/path/duplicates.json
.venv/bin/python scripts/scan_duplicate_accounts.py /secure/path/consistent-copy.db --format csv --output /secure/path/duplicates.csv
```

The output path must be new. Files are created with owner-only permissions (0600); use an operator-only directory too. Console output contains account identifiers: do not send it to shared logs. Reports are admin-only and must be deleted within 30 days of generation. The report contains a delete-by timestamp; deletion is the operator's responsibility, with no scheduler or automatic archive provided. CSV prints run warnings to stderr, including when no pairs are present. JSON is the fullest machine-readable report, including schema availability and skipped-row diagnostics.

## Input and SQLite snapshots

Prepare the copy with SQLite's online backup API or SQLite `.backup` under the established operational backup process. Do not copy only a live main database file: committed data may still be in its WAL. Do not use `immutable=1` on a changing database or discard WAL files. Keep the copy and any sidecars together in a private directory. This feature does not perform production access or create a production copy for you.

The scanner opens the existing database with `mode=ro`, enables `query_only`, and holds a single read transaction for all queries. Missing paths fail rather than creating databases. No account, schema, migration or review writes occur. SQLite may use shared-memory locking metadata for WAL reads; byte-for-byte preservation of the SHM lock file is not promised. Tests verify unchanged database and committed WAL bytes, unchanged logical contents and visibility of committed versus uncommitted data. A consistent copy avoids holding read locks against the live service.

## Evidence and ranking version 1

Lower priority numbers appear first. They are categorical sorting labels, not a probability of common ownership or an enforcement threshold:

1. Canonical email match: recomputed from stored addresses using the approved identity normalization, ignoring potentially stale cached canonical columns.
2. Confirmed device match: matching nonempty keyed device hashes under the same key ID, with confirmed cookies on both accounts.
3. Fingerprint match: matching nonempty keyed fingerprint hashes under the same key ID, with `ok` fingerprint status on both accounts.

A pair takes its strongest category and lists all matching categories. Rotation keys and repeated sightings do not multiply a pair or reason. Reports omit hash values, keys, passwords and session tokens. Device evidence includes each account's first/last observation range across matched, eligible sightings; ranges do not imply simultaneous activity. Canonical email evidence has no invented observation timestamp; the scan timestamp establishes when it was evaluated.

Device sightings at or before the rolling 180-day cutoff are excluded. Future, invalid or reversed timestamps, orphan sightings and ambiguous stable-ID associations are skipped and counted. Naive database timestamps are interpreted as UTC, consistent with existing models. IP/subnet/name matching and numeric risk scoring are deferred. No transitive pairs or ownership claims are inferred: A–B and B–C do not establish A–C.

Older schemas work with stored email IDs alone; missing/incomplete device schemas are disclosed, with no invented device history. Available direct ownership counts cover teams, matches, tours, tournaments, leagues and user player overrides. Missing tables are omitted rather than represented as zero. These are not comprehensive merge/deletion impact counts; indirect player/fixture references and private Community contents are not exported.

Reviewers must establish common ownership separately before any later merge workflow. Importing evidence into persistent admin cases is a later, explicit write workflow.

## Validation

```sh
.venv/bin/python -m pytest tests/test_identity_scan.py --no-cov -q
```

Tests use disposable synthetic SQLite fixtures, never live accounts or production data. No logged-in test sessions or new QA accounts are needed.
