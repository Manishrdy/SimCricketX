"""Immutable match length; legacy List A always means scheduled 50 overs."""
from sqlalchemy import inspect, text


def run_migration(db, app):
    with app.app_context(), db.engine.begin() as conn:
        for table, format_column in (("matches", "match_format"), ("tournaments", "format_type")):
            if not inspect(conn).has_table(table):
                continue
            if "scheduled_overs" not in {c["name"] for c in inspect(conn).get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN scheduled_overs INTEGER"))
            conn.execute(text(f"UPDATE {table} SET scheduled_overs = CASE {format_column} "
                              "WHEN 'ListA' THEN 50 WHEN 'T20' THEN 20 END "
                              f"WHERE scheduled_overs IS NULL AND {format_column} IN ('ListA', 'T20')"))
