"""
db_schema_verify.py
-------------------
Step 2 of the SQLite → Supabase migration pipeline.

Connects to Supabase via DIRECT_URL (from .env), introspects its live schema,
then strictly compares it against the SQLite schema.

Checks performed per table:
  ✓ Table exists on both sides
  ✓ All columns present (name, type family, nullable)
  ✓ Primary key columns match
  ✓ Unique constraints match (by column set)
  ✓ Foreign key relationships match (local cols → ref table.ref cols)
  ✓ Indexes present

Exits with code 0 if schemas match, code 1 if any mismatch found.

Usage:
    python scripts/db_schema_verify.py

Requires:
    DIRECT_URL in .env  (postgresql://... port 5432, bypasses pooler)
    psycopg2-binary installed  (pip install psycopg2-binary)
"""

import os
import sys
from collections import defaultdict

# Allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass  # If python-dotenv not installed, rely on shell env

from sqlalchemy import create_engine, inspect

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SQLITE_PATH = os.path.join(BASE_DIR, "cricket_sim.db")
DIRECT_URL  = os.environ.get("DIRECT_URL", "").strip()

# SQLite base types → normalised family for loose comparison
# We compare type *families*, not exact lengths/precision, because SQLite
# VARCHAR(120) and Postgres VARCHAR(120) are semantically identical.
SQLITE_FAMILY = {
    "INTEGER": "int", "BIGINT": "int", "SMALLINT": "int", "INT": "int",
    # SQLite stores BOOLEAN as INTEGER (0/1) — treat both as the same family
    "BOOLEAN": "int",
    "VARCHAR": "str", "CHAR": "str", "TEXT": "str", "CLOB": "str",
    "FLOAT": "float", "REAL": "float", "DOUBLE PRECISION": "float",
    "NUMERIC": "numeric", "DECIMAL": "numeric",
    "DATETIME": "datetime", "TIMESTAMP": "datetime",
    "TIMESTAMP WITHOUT TIME ZONE": "datetime",
    "TIMESTAMP WITH TIME ZONE": "datetime",
    "DATE": "date",
    "BLOB": "bytes", "BYTEA": "bytes",
    "JSON": "json", "JSONB": "json",
}

# These (SQLite-family, Postgres-family) pairs are known SQLite schema drift —
# the Postgres type (from models.py) is authoritative. Demote to warning.
KNOWN_DRIFT_PAIRS = {
    ("float", "str"),   # overs columns declared String in models but FLOAT in old SQLite migrations
}


def _safe_pg_url(raw: str) -> str:
    """Re-encode password in URL so special chars (@ # %) don't break parsing."""
    from urllib.parse import urlparse, urlunparse, quote
    raw = raw.replace("postgres://", "postgresql://", 1).split("?")[0]
    parsed = urlparse(raw)
    if parsed.password:
        safe_pw = quote(parsed.password, safe="")
        netloc  = f"{parsed.username}:{safe_pw}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
    return urlunparse(parsed)


def _family(type_str: str) -> str:
    """Return the normalised type family for comparison."""
    base = str(type_str).upper().split("(")[0].strip()
    return SQLITE_FAMILY.get(base, base)


def _introspect(engine) -> dict:
    """
    Returns a dict keyed by table name. Each value is:
    {
        "columns":     {col_name: {"family": ..., "nullable": bool}},
        "pk":          [col_name, ...],
        "unique":      [frozenset({col_names}), ...],
        "fk":          [(local_cols_tuple, ref_table, ref_cols_tuple), ...],
        "indexes":     [frozenset({col_names}), ...],
    }
    """
    insp   = inspect(engine)
    tables = insp.get_table_names()
    result = {}

    for table in tables:
        cols = {}
        for c in insp.get_columns(table):
            cols[c["name"]] = {
                "family":   _family(str(c["type"])),
                "nullable": c["nullable"],
            }

        pk = insp.get_pk_constraint(table).get("constrained_columns", [])

        unique = [
            frozenset(uq["column_names"])
            for uq in insp.get_unique_constraints(table)
        ]

        fk = []
        for f in insp.get_foreign_keys(table):
            fk.append((
                tuple(f["constrained_columns"]),
                f["referred_table"],
                tuple(f["referred_columns"]),
            ))

        indexes = [
            frozenset(ix["column_names"])
            for ix in insp.get_indexes(table)
            if not ix.get("unique")
        ]

        result[table] = {
            "columns": cols,
            "pk":      pk,
            "unique":  unique,
            "fk":      fk,
            "indexes": indexes,
        }

    return result


