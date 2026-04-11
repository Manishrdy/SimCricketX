"""
db_create_pg_schema.py
----------------------
Creates all tables on Supabase using SQLAlchemy models (db.create_all()).
Run this ONCE before running db_schema_verify.py.

Uses DIRECT_URL from .env (port 5432, bypasses pooler — required for DDL).

Usage:
    python scripts/db_create_pg_schema.py
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

DIRECT_URL = os.environ.get("DIRECT_URL", "").strip()

if not DIRECT_URL:
    print("[ERROR] DIRECT_URL not set in .env")
    sys.exit(1)

if "[YOUR-PASSWORD]" in DIRECT_URL:
    print("[ERROR] DIRECT_URL still contains the placeholder [YOUR-PASSWORD].")
    print("        Replace it with your actual Supabase database password in .env")
    sys.exit(1)

# Re-encode the URL so special characters in the password (e.g. @, #, %)
# don't break URL parsing. We parse the raw URL, percent-encode the password
# component, then rebuild a clean URL.
from urllib.parse import urlparse, urlunparse, quote

def _safe_pg_url(raw: str) -> str:
    raw = raw.replace("postgres://", "postgresql://", 1)
    # Strip pgbouncer param for direct connection
    raw = raw.split("?")[0]
    parsed = urlparse(raw)
    if parsed.password:
        safe_password = quote(parsed.password, safe="")
        netloc = f"{parsed.username}:{safe_password}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
    return urlunparse(parsed)

pg_url = _safe_pg_url(DIRECT_URL)

print(f"[INFO] Connecting to Supabase via DIRECT_URL...")

# Bootstrap a minimal Flask app pointing at Supabase
from flask import Flask
from database import db
import database.models  # ensures all models are registered

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = pg_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

with app.app_context():
    try:
        db.create_all()
        print("[OK] All tables created (or already exist) on Supabase.")
    except Exception as e:
        print(f"[ERROR] db.create_all() failed: {e}")
        sys.exit(1)

    # db.create_all() does NOT add columns to existing tables.
    # Run ALTER TABLE IF NOT EXISTS for any columns that models.py now has
    # but the table already existed without them.
    from sqlalchemy import inspect, text

    ADD_MISSING = {
        "matches": [
            ("fc_days",         "INTEGER"),
            ("fc_innings_json", "TEXT"),
        ],
        "tournament_player_stats_cache": [
            ("category",   "VARCHAR(20)"),
            ("data_json",  "TEXT"),
            ("updated_at", "TIMESTAMP WITHOUT TIME ZONE"),
        ],
    }

    engine = db.engine
    insp   = inspect(engine)
    pg_tables = insp.get_table_names()

    for table, col_defs in ADD_MISSING.items():
        if table not in pg_tables:
            print(f"[WARN] Table '{table}' not found in Postgres — skipping ALTER")
            continue
        existing_cols = {c["name"] for c in insp.get_columns(table)}
        for col_name, col_type in col_defs:
            if col_name in existing_cols:
                print(f"  [skip] {table}.{col_name} already exists")
            else:
                sql = f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{col_name}" {col_type}'
                with engine.begin() as conn:
                    conn.execute(text(sql))
                print(f"  [added] {table}.{col_name} {col_type}")

    print("\n[OK] Schema is up to date. Run db_schema_verify.py to confirm.")
