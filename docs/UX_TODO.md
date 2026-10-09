# UX improvement backlog

User priorities recorded October 7, 2026. Work on one item at a time.

1. [x] Return to an unfinished game — implemented October 7, 2026: conditionally show a Continue Match feature card immediately after Simulate Match on Home, occupying Create Team’s former position. Preserve Create Team and dynamically reflow the remaining cards. Remove pending matches from the profile dropdown.
2. [x] Help complete a legal squad — implemented October 7, 2026: live readiness guidance on Create/Edit Team and Manage Squad; actions filter for wicketkeepers or combined bowling options and focus captain/keeper selectors. Explain overlapping requirements and necessary replacements at the squad limit; link to create/import players.
3. [x] Replay a tournament fixture safely — implemented October 9, 2026: preview the exact affected fixtures, check ownership and squad readiness, and explicitly confirm before resetting results. Reverse statistics and standings transactionally, reject stale previews, make retries idempotent, and prevent invalidated matches from being restored. Dependent fixtures wait for qualification again; the selected fixture opens in match setup.

The first task concerns Home navigation using the existing pending-match source. Durable recovery across server restarts is a separate future improvement.

Validation (Continue Match): rendered Home using the shared QA account on a temporary database copy with zero, one, and three pending matches. Checked card order, resume URLs, exclusion of completed/other-user matches, and removal from the profile dropdown.

Validation (squad assistance): shared guidance checks cover legal, empty, missing-leader, full and oversized squads. Shared QA account on a temporary database copy rendered Create Team and T20/FC squad pages; checked inline JavaScript syntax, combined bowling-option search totals and roles, and empty search results.

Create Team refinement: consolidated format navigation and readiness above the player lists, replaced repeated checks with one next action, enlarged roster controls, and added a save summary across formats. Verified desktop (1280px) and phone (390px) layouts, adding a player, guided role filtering, and live readiness when switching formats using the shared QA account on a temporary DB copy.

Validation (fixture replay): regression coverage includes tournament modes, match formats, scorecard/stat reversal, rollback, stale previews, ownership, squad blockers, simultaneous confirmations, cleanup retries, and late match completion. Browser testing with a shared QA account on a temporary database copy verified desktop/390px previews, cancellation, confirmation into match setup, and the persisted IPL dependency reset while preserving the Eliminator result.

Replay operations: startup migration precheck creates the replay receipt and invalidated-match tables. Artifact cleanup runs after the database commit; failed cleanup is recorded and retried when the owner next visits the tournament dashboard. Replay has no undo and uses current squads and fresh setup choices.
