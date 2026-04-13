"""
Player Pool Linkage Migration (PPM-01)
======================================

Retroactively links every existing Player row to a MasterPlayer or UserPlayer
via nullable FKs (`master_player_id`, `user_player_id`).

One-time, standalone script. Not wired into app startup — run it once per
environment, then never again.

The CLI runs both phases in a single invocation:

1. **Schema** — idempotent; adds `master_player_id` / `user_player_id` FK
   columns + indexes on the `players` table via `ALTER TABLE`.

2. **Data linking** — walks every Player and sets one of the two FKs, creating
   UserPlayer override/custom rows as needed. Dry-run mode writes a CSV report
   under `migration_reports/` and rolls back; commit mode persists.

Usage:
    python -m migrations.link_players_to_pool --dry-run
    python -m migrations.link_players_to_pool --commit

NOTE: the new Player columns are declared in `database/models.py`, so deploy
the schema change (run this script with either flag) BEFORE shipping code
that queries those columns.

See `memory/project_player_pool_migration.md` for the full design rationale.
"""

import csv
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception


# Six attributes that define "identical ratings" between Player and MasterPlayer.
RATING_COLS = (
    "batting_rating",
    "bowling_rating",
    "fielding_rating",
    "batting_hand",
    "bowling_type",
    "bowling_hand",
)


# ── Schema migration (idempotent, called at app startup) ──────────────────────

def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            _add_fk_columns(conn)
            trans.commit()
            print("[Migration] link_players_to_pool: schema ready.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "link_players_to_pool"})
            trans.rollback()
            print(f"[Migration] link_players_to_pool: FAILED — {exc}")
            raise
        finally:
            conn.close()


def _add_fk_columns(conn):
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(players)")).fetchall()}

    if "master_player_id" not in cols:
        conn.execute(text(
            "ALTER TABLE players ADD COLUMN master_player_id INTEGER "
            "REFERENCES master_players(id) ON DELETE SET NULL"
        ))
        print("[Migration] players.master_player_id column added.")

    if "user_player_id" not in cols:
        conn.execute(text(
            "ALTER TABLE players ADD COLUMN user_player_id INTEGER "
            "REFERENCES user_players(id) ON DELETE SET NULL"
        ))
        print("[Migration] players.user_player_id column added.")

    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_players_master_player_id "
        "ON players(master_player_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_players_user_player_id "
        "ON players(user_player_id)"
    ))


# ── Data linking (CLI only) ───────────────────────────────────────────────────

def link_players(db, app, dry_run=True):
    """
    Walk every Player row and link it to a MasterPlayer or UserPlayer.

    Returns the stats dict. In dry-run mode, nothing is persisted and a CSV
    report is written to `migration_reports/`.
    """
    from database.models import Player, MasterPlayer, UserPlayer, Team, User

    with app.app_context():
        session = db.session

        # Index MasterPlayer by normalized name.
        master_by_norm = {
            m.name.strip().casefold(): m
            for m in session.query(MasterPlayer).all()
        }

        # Pre-load existing UserPlayer rows so we can reuse overrides/customs.
        override_cache = {}   # (user_id, master_id) -> UserPlayer
        custom_cache = {}     # (user_id, norm_name) -> UserPlayer
        for up in session.query(UserPlayer).all():
            if up.master_player_id is not None:
                override_cache[(up.user_id, up.master_player_id)] = up
            else:
                custom_cache[(up.user_id, up.name.strip().casefold())] = up

        # Profile → format lookup for CSV enrichment.
        profile_format = dict(
            session.execute(text("SELECT id, format_type FROM team_profiles")).fetchall()
        )

        player_rows = (
            session.query(Player, Team, User)
            .join(Team, Player.team_id == Team.id)
            .outerjoin(User, Team.user_id == User.id)
            .order_by(Team.user_id, Team.id, Player.id)
            .all()
        )

        rows_out = []
        stats = {
            "processed": 0,
            "skip_already_linked": 0,
            "skip_placeholder": 0,
            "skip_orphan": 0,
            "skip_firstclass": 0,
            "link_to_master": 0,
            "create_override": 0,
            "reuse_override": 0,
            "create_custom": 0,
            "reuse_custom": 0,
        }

        for player, team, user in player_rows:
            stats["processed"] += 1

            if team is None:
                stats["skip_orphan"] += 1
                continue
            if team.is_placeholder:
                stats["skip_placeholder"] += 1
                continue
            if profile_format.get(player.profile_id) == "FirstClass":
                stats["skip_firstclass"] += 1
                continue
            if player.master_player_id is not None or player.user_player_id is not None:
                stats["skip_already_linked"] += 1
                rows_out.append(_row(
                    player, team, user, profile_format,
                    match_type="", action="skip_already_linked", master=None,
                ))
                continue

            norm = player.name.strip().casefold()
            master = master_by_norm.get(norm)
            action = None
            target_up = None
            match_type = ""

            if master is not None:
                identical = all(
                    getattr(player, c) == getattr(master, c) for c in RATING_COLS
                )
                if identical:
                    match_type = "master_exact"
                    action = "link_to_master"
                    stats["link_to_master"] += 1
                    if not dry_run:
                        player.master_player_id = master.id
                else:
                    match_type = "master_rating_divergence"
                    key = (team.user_id, master.id)
                    existing = override_cache.get(key)
                    if existing is not None:
                        action = "reuse_override"
                        target_up = existing
                        stats["reuse_override"] += 1
                    else:
                        action = "create_override"
                        stats["create_override"] += 1
                        if not dry_run:
                            target_up = _make_user_player(
                                session, team.user_id, master.id, player,
                            )
                            override_cache[key] = target_up
                    if not dry_run and target_up is not None:
                        player.user_player_id = target_up.id
            else:
                match_type = "no_master_match"
                key = (team.user_id, norm)
                existing = custom_cache.get(key)
                if existing is not None:
                    action = "reuse_custom"
                    target_up = existing
                    stats["reuse_custom"] += 1
                else:
                    action = "create_custom"
                    stats["create_custom"] += 1
                    if not dry_run:
                        target_up = _make_user_player(
                            session, team.user_id, None, player,
                        )
                        custom_cache[key] = target_up
                if not dry_run and target_up is not None:
                    player.user_player_id = target_up.id

            rows_out.append(_row(
                player, team, user, profile_format,
                match_type=match_type, action=action, master=master,
            ))

        if dry_run:
            session.rollback()
            path = _write_csv(rows_out)
            print(f"[Migration] Dry-run complete. CSV written to: {path}")
        else:
            try:
                session.commit()
                print("[Migration] Linking committed.")
            except Exception as exc:
                session.rollback()
                log_exception(exc, source="sqlite", context={"migration": "link_players_to_pool", "phase": "commit"})
                print(f"[Migration] Linking FAILED — rolled back. {exc}")
                raise

        print(f"[Migration] Stats: {stats}")
        return stats


