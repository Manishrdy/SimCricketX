"""Full-text search over community posts (SQLite FTS5).

The FTS index is an external-content table kept in sync by triggers, so the
ORM never writes to it. Visibility/soft-delete filtering happens on the join
back to community_posts. If the index is missing (e.g. a test DB built by
``db.create_all``) search falls back to LIKE.
"""

from __future__ import annotations

import re

from sqlalchemy import text

FTS_TABLE = "community_posts_fts"

FTS_DDL = [
    f"""CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
        title, body, content='community_posts', content_rowid='id',
        tokenize='porter unicode61')""",
    f"""CREATE TRIGGER IF NOT EXISTS community_posts_fts_ai AFTER INSERT ON community_posts BEGIN
        INSERT INTO {FTS_TABLE}(rowid, title, body) VALUES (new.id, new.title, new.body);
    END""",
    f"""CREATE TRIGGER IF NOT EXISTS community_posts_fts_ad AFTER DELETE ON community_posts BEGIN
        INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
    END""",
    f"""CREATE TRIGGER IF NOT EXISTS community_posts_fts_au AFTER UPDATE OF title, body ON community_posts BEGIN
        INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
        INSERT INTO {FTS_TABLE}(rowid, title, body) VALUES (new.id, new.title, new.body);
    END""",
]

_WORD_RE = re.compile(r"[A-Za-z0-9]{2,}")
# Too common to help "similar posts" ranking.
_STOP = frozenset("""
    the and for with not but are was were this that have has had from when what
    how why can cant cannot does doesnt dont into your you its it is on in of to
    my me i a an be at by or as if so do no
""".split())


def ensure_fts(conn) -> bool:
    """Create the FTS table + triggers if missing. Returns True if created."""
    exists = conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": FTS_TABLE}).fetchone()
    for stmt in FTS_DDL:
        conn.execute(text(stmt))
    if not exists:
        conn.execute(text(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES ('rebuild')"))
    return not exists


def fts_available(session) -> bool:
    return session.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": FTS_TABLE}).fetchone() is not None


def query_terms(q: str, limit: int = 8) -> list[str]:
    seen = []
    for w in _WORD_RE.findall((q or "").lower()):
        if w not in _STOP and w not in seen:
            seen.append(w)
    return seen[:limit]


def match_expression(terms: list[str]) -> str:
    """OR of prefix terms — quoted, so user input can't inject FTS syntax."""
    return " OR ".join(f'"{t}"*' for t in terms)


def _fts_ids(session, expression: str, limit: int) -> list[int]:
    rows = session.execute(text(
        f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH :m "
        f"ORDER BY bm25({FTS_TABLE}, 4.0, 1.0) LIMIT :lim"),
        {"m": expression, "lim": limit}).fetchall()
    return [r[0] for r in rows]


def search_post_ids(session, q: str, limit: int = 50, *, precise: bool = False) -> list[int]:
    """Ranked post ids matching ``q`` (best first). Caller applies visibility.

    Every term is a prefix match, so a half-typed last word ("wrong bow")
    already finds "bowler". ``precise`` ranks posts containing *all* terms
    first and tops up with posts matching *any* term — what search-as-you-type
    wants. Without it, any-term matching only (duplicate detection wants the
    wider net)."""
    terms = query_terms(q)
    if not terms:
        return []
    if fts_available(session):
        ids: list[int] = []
        if precise and len(terms) > 1:
            ids = _fts_ids(session, " AND ".join(f'"{t}"*' for t in terms), limit)
        if len(ids) < limit:
            seen = set(ids)
            ids += [i for i in _fts_ids(session, match_expression(terms), limit) if i not in seen]
        return ids[:limit]
    # Fallback: rank by number of matching terms in title/body.
    clauses = " + ".join(
        f"(CASE WHEN lower(title) LIKE :t{i} THEN 2 ELSE 0 END + CASE WHEN lower(body) LIKE :t{i} THEN 1 ELSE 0 END)"
        for i in range(len(terms)))
    params = {f"t{i}": f"%{t}%" for i, t in enumerate(terms)}
    params["lim"] = limit
    rows = session.execute(text(
        f"SELECT id, ({clauses}) AS score FROM community_posts WHERE ({clauses}) > 0 "
        f"ORDER BY score DESC, id DESC LIMIT :lim"), params).fetchall()
    return [r[0] for r in rows]
