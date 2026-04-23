# Auction Module E Checklist

Module E goal: provide one-click auto-prepare that chains safe setup automation and reports remaining blockers.

## Scope

- [ ] Add `POST /seasons/<id>/auction/auto-prepare`.
- [ ] Auto-prepare runs starter setup and auto-fill in one action.
- [ ] Setup page shows a single primary "Auto-prepare setup" CTA.
- [ ] After auto-prepare, user gets clear readiness feedback for finalize.

## Guardrails

- [ ] Available only when `AUCTION_SIMPLIFIED_FLOW=true`.
- [ ] Strict editable guard applies (`auction_ready` lock still blocks with 409).
- [ ] Action is idempotent when setup is already sufficiently prepared.
- [ ] No schema/migration changes.

## Validation

- [ ] Happy path reaches finalize-ready state with adequate pool supply.
- [ ] Partial-fill behavior is safe when supply is insufficient.
- [ ] Disabled-mode call does not mutate setup data.
- [ ] Repeated calls do not duplicate starter categories or overfill pool.
