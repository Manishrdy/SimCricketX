"""
Repair Corrupted Knockout Bracket Fixtures
============================================
Finds `mode='knockout'` tournament fixtures (Round 2+) that were resolved by
a bug in the old `_advance_bye_winners`: at tournament-creation time, its
"Step 2" judged a next-round fixture's fate purely from whatever partial
state "Step 1" had already written into it, without ever checking whether
its two actual feeder fixtures were both genuinely decided yet. Any
next-round fixture still sitting at its completely normal starting state
(home=None, away=None, status='Locked' — the same shape as a real bye or
phantom) — because one or both of its real feeder matches simply hadn't
been played yet — got misread as an already-decided dead branch and marked
'Completed' immediately, with a bogus or missing winner, before a single
real match had been played. This affected knockout brackets with 3+ rounds
(5+ teams) regardless of whether any bye was actually involved.

Symptoms in the DB, for a round-2+ fixture:
  - status='Completed', match_id IS NULL, home_team_id and away_team_id
    both NULL ("Phantom Match" that was never actually earned), or
  - status='Completed', match_id IS NULL, exactly one of
    home_team_id/away_team_id populated (a bogus "Bye" advance).

Downstream fallout: the tournament's `current_stage` gets frozen at an
earlier round forever (an "Active" tournament nobody can ever finish
playing), or worse — `tournament.status` flips to 'Completed' with a
fake/missing champion, because `_check_tournament_completion` only counts
fixture *statuses*, not whether those statuses are trustworthy.

This script walks each knockout tournament's bracket round by round and:
  1. Resets any fixture whose true feeders are NOT both actually decided
     back to a clean 'Locked' placeholder (undoing the premature marking) —
     unless it already has a real match linked or is already a real
     'Scheduled' match, which are never touched.
  2. Re-resolves (via the same `TournamentEngine._resolve_round_pair` the
     live code now uses, so this script can never drift from production
     behavior) any fixture whose true feeders ARE both decided — so a
     fixture that should be a real 'Scheduled' match, a Bye, or a genuine
     Phantom ends up in that correct state.
  3. Recomputes `tournament.current_stage` and `tournament.status` from the
     corrected fixture tree.

Usage:
    # Dry run — read-only report:
    python migrations/repair_knockout_bracket_corruption.py

    # Apply the fixes:
    python migrations/repair_knockout_bracket_corruption.py --apply
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Bracket geometry — must exactly match TournamentEngine._advance_bye_winners
# ---------------------------------------------------------------------------

def _bracket_rounds(next_power):
    """[(start_bracket_pos, match_count), ...] for a knockout bracket."""
    rounds = []
    start = 0
    count = next_power // 2
    while count >= 1:
        rounds.append((start, count))
        start += count
        count //= 2
    return rounds


# ---------------------------------------------------------------------------
# Per-tournament diagnosis / repair
# ---------------------------------------------------------------------------

def _diagnose_and_repair(engine, tournament, fixtures_by_bp, rounds):
    """
    Walk the bracket round by round, correcting fixtures in place (mutating
    the ORM objects in fixtures_by_bp — callers decide whether to commit).

    Returns (actions, suspects):
      actions  — (fixture, action, detail) for every fixture actually
                 changed. action is 'reset' (undid a premature marking) or
                 'resolved' (re-derived the correct state now that feeders
                 are truly decided).
      suspects — (fixture, detail) for fixtures that are NEVER auto-mutated
                 (status='Scheduled', no match_id — a user may be actively
                 mid-match on it right now) but whose current team
                 assignment doesn't match what its own feeders actually
                 produce. This is the same historical bug's third
                 manifestation: the old code could fabricate a 'Scheduled'
                 pairing between two teams whose qualification was never
                 genuinely earned (e.g. a bogus quarterfinal "winner"
                 cascaded into a fabricated semifinal matchup). These need
                 a human to check whether the match is actually in progress
                 before anything touches them.
    """
    actions = []
    suspects = []

    for r_idx in range(len(rounds) - 1):
        r_start, r_count = rounds[r_idx]
        nr_start, _ = rounds[r_idx + 1]

        for i in range(0, r_count, 2):
            m1 = fixtures_by_bp.get(r_start + i)
            m2 = fixtures_by_bp.get(r_start + i + 1)
            next_fixture = fixtures_by_bp.get(nr_start + (i // 2))
            if not m1 or not m2 or not next_fixture:
                continue

            # A real, already-played match is undeniable ground truth —
            # never touch it, automatically or otherwise.
            if next_fixture.match_id:
                continue

            both_decided = m1.status == 'Completed' and m2.status == 'Completed'

            if next_fixture.status == 'Scheduled':
                # Never auto-mutate a 'Scheduled' fixture — a real match may
                # be in progress on it. But flag it if its current teams
                # don't match what its feeders would actually produce.
                expected = {m1.winner_team_id, m2.winner_team_id} if both_decided else None
                current = {next_fixture.home_team_id, next_fixture.away_team_id}
                if expected is None or expected != current:
                    reason = (
                        "feeders are not both genuinely decided yet"
                        if expected is None else
                        f"feeders actually resolve to {sorted(x for x in expected if x)}"
                    )
                    suspects.append((
                        next_fixture,
                        f"'Scheduled' with home={next_fixture.home_team_id}, "
                        f"away={next_fixture.away_team_id}, no match_id — but {reason}. "
                        "This pairing may never have been legitimately earned. "
                        "NOT auto-corrected — check whether a match is in progress "
                        "on it before touching it.",
                    ))
                continue

            if both_decided:
                before = (
                    next_fixture.status, next_fixture.home_team_id,
                    next_fixture.away_team_id, next_fixture.winner_team_id,
                )
                engine._resolve_round_pair(m1, m2, next_fixture)
                after = (
                    next_fixture.status, next_fixture.home_team_id,
                    next_fixture.away_team_id, next_fixture.winner_team_id,
                )
                if before != after:
                    actions.append((next_fixture, 'resolved', f'{before} -> {after}'))
            else:
                already_clean = (
                    next_fixture.status == 'Locked'
                    and next_fixture.home_team_id is None
                    and next_fixture.away_team_id is None
                )
                if not already_clean:
                    before = (
                        next_fixture.status, next_fixture.home_team_id,
                        next_fixture.away_team_id, next_fixture.winner_team_id,
                    )
                    next_fixture.home_team_id = None
                    next_fixture.away_team_id = None
                    next_fixture.winner_team_id = None
                    next_fixture.status = 'Locked'
                    next_fixture.stage_description = None
                    next_fixture.standings_applied = False
                    actions.append((
                        next_fixture, 'reset',
                        f'{before} -> Locked (feeder(s) not yet actually decided)',
                    ))

    return actions, suspects


def _recompute_tournament_state(tournament, rounds, fixtures_by_bp):
    """Recompute current_stage/status from the corrected fixture tree.
    Mirrors TournamentEngine._check_tournament_completion's own definition
    of "done": no fixture left in Scheduled/Locked.
    """
    changes = []

    new_stage = 'completed'
    for r_start, r_count in rounds:
        round_fixtures = [
            fixtures_by_bp[r_start + i]
            for i in range(r_count)
            if (r_start + i) in fixtures_by_bp
        ]
        if not round_fixtures:
            continue
        if any(f.status != 'Completed' for f in round_fixtures):
            new_stage = round_fixtures[0].stage
            break

    if tournament.current_stage != new_stage:
        changes.append(('current_stage', tournament.current_stage, new_stage))
        tournament.current_stage = new_stage

    pending = sum(1 for f in fixtures_by_bp.values() if f.status in ('Scheduled', 'Locked'))
    new_status = 'Completed' if pending == 0 else 'Active'
    if tournament.status != new_status:
        changes.append(('status', tournament.status, new_status))
        tournament.status = new_status

    return changes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(db, app, apply=False):
    with app.app_context():
        from database.models import Tournament, TournamentFixture
        from engine.tournament_engine import TournamentEngine

        engine = TournamentEngine()

        tournaments = Tournament.query.filter_by(mode='knockout').order_by(Tournament.id).all()
        if not tournaments:
            print("No knockout-mode tournaments found. Nothing to check.")
            return

        sep = "=" * 80
        print(sep)
        print(f"Repair Corrupted Knockout Bracket Fixtures — {'APPLYING' if apply else 'DRY RUN'}")
        print(sep)
        print(f"  Knockout tournaments scanned: {len(tournaments)}")
        print()

        touched_tournaments = 0
        total_actions = 0
        all_suspects = []
        errors = []

        for t in tournaments:
            num_teams = len(t.participating_teams)
            if num_teams < 2:
                continue
            next_power = engine._next_power_of_two(num_teams)
            rounds = _bracket_rounds(next_power)

            fixtures_by_bp = {
                f.bracket_position: f
                for f in TournamentFixture.query.filter(
                    TournamentFixture.tournament_id == t.id,
                    TournamentFixture.bracket_position.isnot(None),
                ).all()
            }
            if not fixtures_by_bp:
                continue

            try:
                orig_status = t.status
                actions, suspects = _diagnose_and_repair(engine, t, fixtures_by_bp, rounds)
                state_changes = _recompute_tournament_state(t, rounds, fixtures_by_bp)

                if suspects:
                    all_suspects.append((t, suspects))

                if not actions and not state_changes:
                    continue

                touched_tournaments += 1
                total_actions += len(actions)
                print(f"── Tournament {t.id}: {t.name!r} (currently status={orig_status}) ──")
                for fixture, action, detail in actions:
                    print(f"    fixture {fixture.id:>6} (round {fixture.round_number}, "
                          f"stage={fixture.stage}, bp={fixture.bracket_position}): "
                          f"{action.upper():<8} {detail}")
                for field, before, after in state_changes:
                    print(f"    tournament.{field}: {before!r} -> {after!r}")
                print()

                if apply:
                    db.session.commit()
                else:
                    db.session.rollback()

            except Exception as exc:
                db.session.rollback()
                msg = f"tournament {t.id}: {type(exc).__name__}: {exc}"
                print(f"  ✗ FAILED — {msg}")
                errors.append(msg)

        if all_suspects:
            print(sep)
            print("SUSPECT FIXTURES — NEVER AUTO-TOUCHED, NEEDS MANUAL REVIEW")
            print(sep)
            print(
                "These are 'Scheduled' with no match_id (this script never mutates "
                "that — a real match may be in progress on one right now), but their "
                "team pairing doesn't check out against their own feeders. Check "
                "whether each one actually has a match being played before touching it."
            )
            print()
            for t, suspects in all_suspects:
                print(f"── Tournament {t.id}: {t.name!r} ──")
                for fixture, detail in suspects:
                    print(f"    fixture {fixture.id:>6} (round {fixture.round_number}, "
                          f"stage={fixture.stage}, bp={fixture.bracket_position}): {detail}")
                print()

        print(sep)
        print("SUMMARY")
        print(f"  Tournaments needing repair   : {touched_tournaments}")
        print(f"  Fixture-level fixes          : {total_actions}")
        print(f"  Suspect fixtures (unresolved): {sum(len(s) for _, s in all_suspects)}")
        print(f"  Errors                       : {len(errors)}")
        if not apply and touched_tournaments:
            print()
            print("DRY RUN — no changes were committed. Re-run with --apply to fix.")
        print(sep)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Repair knockout-bracket fixtures corrupted by the historical "
                     "premature bye/phantom resolution bug."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the fixes. Without this flag, changes are computed and reported "
             "then rolled back (dry run).",
    )
    args = parser.parse_args()

    from database import db as _db
    from app import create_app

    _app = create_app()
    run(_db, _app, apply=args.apply)
