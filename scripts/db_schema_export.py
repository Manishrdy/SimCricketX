"""
db_schema_export.py
-------------------
Step 1 of the SQLite → Supabase migration pipeline.

Reads the live SQLite database schema (via SQLAlchemy introspection) and
generates a PostgreSQL-compatible CREATE TABLE script at:

    scripts/output/schema_postgres.sql

This file is used by db_schema_verify.py to compare against what actually
exists on Supabase after db.create_all() runs there.

Usage:
    python scripts/db_schema_export.py

No environment variables required — reads cricket_sim.db directly.
"""

import os
import sys

# Allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import create_engine, inspect, text

# ── Output directory ──────────────────────────────────────────────────────────
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "schema_postgres.sql")

# ── SQLite source ─────────────────────────────────────────────────────────────
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SQLITE_PATH = os.path.join(BASE_DIR, "cricket_sim.db")

if not os.path.exists(SQLITE_PATH):
    print(f"[ERROR] SQLite database not found at: {SQLITE_PATH}")
    sys.exit(1)

# ── SQLite → PostgreSQL type mapping ─────────────────────────────────────────
# SQLAlchemy reports generic type names from SQLite; we map them to idiomatic
# PostgreSQL equivalents.
TYPE_MAP = {
    # Integers
    "INTEGER":          "INTEGER",
    "BIGINT":           "BIGINT",
    "SMALLINT":         "SMALLINT",
    # Strings / Text
    "VARCHAR":          "VARCHAR",
    "CHAR":             "CHAR",
    "TEXT":             "TEXT",
    "CLOB":             "TEXT",
    # Boolean (SQLite stores 0/1; Postgres has real BOOLEAN)
    "BOOLEAN":          "BOOLEAN",
    # Floats / Numerics
    "FLOAT":            "DOUBLE PRECISION",
    "REAL":             "REAL",
    "NUMERIC":          "NUMERIC",
    "DECIMAL":          "DECIMAL",
    # Date / Time
    "DATETIME":         "TIMESTAMP WITHOUT TIME ZONE",
    "DATE":             "DATE",
    "TIME":             "TIME",
    # Binary
    "BLOB":             "BYTEA",
    # JSON (SQLAlchemy JSON type maps to TEXT in SQLite, JSONB in Postgres)
    "JSON":             "JSONB",
}

# Tables that must be created before others (FK dependency order).
# If a table is not listed here it will be created after all listed tables.
FK_ORDER = [
    "users",
    "teams",
    "team_profiles",
    "players",
    "tournaments",
    "matches",
    "match_scorecards",
    "match_partnerships",
    "tournament_teams",
    "tournament_fixtures",
    "tournament_player_stats_cache",
    "failed_login_attempts",
    "blocked_ips",
    "active_sessions",
    "site_counters",
    "announcement_banner",
    "user_banner_dismissals",
    "login_history",
    "ip_whitelist",
    "user_ground_configs",
    "auth_event_log",
    "exception_log",
    "issue_report",
    "issue_webhook_event",
    "admin_audit_log",
]


def _pg_type(col) -> str:
    """Convert a SQLAlchemy column type to a PostgreSQL type string."""
    type_str = str(col["type"]).upper()

    # Handle parameterised types like VARCHAR(120), NUMERIC(10,2)
    base = type_str.split("(")[0].strip()
    params = ""
    if "(" in type_str:
        params = "(" + type_str.split("(", 1)[1]  # includes closing paren

    pg_base = TYPE_MAP.get(base, base)  # fall back to original if not in map

    # SERIAL for auto-increment integer PKs is handled separately at column
    # definition time — keep plain INTEGER here.
    return pg_base + params


def _col_definition(col, pk_cols: list[str]) -> str:
    """Build a single column definition line for PostgreSQL."""
    name = col["name"]
    pg_type = _pg_type(col)
    parts = [f'    "{name}" {pg_type}']

    # Auto-increment primary key → SERIAL (single-column PK only)
    if len(pk_cols) == 1 and name == pk_cols[0] and "INT" in pg_type.upper():
        parts = [f'    "{name}" SERIAL']

    # Nullability
    if not col["nullable"] and name not in pk_cols:
        parts.append("NOT NULL")

    # Default values — translate SQLite literals to Postgres equivalents
    default = col.get("default")
    if default is not None:
        default_str = str(default).strip().strip("'\"")
        # SQLite datetime defaults — skip; app sets these in Python
        sqlite_datetime_defaults = {
            "CURRENT_TIMESTAMP", "datetime('now')", "datetime(\"now\")",
        }
        if default_str.upper() not in sqlite_datetime_defaults:
            # Booleans
            if default_str in ("1", "true", "True"):
                parts.append("DEFAULT TRUE")
            elif default_str in ("0", "false", "False"):
                parts.append("DEFAULT FALSE")
            else:
                parts.append(f"DEFAULT {default_str}")

    return " ".join(parts)


