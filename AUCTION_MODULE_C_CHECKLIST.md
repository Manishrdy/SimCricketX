# Auction Module C Checklist

Module C goal: reduce setup friction with a one-click starter setup action.

## Scope

- [ ] Add `POST /seasons/<id>/auction/quickstart` for simplified flow mode.
- [ ] Button visible on setup page when simplified flow is enabled and setup is editable.
- [ ] Quickstart creates starter categories if missing.
- [ ] Quickstart fills obvious missing defaults (uniform budget and missing traditional category base prices).

## Guardrails

- [ ] Feature is blocked when `AUCTION_SIMPLIFIED_FLOW=false`.
- [ ] Feature obeys editable-status guard (`auction_ready` remains locked under strict mode).
- [ ] Operation is idempotent (no duplicate categories on repeated clicks).
- [ ] No schema or migration changes.

## Validation

- [ ] Starter categories created with expected order and base prices.
- [ ] Re-running quickstart does not create duplicates.
- [ ] Disabled-mode call performs no changes.
- [ ] Locked finalized setup returns HTTP 409.
