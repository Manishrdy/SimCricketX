"""
Repair Orphaned Tournament Matches
====================================
Finds tournament matches that were persisted to the ``matches`` table but never
linked to their ``tournament_fixtures`` row (``fixture.match_id IS NULL``).

Root cause (pre-fix): the archiver's ``_save_to_database()`` committed the
DBMatch row before the tournament completion handler ran.  If the handler then
failed, the DBMatch row survived but the fixture link was never set, so
``update_standings()`` found no fixture and silently returned False.

Two classes of orphan are found and handled differently:

  FIXABLE — real gap:
    A Scheduled fixture for the same tournament+team pair exists with
    ``match_id IS NULL`` and ``standings_applied = False``.
    Action: link the fixture, call ``update_standings()``.

  DUPLICATE-PLAY — junk rows:
    The fixture for the same pair already has ``match_id`` pointing to a
    *different* match (the first successful play) and ``standings_applied=True``.
    The user re-played because the first result appeared unregistered; standings
    are already correct.  These rows cannot affect standings but are noise.
    Action (opt-in): delete with ``--delete-duplicates``.

Usage:
    # Dry run — read-only report:
    python migrations/repair_tournament_standings.py

    # Apply fixture links + standings only:
    python migrations/repair_tournament_standings.py --apply

    # Apply AND delete duplicate-play junk rows:
    python migrations/repair_tournament_standings.py --apply --delete-duplicates

    # Delete duplicates without re-applying standings:
    python migrations/repair_tournament_standings.py --delete-duplicates
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def _find_orphans(db):
    """
    Return a tuple of three lists:
      fixable         : list of (Match, TournamentFixture)
      duplicate_plays : list of Match   — fixture already linked elsewhere
      no_fixture      : list of Match   — no fixture at all for this pair
    """
    from database.models import Match as DBMatch, TournamentFixture

    orphaned = (
        DBMatch.query
        .filter(DBMatch.tournament_id.isnot(None))
        .outerjoin(TournamentFixture, TournamentFixture.match_id == DBMatch.id)
        .filter(TournamentFixture.id.is_(None))
        .order_by(DBMatch.tournament_id, DBMatch.date)
        .all()
    )

    claimed_fixture_ids = set()
    fixable         = []
    duplicate_plays = []
    no_fixture      = []

    for match in orphaned:
        # Try to find a free fixture (unlinked, standings not yet applied)
        candidates = (
            TournamentFixture.query
            .filter_by(
                tournament_id=match.tournament_id,
                home_team_id=match.home_team_id,
                away_team_id=match.away_team_id,
            )
            .filter(
                TournamentFixture.match_id.is_(None),
                TournamentFixture.standings_applied == False,  # noqa: E712
            )
            .order_by(TournamentFixture.round_number)
            .all()
        )

        candidate = next((c for c in candidates if c.id not in claimed_fixture_ids), None)
        if candidate:
            claimed_fixture_ids.add(candidate.id)
            fixable.append((match, candidate))
            continue

        # No free fixture — check if one exists but is already linked elsewhere
        linked_fixture = (
            TournamentFixture.query
            .filter_by(
                tournament_id=match.tournament_id,
                home_team_id=match.home_team_id,
                away_team_id=match.away_team_id,
            )
            .filter(
                TournamentFixture.match_id.isnot(None),
                TournamentFixture.standings_applied == True,  # noqa: E712
            )
            .first()
        )
        if linked_fixture:
            duplicate_plays.append(match)
        else:
            no_fixture.append(match)

    return fixable, duplicate_plays, no_fixture


# ---------------------------------------------------------------------------
# Dry-run report
# ---------------------------------------------------------------------------

def _report(fixable, duplicate_plays, no_fixture):
    from database.models import Tournament, Team, TournamentTeam, TournamentFixture

    def _tname(tid):
        t = Team.query.get(tid)
        return t.name if t else f"(id={tid})"

    def _tourn(tid):
        t = Tournament.query.get(tid)
        return t.name if t else f"(id={tid})"

    sep = "=" * 80
    total = len(fixable) + len(duplicate_plays) + len(no_fixture)
    print(sep)
    print("Repair Orphaned Tournament Matches — DRY RUN")
    print(sep)
    print(f"  Total orphaned matches   : {total}")
    print(f"  Fixable (standings gap)  : {len(fixable)}")
    print(f"  Duplicate play (junk)    : {len(duplicate_plays)}")
    print(f"  No fixture at all        : {len(no_fixture)}")
    print()

    # ---- FIXABLE ---------------------------------------------------------
    if fixable:
        by_tourn = {}
        for match, fixture in fixable:
            by_tourn.setdefault(match.tournament_id, []).append((match, fixture))

        print("── FIXABLE ─────────────────────────────────────────────────────────────────")
        for tid, pairs in by_tourn.items():
            print(f"\n  Tournament: {_tourn(tid)}  (id={tid})")

            standings = (
                TournamentTeam.query
                .filter_by(tournament_id=tid)
                .order_by(TournamentTeam.points.desc(), TournamentTeam.net_run_rate.desc())
                .all()
            )
            if standings:
                print(f"  Current standings ({len(standings)} teams):")
                for s in standings:
                    print(
                        f"    {_tname(s.team_id):<24} "
                        f"P={s.played} W={s.won} L={s.lost} "
                        f"Pts={s.points}  NRR={s.net_run_rate:+.3f}"
                    )
            else:
                print("  Current standings: (none yet)")

            print(f"\n  Matches to repair ({len(pairs)}):")
            for match, fixture in pairs:
                winner = _tname(match.winner_team_id) if match.winner_team_id else "Tie / No-Result"
                print(f"    Match  : {match.id}")
                print(f"    Teams  : {_tname(match.home_team_id)} vs {_tname(match.away_team_id)}")
                print(f"    Score  : {match.home_team_score}/{match.home_team_wickets} "
                      f"({match.home_team_overs}) vs "
                      f"{match.away_team_score}/{match.away_team_wickets} "
                      f"({match.away_team_overs})")
                print(f"    Result : {match.result_description or '—'}")
                print(f"    Winner : {winner}")
                print(
                    f"    Fixture: #{fixture.id}  stage={fixture.stage}  "
                    f"round={fixture.round_number}  status={fixture.status}"
                )
                print()

    # ---- DUPLICATE PLAYS -------------------------------------------------
    if duplicate_plays:
        print("── DUPLICATE-PLAY JUNK (fixture already linked to an earlier play) ─────────")
        print("   These rows have no standings impact; standings were already applied via")
        print("   the first successful play.  Use --delete-duplicates to remove them.")
        print()
        by_tourn = {}
        for match in duplicate_plays:
            by_tourn.setdefault(match.tournament_id, []).append(match)
        for tid, matches in by_tourn.items():
            print(f"  Tournament: {_tourn(tid)}  (id={tid})")
            for match in matches:
                # Find the fixture that was linked elsewhere to show context
                linked_fix = (
                    TournamentFixture.query
                    .filter_by(
                        tournament_id=match.tournament_id,
                        home_team_id=match.home_team_id,
                        away_team_id=match.away_team_id,
                    )
                    .filter(TournamentFixture.match_id.isnot(None))
                    .first()
                )
                other_match_id = linked_fix.match_id if linked_fix else "?"
                print(
                    f"    {match.id}  |  "
                    f"{_tname(match.home_team_id)} vs {_tname(match.away_team_id)}"
                )
                print(f"    Result : {match.result_description or '—'}")
                print(f"    Already counted via match: {other_match_id}")
                print()

    # ---- NO FIXTURE ------------------------------------------------------
    if no_fixture:
        print("── NO FIXTURE FOUND (manual review needed) ─────────────────────────────────")
        for match in no_fixture:
            print(
                f"  {match.id}  tournament={_tourn(match.tournament_id)}  "
                f"{_tname(match.home_team_id)} vs {_tname(match.away_team_id)}  "
                f"| {match.result_description or '—'}"
            )
        print()

    return len(fixable), len(duplicate_plays), len(no_fixture)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _apply_fixes(db, fixable):
    """Link fixtures and apply standings for fixable orphans."""
    from engine.tournament_engine import TournamentEngine
    from database.models import Team

    def _tname(tid):
        t = Team.query.get(tid)
        return t.name if t else f"id={tid}"

    engine  = TournamentEngine()
    applied = 0
    skipped = 0
    errors  = []

    print("=" * 80)
    print(f"Applying standings repairs for {len(fixable)} match(es) …")
    print("=" * 80)

    for idx, (match, fixture) in enumerate(fixable, 1):
        home = _tname(match.home_team_id)
        away = _tname(match.away_team_id)
        print(f"\n[{idx}/{len(fixable)}] Match {match.id}  ({home} vs {away})")
        print(f"  Linking to fixture #{fixture.id} "
              f"(stage={fixture.stage}, round={fixture.round_number})")
        try:
            # Step 1: Set match_id so update_standings() can find the fixture.
            fixture.match_id = match.id
            db.session.flush()

            # Step 2: Apply standings + commit.
            # update_standings() also sets fixture.status='Completed',
            # fixture.winner_team_id, and fixture.standings_applied=True.
            ok = engine.update_standings(match, commit=True)

            if ok:
                print(f"  ✓ Standings applied and committed.")
                applied += 1
            else:
                print(f"  ⚠ update_standings returned False (already applied?).")
                skipped += 1

        except Exception as exc:
            db.session.rollback()
            msg = f"match {match.id}: {type(exc).__name__}: {exc}"
            print(f"  ✗ FAILED — {msg}")
            errors.append(msg)

    return applied, skipped, errors


def _delete_duplicates(db, duplicate_plays):
    """Delete duplicate-play junk rows from matches (and cascade scorecards)."""
    from database.models import Match as DBMatch

    deleted = 0
    errors  = []

    print("=" * 80)
    print(f"Deleting {len(duplicate_plays)} duplicate-play junk match row(s) …")
    print("=" * 80)

    for match in duplicate_plays:
        print(f"  Deleting {match.id} …", end="  ")
        try:
            db.session.delete(match)
            db.session.commit()
            print("✓")
            deleted += 1
        except Exception as exc:
            db.session.rollback()
            msg = f"match {match.id}: {type(exc).__name__}: {exc}"
            print(f"✗ {msg}")
            errors.append(msg)

    return deleted, errors


# ---------------------------------------------------------------------------
# Post-repair standings snapshot
# ---------------------------------------------------------------------------

def _print_post_standings(fixable):
    from database.models import TournamentTeam, Tournament, Team

    def _tname(tid):
        t = Team.query.get(tid)
        return t.name if t else f"id={tid}"

    tournament_ids = sorted({m.tournament_id for m, _ in fixable})
    print()
    print("POST-REPAIR STANDINGS")
    print("-" * 80)
    for tid in tournament_ids:
        t = Tournament.query.get(tid)
        print(f"\n  {t.name if t else tid}  (tournament_id={tid})")
        standings = (
            TournamentTeam.query
            .filter_by(tournament_id=tid)
            .order_by(TournamentTeam.points.desc(), TournamentTeam.net_run_rate.desc())
            .all()
        )
        if standings:
            print(f"  {'Team':<24} {'P':>3} {'W':>3} {'L':>3} {'T':>3} {'NR':>3} "
                  f"{'Pts':>4}  {'NRR':>7}")
            print(f"  {'-'*24} {'-'*3} {'-'*3} {'-'*3} {'-'*3} {'-'*3} "
                  f"{'-'*4}  {'-'*7}")
            for s in standings:
                print(
                    f"  {_tname(s.team_id):<24} {s.played:>3} {s.won:>3} {s.lost:>3} "
                    f"{s.tied:>3} {s.no_result:>3} {s.points:>4}  "
                    f"{s.net_run_rate:>+7.3f}"
                )
        else:
            print("  (no standings rows)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(db, app, apply=False, delete_duplicates=False):
    with app.app_context():
        fixable, duplicate_plays, no_fixture = _find_orphans(db)
        total = len(fixable) + len(duplicate_plays) + len(no_fixture)

        if total == 0:
            print("No orphaned tournament matches found. DB is clean.")
            return

        _report(fixable, duplicate_plays, no_fixture)

        if not apply and not delete_duplicates:
            print()
            print("DRY RUN — no changes made.")
            print("  --apply              link fixtures and re-apply standings")
            print("  --delete-duplicates  remove duplicate-play junk rows")
            print("  Both flags can be combined.")
            return

        summary_lines = []

        if apply and fixable:
            applied, skipped, errors = _apply_fixes(db, fixable)
            summary_lines.append(
                f"Standings: applied={applied}  skipped={skipped}  errors={len(errors)}"
            )
            if errors:
                print("\nStandings errors:")
                for e in errors:
                    print(f"  • {e}")
            if applied > 0:
                _print_post_standings(fixable)

        if delete_duplicates and duplicate_plays:
            deleted, errors = _delete_duplicates(db, duplicate_plays)
            summary_lines.append(
                f"Duplicates: deleted={deleted}  errors={len(errors)}"
            )
            if errors:
                print("\nDeletion errors:")
                for e in errors:
                    print(f"  • {e}")

        print()
        print("=" * 80)
        print("FINAL SUMMARY")
        for line in summary_lines:
            print(f"  {line}")
        print("=" * 80)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Repair orphaned tournament match fixture links and recalculate standings."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Link fixtures and re-apply standings for fixable orphans.",
    )
    parser.add_argument(
        "--delete-duplicates",
        action="store_true",
        dest="delete_duplicates",
        help="Delete duplicate-play junk rows from matches table.",
    )
    args = parser.parse_args()

    from database import db as _db
    from app import create_app

    _app = create_app()
    run(_db, _app, apply=args.apply, delete_duplicates=args.delete_duplicates)