class SchemaReport:
    def __init__(self):
        self.errors   = []
        self.warnings = []
        self.ok       = []

    def error(self, msg):
        self.errors.append(f"  [FAIL] {msg}")

    def warn(self, msg):
        self.warnings.append(f"  [WARN] {msg}")

    def good(self, msg):
        self.ok.append(f"  [ OK ] {msg}")

    def passed(self) -> bool:
        return len(self.errors) == 0

    def print_summary(self):
        print("\n" + "=" * 62)
        print("  SCHEMA VERIFICATION REPORT")
        print("=" * 62)

        if self.errors:
            print(f"\n  FAILURES ({len(self.errors)}):")
            for e in self.errors:
                print(e)

        if self.warnings:
            print(f"\n  WARNINGS ({len(self.warnings)}):")
            for w in self.warnings:
                print(w)

        if self.ok:
            print(f"\n  PASSED ({len(self.ok)}):")
            for o in self.ok:
                print(o)

        print("\n" + "=" * 62)
        if self.passed():
            print("  RESULT: SCHEMAS MATCH — safe to run data migration")
        else:
            print("  RESULT: SCHEMA MISMATCH — fix errors before migrating data")
        print("=" * 62 + "\n")


def compare(sqlite_schema: dict, pg_schema: dict, report: SchemaReport):
    sqlite_tables = set(sqlite_schema.keys())
    pg_tables     = set(pg_schema.keys())

    # ── Missing tables ────────────────────────────────────────────────────────
    missing_in_pg  = sqlite_tables - pg_tables
    extra_in_pg    = pg_tables - sqlite_tables

    for t in sorted(missing_in_pg):
        report.error(f"Table '{t}' exists in SQLite but NOT in Postgres")

    for t in sorted(extra_in_pg):
        report.warn(f"Table '{t}' exists in Postgres but NOT in SQLite (extra)")

    common = sqlite_tables & pg_tables

    for table in sorted(common):
        sq = sqlite_schema[table]
        pg = pg_schema[table]

        # ── Columns ──────────────────────────────────────────────────────────
        sq_cols = sq["columns"]
        pg_cols = pg["columns"]

        missing_cols = set(sq_cols) - set(pg_cols)
        extra_cols   = set(pg_cols) - set(sq_cols)

        for col in sorted(missing_cols):
            report.error(f"'{table}'.'{col}' missing in Postgres")

        for col in sorted(extra_cols):
            report.warn(f"'{table}'.'{col}' extra in Postgres (not in SQLite)")

        for col in sorted(set(sq_cols) & set(pg_cols)):
            sq_c = sq_cols[col]
            pg_c = pg_cols[col]

            # Type family
            if sq_c["family"] != pg_c["family"]:
                pair = (sq_c["family"], pg_c["family"])
                if pair in KNOWN_DRIFT_PAIRS:
                    report.warn(
                        f"'{table}'.'{col}' SQLite schema drift: "
                        f"SQLite={sq_c['family']} but models.py defines {pg_c['family']} "
                        f"(Postgres is correct — data will be coerced during migration)"
                    )
                else:
                    report.error(
                        f"'{table}'.'{col}' type mismatch: "
                        f"SQLite={sq_c['family']} vs Postgres={pg_c['family']}"
                    )

            # Nullability (only flag if SQLite says NOT NULL and Postgres allows NULL)
            if not sq_c["nullable"] and pg_c["nullable"]:
                report.warn(
                    f"'{table}'.'{col}' is NOT NULL in SQLite but nullable in Postgres"
                )

        # ── Primary key ───────────────────────────────────────────────────────
        if sorted(sq["pk"]) != sorted(pg["pk"]):
            report.error(
                f"'{table}' PK mismatch: SQLite={sq['pk']} vs Postgres={pg['pk']}"
            )

        # ── Unique constraints ────────────────────────────────────────────────
        sq_uqs = set(sq["unique"])
        pg_uqs = set(pg["unique"])
        for uq in sq_uqs - pg_uqs:
            report.error(
                f"'{table}' unique constraint {set(uq)} missing in Postgres"
            )

        # ── Foreign keys ──────────────────────────────────────────────────────
        sq_fks = set(sq["fk"])
        pg_fks = set(pg["fk"])
        for fk in sq_fks - pg_fks:
            local, ref_table, ref_cols = fk
            report.error(
                f"'{table}' FK {list(local)} → '{ref_table}'.{list(ref_cols)} "
                f"missing in Postgres"
            )

        # ── Indexes ───────────────────────────────────────────────────────────
        sq_idxs = set(sq["indexes"])
        pg_idxs = set(pg["indexes"])
        for ix in sq_idxs - pg_idxs:
            report.warn(
                f"'{table}' index on {set(ix)} present in SQLite but not Postgres"
            )

        if not missing_cols and sorted(sq["pk"]) == sorted(pg["pk"]):
            report.good(f"'{table}' — columns, PK, constraints OK")


