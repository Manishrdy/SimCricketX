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
    home_team_id/away_team_id populated (a bogus "Bye" advance), or
  - status='Scheduled', match_id IS NULL, both teams populated from those
    bogus winners — a playable match between two teams that never earned
    their place ('fabricated' below).

Downstream fallout: the tournament's `current_stage` gets frozen at an
earlier round forever (an "Active" tournament nobody can ever finish
playing), or worse — `tournament.status` flips to 'Completed' with a
fake/missing champion, because `_check_tournament_completion` only counts
fixture *statuses*, not whether those statuses are trustworthy.

This script walks each knockout tournament's bracket round by round and:
  1. Resets any fixture whose true feeders are NOT both actually decided
     back to the 'Locked' placeholder `_generate_knockout` would have
     created (undoing the premature marking) — never touching one that
     already has a real match linked.
  2. Re-resolves (via the same `TournamentEngine._resolve_round_pair` the
     live code now uses, so this script can never drift from production
     behavior) any fixture whose true feeders ARE both decided — so a
     fixture that should be a real 'Scheduled' match, a Bye, or a genuine
     Phantom ends up in that correct state.
  3. Recomputes `tournament.current_stage` and `tournament.status` from the
     corrected fixture tree.

'Scheduled' fixtures need the extra `--include-suspects` flag, because
status alone can't distinguish a fabricated pairing from a match a user is
mid-way through simulating right now (`match_id` is only written at
completion). With the flag, that one ambiguity is resolved against
data/matches — the setup files that record a `fixture_id` while a match is
live — and only genuinely in-flight fixtures are left for a human.

Usage:
    # Dry run — read-only report:
    python migrations/repair_knockout_bracket_corruption.py

    # Apply the fixes:
    python migrations/repair_knockout_bracket_corruption.py --apply

    # Full repair, including fabricated 'Scheduled' pairings:
    python migrations/repair_knockout_bracket_corruption.py --apply --include-suspects
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# In-flight match detection
# ---------------------------------------------------------------------------

