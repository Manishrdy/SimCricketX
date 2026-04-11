"""
db_reset_sequences.py
---------------------
Resets all PostgreSQL SERIAL sequences to MAX(id) + 1 after a bulk data import.

When rows are inserted with explicit integer IDs (as in a migration), Postgres
sequences don't advance automatically. This causes UniqueViolation on the next
INSERT. Run this once after every bulk import.

Usage:
    python scripts/db_reset_sequences.py
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

from urllib.parse import urlparse, urlunparse, quote
from sqlalchemy import create_engine, inspect, text

DIRECT_URL = os.environ.get("DIRECT_URL", "").strip().strip('"')

if not DIRECT_URL:
    print("[ERROR] DIRECT_URL not set in .env")
    sys.exit(1)


def _safe_url(raw: str) -> str:
    raw = raw.replace("postgres://", "postgresql://", 1).split("?")[0]
    p = urlparse(raw)
    if p.password:
        netloc = f"{p.username}:{quote(p.password, safe='')}@{p.hostname}:{p.port}"
        raw = urlunparse(p._replace(netloc=netloc))
    return raw


def reset_sequences():
    engine = create_engine(_safe_url(DIRECT_URL), connect_args={"connect_timeout": 15})
    insp = inspect(engine)

    print("\n[db_reset_sequences] Resetting SERIAL sequences...\n")

    results = []
    with engine.begin() as conn:
        for table in insp.get_table_names():
            pk = insp.get_pk_constraint(table).get("constrained_columns", [])
            if len(pk) != 1:
                continue

            pk_col = pk[0]

            # Only reset if the column has a sequence (SERIAL / GENERATED)
            seq_name_row = conn.execute(text(
                "SELECT pg_get_serial_sequence(:t, :c)"
            ), {"t": table, "c": pk_col}).fetchone()

            if not seq_name_row or not seq_name_row[0]:
                continue  # String PK or no sequence (e.g. users, matches, site_counters)

            seq_name = seq_name_row[0]

            # Get current max id (0 if table is empty)
            max_row = conn.execute(
                text(f'SELECT COALESCE(MAX("{pk_col}"), 0) FROM "{table}"')
            ).fetchone()
            max_id = max_row[0] if max_row else 0

            # setval(seq, max_id, true) sets last_value=max_id so next value = max_id + 1
            conn.execute(
                text("SELECT setval(:seq, :val, true)"),
                {"seq": seq_name, "val": max(max_id, 1)},
            )
            results.append((table, pk_col, max_id, max_id + 1))

    print(f"  {'Table':<40} {'Max ID':>8}  {'Next ID':>8}")
    print("  " + "-" * 60)
    for table, col, max_id, next_id in results:
        print(f"  {table:<40} {max_id:>8}  {next_id:>8}")

    print(f"\n  Reset {len(results)} sequence(s). All INSERTs will now use correct IDs.\n")


if __name__ == "__main__":
    reset_sequences()
