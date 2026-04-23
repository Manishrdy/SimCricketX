# Auction Module B Checklist

Module B goal: simplify the setup journey with a guided progress checklist, without changing auction rules.

## Scope

- [ ] Setup page shows a step-by-step progress tracker (Teams, Configuration, Categories, Player Pool, Finalize).
- [ ] Each step clearly shows `Complete` or `Pending`.
- [ ] A "Next best action" hint points to the first incomplete step.
- [ ] Changes are active only when `AUCTION_SIMPLIFIED_FLOW=true`.

## Guardrails

- [ ] Existing finalize validation rules are unchanged.
- [ ] Existing setup mutation routes and status transitions are unchanged.
- [ ] No schema/migration changes required.

## Validation

- [ ] Empty setup renders all progress steps as pending.
- [ ] Minimal valid setup renders all progress steps as complete.
- [ ] Module A lock tests continue to pass.
