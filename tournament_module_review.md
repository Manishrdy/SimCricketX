# Tournament Module Review

This document summarizes potential issues discovered in the tournament module across engine logic, route handling, data integrity, and UX.

## Scope
- `engine/tournament_engine.py`
- `app.py` (tournament routes + match save flow)
- `database/models.py`
- `match_archiver.py`
- Tournament templates (rendering behavior in `app.py`)

## Findings (highlights)
- Placeholder team IDs (BYE/TBD) are hard-coded and can collide with real teams.
- Tournament ownership and fixture/tournament IDs are not consistently validated at the API boundary.
- Knockout and playoff progression can proceed with missing winners.
- Standings updates are not idempotent and may double count in some flows.
- Resimulation only resets league stats; knockout fixtures and downstream fixtures are not fully reset.
- Standings ordering in the UI differs from engine tie-breakers.

See the interview response for the full line-by-line analysis.