def _in_flight_fixture_ids():
    """Fixture ids that have a match currently in flight, read from disk.

    A fixture's `match_id` is only written once the match *completes*
    (app.py, "Step 9"), so the DB alone genuinely cannot tell an idle
    fixture apart from one a user is twenty overs into right now. The
    setup file at data/matches/match_<uuid>.json does record `fixture_id`
    the moment a match starts, and the cleanup scheduler deletes those
    files 24h after their last write — so a file here means a match was
    started on that fixture within the last day and may still be live,
    while its absence means nothing is in flight.

    Returns a set of fixture ids, or None if the directory could not be
    read cleanly — None means "every suspect must be treated as live",
    because the safe error is refusing to touch a fixture, never mutating
    one out from under a match in progress.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    match_dir = os.path.join(project_root, "data", "matches")
    in_flight = set()
    if not os.path.isdir(match_dir):
        return in_flight

    for fn in os.listdir(match_dir):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(match_dir, fn)) as f:
                data = json.load(f)
        except Exception:
            # Unreadable/half-written file — can't rule out a live match.
            # Fail closed by treating every suspect as in-flight.
            return None
        raw = data.get("fixture_id")
        if raw in (None, ""):
            continue
        try:
            in_flight.add(int(raw))
        except (TypeError, ValueError):
            continue

    return in_flight


def _virgin_stage_description(fixture, final_stage):
    """The stage_description _generate_knockout gives a fresh placeholder.

    Resetting has to land on the state the generator would have produced,
    not merely a blank one — the dashboard renders this label, and a None
    here survives every later legitimate resolution (_resolve_round_pair
    only writes stage_description for its Bye and Phantom outcomes).
    """
    return "Tournament Winner" if fixture.stage == final_stage else "Winner advances"


# ---------------------------------------------------------------------------
# Per-tournament diagnosis / repair
# ---------------------------------------------------------------------------

def _diagnose_and_repair(engine, tournament, fixtures_by_bp, rounds,
                        include_suspects=False, in_flight=None):
    """
    Walk the bracket round by round, correcting fixtures in place (mutating
    the ORM objects in fixtures_by_bp — callers decide whether to commit).

    Returns (actions, suspects):
      actions  — (fixture, action, detail) for every fixture actually
                 changed. action is 'reset' (undid a premature marking) or
                 'resolved' (re-derived the correct state now that feeders
                 are truly decided).
      suspects — (fixture, detail) for fixtures left untouched because a
                 match may be in flight on them. This is the same
                 historical bug's third manifestation: the old code could
                 fabricate a 'Scheduled' pairing between two teams whose
                 qualification was never genuinely earned (e.g. a bogus
                 quarterfinal "winner" cascaded into a fabricated semifinal
                 matchup).

    Such a pairing is provably fabricated, not merely suspicious: a
    round-2+ fixture's teams can only be written by _resolve_round_pair,
    which runs only once both feeders are 'Completed', so feeders that
    aren't both decided mean no legitimate path could have produced this
    pairing. And leaving it is not the neutral option it looks like —
    _resolve_round_pair refuses to touch a 'Scheduled' fixture, so once
    the feeders reset here are genuinely replayed, the fabricated pairing
    is never overwritten and the real winners never reach their own next
    round.

    So with include_suspects=True these are reset like any other corrupted
    fixture, except where `in_flight` says a match is live on one — that
    single case is the only reason the status alone was ever ambiguous,
    and it's the only one still left for a human.
    """
    actions = []
    suspects = []
    final_stage = engine.STAGE_FINAL

    def _reset(next_fixture, note):
        """Return next_fixture to the state _generate_knockout would create."""
        before = (
            next_fixture.status, next_fixture.home_team_id,
            next_fixture.away_team_id, next_fixture.winner_team_id,
        )
        # Only ever a no-op here (suspects have no match_id, and fixtures
        # that do are skipped above), but going through the engine's own
        # cleanup means an orphaned match row left by a half-committed
        # completion is reversed rather than stranded.
        engine._cleanup_fixture_match_data(next_fixture)
        next_fixture.home_team_id = None
        next_fixture.away_team_id = None
        next_fixture.winner_team_id = None
        next_fixture.match_id = None
        next_fixture.status = 'Locked'
        next_fixture.stage_description = _virgin_stage_description(
            next_fixture, final_stage
        )
        next_fixture.standings_applied = False
        return before, f'{before} -> Locked ({note})'

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
                expected = {m1.winner_team_id, m2.winner_team_id} if both_decided else None
                current = {next_fixture.home_team_id, next_fixture.away_team_id}
                if expected is not None and expected == current:
                    # Legitimately earned and awaiting play — leave it alone.
                    continue

                reason = (
                    "its feeders are not both genuinely decided"
                    if expected is None else
                    f"its feeders actually resolve to {sorted(x for x in expected if x)}"
                )
                live = in_flight is None or next_fixture.id in in_flight

                if not include_suspects or live:
                    blocked = (
                        "a match is in flight on it (data/matches still has its "
                        "setup file) — replay or abandon that match first"
                        if live and include_suspects else
                        "not auto-corrected; re-run with --include-suspects to reset it"
                    )
                    suspects.append((
                        next_fixture,
                        f"'Scheduled' with home={next_fixture.home_team_id}, "
                        f"away={next_fixture.away_team_id}, no match_id — but "
                        f"{reason}, so this pairing was never legitimately "
                        f"earned. {blocked}.",
                    ))
                    continue

                before, detail = _reset(next_fixture, f'{reason}; pairing was fabricated')
                actions.append((next_fixture, 'unfabricate', detail))
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
                    _before, detail = _reset(
                        next_fixture, 'feeder(s) not yet actually decided'
                    )
                    actions.append((next_fixture, 'reset', detail))

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

def run(db, app, apply=False, include_suspects=False):
    with app.app_context():
        from database.models import Tournament, TournamentFixture
        from engine.tournament_engine import TournamentEngine

        engine = TournamentEngine()
        in_flight = _in_flight_fixture_ids() if include_suspects else None

        tournaments = Tournament.query.filter_by(mode='knockout').order_by(Tournament.id).all()
        if not tournaments:
            print("No knockout-mode tournaments found. Nothing to check.")
            return

        sep = "=" * 80
        print(sep)
        print(f"Repair Corrupted Knockout Bracket Fixtures — {'APPLYING' if apply else 'DRY RUN'}")
        print(sep)
        print(f"  Knockout tournaments scanned: {len(tournaments)}")
        if include_suspects:
            if in_flight is None:
                print("  Suspect fixtures : NOT resettable — data/matches unreadable, "
                      "so no match can be ruled out as live")
            else:
                print(f"  Suspect fixtures : will be reset unless in flight "
                      f"({len(in_flight)} fixture(s) currently have a match in flight)")
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
            rounds = engine.bracket_rounds(next_power)

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
                actions, suspects = _diagnose_and_repair(
                    engine, t, fixtures_by_bp, rounds,
                    include_suspects=include_suspects, in_flight=in_flight,
                )
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
                "Fabricated 'Scheduled' pairings left in place. These do NOT heal "
                "themselves: _resolve_round_pair skips a 'Scheduled' fixture, so the "
                "real winners of the rounds reset above will never replace these teams."
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
        if all_suspects and not include_suspects:
            print()
            print("Re-run with --include-suspects to reset the fabricated pairings "
                  "above (any with a match in flight are still skipped).")
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
    parser.add_argument(
        "--include-suspects",
        action="store_true",
        help="Also reset fabricated 'Scheduled' pairings — fixtures whose teams "
             "were never earned because their feeders aren't both decided. Any "
             "with a match in flight (a data/matches setup file naming them) are "
             "still skipped and reported.",
    )
    args = parser.parse_args()

    from database import db as _db
    from app import create_app

    _app = create_app()
    run(_db, _app, apply=args.apply, include_suspects=args.include_suspects)
