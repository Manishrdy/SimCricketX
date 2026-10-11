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


## Dedicated stadiums — October 10, 2026

Implemented the optional stadium adoption flow for existing teams and required stadium selection for new teams. Added the catalogue, private custom grounds, neutral venue selection, seasonal condition previews and immutable match conditions. Existing text grounds remain unchanged until explicitly linked. See [stadium schema and behaviour](STADIUMS.md). Starter numerical profiles are labelled estimates, pending measurement-based historical calibration.

Validation: shared QA accounts on temporary databases; legacy/new team controls checked in a browser; catalogue and match conditions checked at desktop and 390px widths; migration, ownership, adoption, revisions, all five formats and checkpoint compatibility covered by tests.

## Compact team cards — October 10, 2026

Replaced the expanded multi-format details on `/teams/manage` with an accessible format selector and one squad summary at a time. Includes FC, List A, T20, T10 and Hundred; shows the selected squad's player count, captain, captaincy preset and direct Manage squad link. Ground and pitch appear once, with tighter card and page spacing and compact phone statistics.

Validation: authenticated route rendered with the shared QA account on a temporary database copy; browser previews covered five-format, single-format, long-name and empty teams. Verified every format's details and squad URL, independent selections, arrow-key wraparound, captain search, delete-dialog cancellation, light/dark themes and 390px layout without horizontal overflow. Existing draft-recovery checks passed.

## Captaincy studio on Create / Edit Team — October 10, 2026

Moved captain and wicketkeeper selection into a dedicated Leadership section below the player lists, beside a redesigned captaincy studio. Visible Balanced, Attacking and Conservative style cards use matching typography, themed teal selection states and distinct icons. Batting and bowling aggression have labelled sliders and numerical values; the five decision-quality attributes sit in an expandable fine-tuning area. An active-format badge makes the settings' scope explicit. Desktop uses two columns; phones stack the controls.

Validation: shared QA account with an isolated database; Create and Edit Team checked in light/dark themes and at desktop/390px without horizontal overflow. Verified preset selection, keyboard sliders, custom style detection, zero-valued attributes, independent format settings, preservation when changing captains, saved draft recovery and an actual QA edit save. Confirmed persisted per-format values in the temporary database. Eight existing captaincy draft, team draft and validation checks passed. The Manage Squad editor keeps its existing layout.

Player-panel refinement: Create/Edit Team now use equal-width panels with shared header and list rows, keeping both top and bottom edges aligned when copy-squad controls appear. Checked populated Edit Team and empty Create Team in the browser; panel widths, heights and list start positions match. Phone layout keeps separate stacked panels and equal list heights without horizontal overflow.

## Stadium management entry point — October 10, 2026

Added a Stadiums dashboard card and explicit create, view, edit and use-for-new-team actions in the catalogue. Stadium pages now share the main navigation and light/dark theme. The team shortcut preselects an available stadium; custom grounds remain private. Removal uses the existing retirement operation, preserving revisions and past match conditions while excluding the ground from future choices.

Validation: all 36 stadium tests passed, including a shared-QA-account create/edit/retire flow and team preselection with private/retired ground exclusion. Browser previews on an isolated database verified the dashboard and catalogue at desktop width and the catalogue/editor at 390px without horizontal overflow.

## Stadium catalogue redesign — October 10, 2026

Rebuilt `/stadiums` as an illustrated ground collection: a forest-green hero with a cricket-ground drawing, collection totals, official/private collection navigation, country dropdown and search, and venue cards with T20 surface tendencies and typical straight boundaries. Cards retain explore, edit and use-for-new-team actions. Added tailored empty states and a short simulation-profile explanation. Counts and country choices use only stadiums visible to the current user; filters preserve the selected collection. The detail/editor layout remains separate.

Validation: all 36 stadium regression tests passed with shared QA accounts. Browser previews from an isolated database covered the catalogue, India filter, empty search and private collection at 1280px, 390px and 320px in light/dark themes without horizontal overflow. Visually checked desktop and phone layouts.

## Stadium collection navigation and venue profiles — October 10, 2026

Collection tabs, search and filter reset now update results in place, retaining scroll position and keyboard focus rather than reloading the catalogue. URLs remain shareable and browser Back/Forward restores the selected collection. New requests cancel older requests; failed updates keep the current results and display a retry message. Standard links and forms remain available without JavaScript.

Redesigned stadium detail pages, including Basin Reserve, with an illustrated venue header, section navigation, preparation-probability bars for every format, typical/range boundary dimensions, monthly seasonal conditions and expandable source details. Styled the existing creation/editing controls and retirement area to match. Existing save, ownership, revision and retirement behavior is preserved.

Validation: all 36 stadium regression tests passed. Shared QA account on an isolated DB rendered the exact requested venue and creation page. Browser checks covered in-place tabs, scroll retention, Back/Forward, rapid competing clicks, failed-request recovery and search/reset. Detail and creation layouts were checked at 1280px, 390px and 320px in light and dark themes with no horizontal page overflow.

## Simplified stadium profiles — October 10, 2026

Replaced stadium profiles with name, five format pitch defaults, outfield speed, four fixed boundary distances and average temperature. Removed country/timezone, location metadata, advanced JSON controls, monthly climate, boundary ranges and stadium drainage/dew profiles. New matches use immutable `stadium_v2` snapshots; temperature affects existing pitch wear. Weather/day-night remain match settings and pitch can be changed during setup. Current v1 profiles migrate to new v2 revisions without rewriting historical profiles, team/fixture links or saved matches. Numerical defaults are simulation presets.

