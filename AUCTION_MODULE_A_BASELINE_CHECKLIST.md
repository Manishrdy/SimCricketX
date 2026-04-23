# Auction Module A Baseline Checklist

Module A goal: ship a safe feature-flagged lock for finalized setup state, with no behavior regression when disabled.

## Baseline (Flag Off)

- [ ] Organizer can edit auction configuration while `season.status == auction_ready`.
- [ ] Setup category/player mutation routes continue to work in `auction_ready`.
- [ ] Reopen flow still works and resets `auction.category_order` to `[]`.

## Strict Lock (Flag On: `AUCTION_SIMPLIFIED_FLOW=true`)

- [ ] Any setup mutation route rejects edits when `season.status == auction_ready` with HTTP `409`.
- [ ] Error message explains that setup must be reopened first.
- [ ] Reopen route remains available and successful from `auction_ready`.
- [ ] After reopen, `season.status == setup` and setup mutations work again.

## UI Expectations

- [ ] Setup page shows finalized lock message in `auction_ready`.
- [ ] Setup controls are disabled while finalized lock is active.
- [ ] `Reopen setup` action remains visible in finalized state.

## Rollout Safety

- [ ] Flag defaults to `false` unless explicitly enabled.
- [ ] Env override `AUCTION_SIMPLIFIED_FLOW` takes precedence over config file.
- [ ] Existing seasons and auctions load without migration requirements.
