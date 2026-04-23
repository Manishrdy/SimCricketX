# Auction Module D Checklist

Module D goal: remove manual curation grind with one-click auto-fill to minimum pool size.

## Scope

- [ ] Add `POST /seasons/<id>/auction/autofill-min-pool`.
- [ ] Auto-fill uses available master/user pool players only (no duplicates).
- [ ] Fill target is `teams_count * min_players_per_team`.
- [ ] Respect category caps while assigning players.
- [ ] Expose an "Auto-fill to minimum" action in setup UI.

## Guardrails

- [ ] Route available only when `AUCTION_SIMPLIFIED_FLOW=true`.
- [ ] Editable-status guard still applies (`auction_ready` lock blocks with 409 under strict mode).
- [ ] Route is idempotent once minimum requirement is satisfied.
- [ ] No schema/migration changes.

## Validation

- [ ] Minimum pool is reached when enough supply exists.
- [ ] Repeated calls do not overfill once target is met.
- [ ] Category cap constraints are enforced during fill.
- [ ] Disabled-mode call does not mutate setup data.