Validation: 70 stadium tests and 166 related weather/Hundred/tie-breaker/captaincy tests passed; 17 focused JavaScript checks passed. Browser QA used shared accounts on an isolated database to create a simple stadium, verify per-format defaults and match setup resets, and inspect desktop/390px layouts in light/dark themes. Rehearsed migration twice on a copy of the local DB: 16 converted profiles, with team, fixture, match and historical v1 revision rows unchanged. The full JavaScript run passed 155/156 checks; its remaining unrelated team-builder assertion expects unequal columns despite the existing equal-width layout. The broad database checker reports 41 existing foreign-key violations and an unverified unrelated audit-log cleanup; stadium conversion itself passed.

## Stadium editor layout correction — October 10, 2026

Moved the Create/Edit stadium heading inside the form card, replacing the oversized floating legend. Grouped pitch defaults, boundary dimensions, and outfield/temperature into separate sections with consistent labels, input heights and spacing. Desktop uses aligned grids; phones stack fields and provide a full-width save button. Helper text is associated with the corresponding inputs.

Validation: four existing stadium form/route checks passed. Shared QA admin on an isolated database verified Wankhede editing and a saved pitch change. Browser checks at 1280px, 390px and 320px showed no horizontal overflow; inspected light/dark themes.

## Private stadium conditions — October 11, 2026

Added user-owned v3 stadium revisions with captured per-format baselines, independent blending, pitch scoring/wicket/pace/spin adjustments and limited-overs phase/chasing adjustments. Official venues are read-only templates with explicit private copying; existing private grounds adopt conditions under their original ID without relinking teams or fixtures. Adoption receipts make retries idempotent. New selection requires an adopted private venue, while existing links and saved legacy/v1/v2 matches retain compatibility. New v3 matches freeze configuration, weather and provenance; Hundred phases use integer legal-ball progress, including reduced innings. FC excludes phase controls.

The editor preserves independent unsaved format/pitch settings, supports 0× multipliers and slider endpoints, displays format-specific ranges and recovers local drafts. Ground Conditions navigation is retired; bookmarked editor/guide URLs redirect and mutation endpoints return 410. Stored user tuning remains available internally. `STADIUM_V3_ENABLED` controls new creation/adoption/v3 matches independently of saved-match resume.

Validation: 196 focused Python checks passed with one skipped, including ownership, adoption retries, baseline preservation, strict validation, neutral tuning, all five formats, Hundred reductions, migration, weather/rain and checkpoints/tie breakers. All 12 focused JavaScript checks passed. Shared QA accounts and isolated databases were used. Rehearsed the additive migration twice on a copy of the local database, verifying historical rows, user tuning, links and saved matches were unchanged. Browser checks covered zero persistence, format isolation, draft recovery, List A 40-over/Hundred ranges, desktop and 390px/320px widths in light/dark themes without horizontal overflow. A broader engine run passed 158 tests but failed the T20 captaincy shootout checkpoint random-stream comparison; it passed in isolation, and its cause remains unresolved. Existing unrelated checkout changes were preserved.


## Team stadium selection — October 11, 2026

Create/Edit Team now have a dedicated home-stadium card with a selected venue summary and a Stadiums link. Removed inline custom stadium creation, including its backend creation path and draft fields. Users create or adopt stadiums on Stadiums, then select them on the team form. New choices use the current v3 format; existing stadium links can be retained. Legacy text grounds are read-only, and attempts to change them through a team POST are rejected. Stadium changes save with the team.

Validation: 131 stadium Python checks and 11 team-form JavaScript checks passed, covering inline-creation rejection, accessible v3 choices, legacy preservation, selected venue identity, draft recovery and clearing selection. Both pages rendered with shared QA accounts on an isolated database. Visual browser inspection was unavailable because the local preview environment blocked serving/opening the rendered pages.


## Direct stadium adoption and official administration — October 11, 2026

Catalogue cards now add venues directly to My stadiums through a CSRF-protected POST, refresh collection counts in place, and show an added state. The non-JavaScript form returns to My stadiums. Explore opens venue details; official edit links are available to admins. Admins can edit official venue details and conditions using the form or API; the first save upgrades an official v2 venue to v3 with factory baselines.

Official updates append immutable revisions to unedited adopted copies within the same transaction, preserving their IDs, team/fixture links and captured baselines. Personal edits mark a complete private override that later official changes cannot replace. Unchanged saves retain official inheritance. Re-adding a venue after official updates reuses the user's active copy. Existing edited copies and saved matches retain their settings.

Validation: 133 stadium Python regression checks passed, followed by four focused checks covering the final official-update behavior, including two additional tests for frozen match snapshots and unchanged personal saves. Seven stadium JavaScript checks passed, including direct card adoption, duplicate-click prevention, catalogue refresh and failure/retry behavior. HTML editor and non-JavaScript adoption were tested with shared QA accounts on isolated databases; permission, revision, inheritance and private-override behavior were verified.

## Match detail cleanup and simulation recovery — October 11, 2026

Removed the Captain tactics panel and expandable Stadium conditions summary from the match detail page. Fixed v3 stadium phase inputs to use scoreboard legal-ball progress instead of the pitch-wear counter, which includes wides/no-balls. The old counter could exceed the innings budget before the last legal ball, repeatedly throwing after reload and triggering the delivery-conflict banner.

Validation: 76 focused Python tests and 13 JavaScript delivery/resume checks passed. Added regressions for T10, T20, List A and Hundred with an exhausted wear counter, a wide and no-ball before the final legal delivery, replay-safe delivery tokens, and successful innings completion. Existing unrelated changes were preserved.