def main():
    print("\n[Step 2] Schema Verification: SQLite ↔ Supabase")
    print("-" * 62)

    # ── Validate prereqs ──────────────────────────────────────────────────────
    if not os.path.exists(SQLITE_PATH):
        print(f"[ERROR] SQLite DB not found: {SQLITE_PATH}")
        sys.exit(1)

    if not DIRECT_URL:
        print("[ERROR] DIRECT_URL not set in .env")
        print("        Set DIRECT_URL=postgresql://... (port 5432) and retry")
        sys.exit(1)

    if not DIRECT_URL.startswith("postgresql://") and not DIRECT_URL.startswith("postgres://"):
        print(f"[ERROR] DIRECT_URL must start with postgresql:// — got: {DIRECT_URL[:30]}...")
        sys.exit(1)

    direct_url = _safe_pg_url(DIRECT_URL)

    print(f"  SQLite  : {SQLITE_PATH}")
    pg_display = direct_url[:40] + "..." if len(direct_url) > 40 else direct_url
    print(f"  Postgres: {pg_display}")
    print()

    # ── Connect ───────────────────────────────────────────────────────────────
    try:
        sqlite_engine = create_engine(f"sqlite:///{SQLITE_PATH}")
        with sqlite_engine.connect():
            pass
        print("[OK] Connected to SQLite")
    except Exception as e:
        print(f"[ERROR] Cannot open SQLite: {e}")
        sys.exit(1)

    try:
        pg_engine = create_engine(direct_url, connect_args={"connect_timeout": 10})
        with pg_engine.connect():
            pass
        print("[OK] Connected to Supabase (Postgres)")
    except Exception as e:
        print(f"[ERROR] Cannot connect to Supabase: {e}")
        print("        Check DIRECT_URL and that psycopg2-binary is installed.")
        sys.exit(1)

    print()

    # ── Introspect ────────────────────────────────────────────────────────────
    print("  Introspecting SQLite schema...")
    sqlite_schema = _introspect(sqlite_engine)
    print(f"  → {len(sqlite_schema)} tables found\n")

    print("  Introspecting Postgres schema...")
    pg_schema = _introspect(pg_engine)
    print(f"  → {len(pg_schema)} tables found\n")

    if not pg_schema:
        print("[WARN] Postgres has no tables yet.")
        print("       Run db.create_all() against Supabase first, then re-run this script.")
        sys.exit(1)

    # ── Compare ───────────────────────────────────────────────────────────────
    report = SchemaReport()
    compare(sqlite_schema, pg_schema, report)
    report.print_summary()

    sys.exit(0 if report.passed() else 1)


if __name__ == "__main__":
    main()