def export_schema():
    engine = create_engine(f"sqlite:///{SQLITE_PATH}")
    insp = inspect(engine)
    all_tables = set(insp.get_table_names())

    # Build ordered table list: FK_ORDER first, then any remaining tables
    ordered = [t for t in FK_ORDER if t in all_tables]
    ordered += sorted(all_tables - set(ordered))

    lines = [
        "-- ============================================================",
        "-- PostgreSQL schema generated from SQLite (cricket_sim.db)",
        "-- Generated by: scripts/db_schema_export.py",
        "-- DO NOT EDIT MANUALLY — regenerate from source SQLite",
        "-- ============================================================",
        "",
    ]

    for table in ordered:
        pk_cols = list(insp.get_pk_constraint(table).get("constrained_columns", []))
        columns = insp.get_columns(table)
        col_defs = [_col_definition(c, pk_cols) for c in columns]

        # Primary key constraint
        if pk_cols:
            if len(pk_cols) == 1 and any(
                f'"{pk_cols[0]}" SERIAL' in d for d in col_defs
            ):
                # SERIAL column — no explicit PK line needed? Actually still add it.
                col_defs.append(f'    PRIMARY KEY ("{pk_cols[0]}")')
            else:
                pk_list = ", ".join(f'"{c}"' for c in pk_cols)
                col_defs.append(f"    PRIMARY KEY ({pk_list})")

        # Unique constraints
        for uq in insp.get_unique_constraints(table):
            cols = ", ".join(f'"{c}"' for c in uq["column_names"])
            name = uq.get("name") or f"uq_{table}_{'_'.join(uq['column_names'])}"
            col_defs.append(f'    CONSTRAINT "{name}" UNIQUE ({cols})')

        # Foreign keys
        for fk in insp.get_foreign_keys(table):
            local_cols  = ", ".join(f'"{c}"' for c in fk["constrained_columns"])
            ref_table   = fk["referred_table"]
            ref_cols    = ", ".join(f'"{c}"' for c in fk["referred_columns"])
            fk_name     = fk.get("name") or f"fk_{table}_{'_'.join(fk['constrained_columns'])}"
            on_delete   = fk.get("options", {}).get("ondelete", "")
            on_del_str  = f" ON DELETE {on_delete}" if on_delete else ""
            col_defs.append(
                f'    CONSTRAINT "{fk_name}" FOREIGN KEY ({local_cols}) '
                f'REFERENCES "{ref_table}" ({ref_cols}){on_del_str}'
            )

        body = ",\n".join(col_defs)
        lines += [
            f'CREATE TABLE IF NOT EXISTS "{table}" (',
            body,
            ");",
            "",
        ]

        # Indexes (non-PK, non-unique)
        for idx in insp.get_indexes(table):
            if idx.get("unique"):
                continue  # already covered by UNIQUE constraint above
            idx_name = idx["name"] or f"ix_{table}_{'_'.join(idx['column_names'])}"
            idx_cols = ", ".join(f'"{c}"' for c in idx["column_names"])
            lines.append(
                f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{table}" ({idx_cols});'
            )
        lines.append("")

    schema_sql = "\n".join(lines)

    with open(OUTPUT_FILE, "w") as f:
        f.write(schema_sql)

    print(f"[OK] Schema exported → {OUTPUT_FILE}")
    print(f"     Tables exported : {len(ordered)}")
    print(f"     Tables in DB    : {len(all_tables)}")

    # Print summary
    print("\n  Table order:")
    for i, t in enumerate(ordered, 1):
        cols = insp.get_columns(t)
        print(f"    {i:2}. {t} ({len(cols)} columns)")


if __name__ == "__main__":
    export_schema()