def _make_user_player(session, user_id, master_id, player):
    from database.models import UserPlayer
    up = UserPlayer(
        user_id=user_id,
        master_player_id=master_id,
        name=player.name,
        role=player.role,
        batting_rating=player.batting_rating,
        bowling_rating=player.bowling_rating,
        fielding_rating=player.fielding_rating,
        batting_hand=player.batting_hand,
        bowling_type=player.bowling_type,
        bowling_hand=player.bowling_hand,
        is_captain=player.is_captain,
        is_wicketkeeper=player.is_wicketkeeper,
    )
    session.add(up)
    session.flush()
    return up


def _row(player, team, user, profile_format, *, match_type, action, master):
    def ratings_str(obj):
        return (
            f"{obj.batting_rating}/{obj.bowling_rating}/{obj.fielding_rating}"
            f"|{obj.batting_hand or ''}|{obj.bowling_type or ''}|{obj.bowling_hand or ''}"
        )

    return {
        "user_id": team.user_id,
        "user_email": user.email if user is not None else "",
        "team_id": team.id,
        "team_name": team.name,
        "profile_format": profile_format.get(player.profile_id, ""),
        "player_id": player.id,
        "player_name": player.name,
        "player_ratings": ratings_str(player),
        "match_type": match_type,
        "master_id": master.id if master is not None else "",
        "master_name": master.name if master is not None else "",
        "master_ratings": ratings_str(master) if master is not None else "",
        "action": action or "",
    }


def _write_csv(rows):
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    out_dir = os.path.join(base_dir, "migration_reports")
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"ppm01_dryrun_{ts}.csv")
    fields = [
        "user_id", "user_email", "team_id", "team_name", "profile_format",
        "player_id", "player_name", "player_ratings",
        "match_type", "master_id", "master_name", "master_ratings", "action",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="PPM-01 — Link Player rows to MasterPlayer / UserPlayer pool entities."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only; write CSV report, no DB changes.")
    parser.add_argument("--commit", action="store_true",
                        help="Execute and commit the linkage.")
    args = parser.parse_args()

    if args.dry_run == args.commit:
        parser.error("Pass exactly one of --dry-run or --commit.")

    from database import db as _db
    from app import create_app

    _app = create_app()

    print("=" * 60)
    print(f"PPM-01 — Player Pool Linkage ({'DRY-RUN' if args.dry_run else 'COMMIT'})")
    print("=" * 60)

    run_migration(_db, _app)
    link_players(_db, _app, dry_run=args.dry_run)
    print("Done.")
