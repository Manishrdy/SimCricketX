# Auction Module — Redesign Plan

**Status**: Phases 1–7 shipped + Phase 8 follow-up (history + audit trail) — 2026-04-16.
**Date**: 2026-04-16
**Reference**: `AUCTION-REDESIGN`

## Phase Status
- [x] **Phase 1** — League/Season/SeasonTeam CRUD, empty Team creation, manager tokens, home nav repointed, legacy auction tables dropped. Migration `redesign_auction_phase1`.
- [x] **Phase 2** — Auction setup wizard end-to-end. Models: `Auction`, `AuctionCategory`, `AuctionPlayer`. Migration `auction_setup_phase2`. Routes: `/seasons/<id>/auction` hub + config + category CRUD/reorder/randomize + player curation (two-pane master+user pool, search+filter, bulk add) + finalize/reopen with server-side readiness validation. Legacy `add_auction` migration removed from precheck registry.
- [x] **Phase 3** — Socket.IO `/auction` namespace, presence map, flat per-auction chat with organizer moderation; team portal + organizer read-only console.
- [x] **Phase 4** — Traditional live auction end-to-end. Migration `auction_live_phase4` adds `live_player_id` / `lot_ends_at` / `lot_paused_remaining_ms` to `auctions`. Pure helpers in `routes/auction_engine.py`; in-memory bid state + server-authoritative 1Hz timer in `routes/auction_runtime.py`. Organizer HTTP routes (start/pause/resume/complete/next-lot/next-round/force-sell/force-unsold/reverse-sale). `bid:place` socket handler with anti-sniping timer reset, budget invariant enforced server-side, reauction round price reduction. Bid history NOT persisted in MVP.
- [x] **Phase 5** — Draft live auction. Migration `auction_draft_phase5` adds the `draft_picks` table. `DraftPick` model in `database/models.py`. Draft helpers in `routes/auction_engine.py` (`snake_team_order`, `generate_round_picks`, `owed_carryovers`, `validate_pick`, `apply_draft_pick`/`apply_missed_pick`). Runtime extended in `routes/auction_runtime.py` — round generation + auto-advance + per-pick timer (shares `lot_ends_at`) + `pick:turn`/`pick:submitted`/`pick:missed`/`round:advance` emits; missed pick in round N creates a carryover pick in round N+1 that's inserted before the team's regular slot in snake order. `pick:submit` socket handler in `routes/auction_realtime.py`. Portal + live templates render the draft pick card conditional on `season.auction_mode`. Organizer controls in draft mode: start / pause / resume / complete only (no force-sell / reverse / next-lot / next-round — draft flow is fully automatic).
- [x] **Phase 6** — Roster sync + export. New module `routes/auction_sync.py` with pure `sync_season_rosters()` and `export_rosters_json/csv()`. `complete_auction()` now auto-runs the sync: for each `SeasonTeam` it copies sold `AuctionPlayer` rows into the team's `TeamProfile` (format = `season.format`), applies a captain/WK heuristic (prefers flagged players; falls back to promoting the most fielding-capable to WK and the highest combined-rating non-WK to captain), and flips `Team.is_draft=False` only when the roster passes the publish rules (11–25 players, ≥1 WK, ≥5 Bowlers/All-rounders, captain + WK designated). Manual re-sync route `POST /seasons/<id>/auction/sync-rosters` for post-edit reruns. Idempotent: wipes and repopulates the target `TeamProfile` on each run. Exports at `GET .../export.csv` and `GET .../export.json`. `auction_live` renders a Completed summary card (per-team roster + spend + unsold list + download buttons) when `status == auction_done`; portal + live pages auto-reload 2s after `status:update { auction_done }` so users see the final state without a manual refresh.
- [x] **Phase 7** — Tournament bootstrap from a completed season. `POST /seasons/<id>/auction/create-tournament` in `routes/auction_routes.py` invokes the existing `TournamentEngine.create_tournament()` with `team_ids` from the season's `SeasonTeam` rows and `format_type = season.format`. Readiness gate: rejects the request if any team is still `is_draft` (i.e., roster didn't pass publish rules). Organizer picks mode (round-robin / knockout / IPL-style / …) and playoff team count in a form on the Completed summary card. Each click creates a new Tournament row, so organizers may spawn multiple formats from the same season. No schema change — reuses existing `Tournament` / `TournamentTeam` / `TournamentFixture` tables. No new migration.
- [x] **Phase 8.1** (follow-up) — Per-sid socket rate limits in `routes/auction_realtime.py`. Sliding-window limiter (`_rate_allow(sid, event_name)`) enforces `bid:place` ≤ 5/s, `pick:submit` ≤ 2/s, `chat:send` ≤ 4/2s per connection. Over-limit bids/picks reject with `reason: "rate-limited"`; over-limit chat drops silently. State is per-sid and cleaned up on disconnect via `_rate_drop_sid(sid)` called from `_presence_drop_sid`.
- [x] **Phase 8** (follow-up) — Persisted bid history + auction audit trail. New models `AuctionBid` + `AuctionAuditLog` in `database/models.py`. Migration `auction_history_phase8` creates both tables. `routes/auction_runtime.py` gains `_log_audit()` + `_record_bid()` helpers; every runtime entry point (`start` / `pause` / `resume` / `complete` / `open_next_lot` / `next_round` / `force_sell` / `force_unsold` / `reverse_last_sale` / draft `submit_pick` / `_draft_timeout` / round advance / auto-sell + auto-unsold from `_expire_lot`) records an audit row with `actor_type ∈ {organizer, team, system}` and a JSON payload. Organizer HTTP routes thread `actor_label=current_user.id` through so every operator action is attributable. `place_bid` persists every accepted bid (rejects stay in memory). `sync-rosters` and `create-tournament` also log audit rows. Read-back: `GET /seasons/<id>/auction/history.json?limit=N` returns the newest bids + audit events; live console has a new History card (Audit / Bids tabs, refresh button, collapsible timeline).

## Post-planning changes applied during Phases 1–2
- Dropped `Season.start_at` and `Season.timezone` (model + migration + UI). Auction start is strictly manual.
- Bid-increment tiers replaced with a single flat `Auction.bid_increment` (BigInteger). Team budgets are the implicit ceiling. Legacy `bid_increment_tiers` column left orphaned in older DBs.
- Per-team category quota reinterpreted as pool-size cap: `AuctionCategory.max_per_team` renamed to `AuctionCategory.max_players` (default 15, nullable = no cap). Enforced at add/edit/move.
- Currency placeholders standardised on USD `$` across all UI copy.
- Legacy `migrations/add_auction.py` deleted; superseded by `redesign_auction_phase1`.

---

## Core Concept

The auction is no longer a standalone standalone feature. It becomes a child of a **Season**, which is a run of a **League**. Teams are created empty under a Season; the auction's job is to fill those teams' rosters. Once the auction completes, the teams are ready to play that season's matches.

```
League (IPL)
  └── Season (IPL 2026)
        ├── SeasonTeam A → existing Team row (empty)
        ├── SeasonTeam B → existing Team row (empty)
        ├── SeasonTeam … (empty)
        └── Auction (1:1 with Season)
              ├── Categories (Platinum, Gold, …)
              ├── AuctionPlayer rows (from master + user pool)
              ├── Bids / DraftPicks
              └── Chat + AuditLog
```

Empty `Team` rows are created at season-team creation and filled via the auction. Existing `TeamProfile` (T20/ListA) split is preserved.

---

## Locked Decisions

| # | Decision |
|---|---|
| Hierarchy | League → Season → SeasonTeam + Auction |
| Reuse Team model | Yes — auction fills existing `Team` + `TeamProfile` |
| Manager auth | Token-only, one token per team (v1) |
| Organizer role | Neutral referee, no team participation, audit-logged |
| Reverse purchase | Organizer-only |
| Category order | Organizer picks manual or randomize |
| Skip player | Not supported — every player is sold or unsold |
| Draft tie-break | First-to-server wins (row-level lock on pick) |
| Draft missed-pick | Missed in R1 → in R2 pick one from R1's category + one from R2's category |
| Draft order | Snake (1→N, then N→1, then 1→N…) |
| Auction start | Manual (scheduled start stored as advisory only) |
| Per-player timer | Default 20s traditional, 30s draft; user-overridable |
| Per-player base price | Category default; per-player override inline |
| Chat moderation | Organizer has full control — delete any message, wipe chat, reset |
| Old auction tables | Drop entirely (unused in prod) |
| Realtime | Flask-SocketIO (WebSocket). Redis adapter later for multi-worker |

---

## Data Model

### League
| Field | Type | Notes |
|---|---|---|
| id | int, PK | |
| user_id | FK users | owner |
| name | str(200) | "IPL" |
| short_code | str(10) | nullable |
| frequency | enum | `one_time` \| `recurring` |
| created_at | ts | |

### Season
| Field | Type | Notes |
|---|---|---|
| id | int, PK | |
| league_id | FK league | |
| name | str(200) | "IPL 2026" |
| format | enum | `T20` \| `ListA` |
| auction_mode | enum | `traditional` \| `draft` |
| status | enum | `setup` \| `teams_ready` \| `auction_ready` \| `auction_live` \| `auction_paused` \| `auction_done` \| `archived` |
| created_at / updated_at | ts | |

### SeasonTeam
| Field | Type | Notes |
|---|---|---|
| id | int, PK | |
| season_id | FK season | |
| team_id | FK teams | existing Team row (empty roster at creation) |
| display_name | str(200) | |
| access_token | uuid str(36), UNIQUE | |
| custom_budget | bigint | nullable, custom budget mode |
| purse_remaining | bigint | |
| players_bought | int | default 0 |

Unique: `(season_id, display_name)`.

### Auction
| Field | Type | Notes |
|---|---|---|
| id | int, PK | |
| season_id | FK season, UNIQUE | |
| budget_mode | enum | `uniform` \| `custom` |
| uniform_budget | bigint | |
| bid_increment | bigint | flat; 0 = any strictly higher bid wins |
| min_players_per_team | int | default 12 |
| max_players_per_team | int | default 25 |
| per_player_timer_seconds | int | default 20 (traditional) |
| draft_pick_timer_seconds | int | default 30 (draft) |
| category_order_mode | enum | `manual` \| `random` |
| category_order | json | frozen `[cat_id, …]` on finalize |
| reauction_rounds | int | default 0 |
| reauction_price_reduction_pct | int | default 0 |
| current_round | int | default 1 |
| started_at / ended_at | ts | nullable |

Auction mode (`traditional` \| `draft`) lives on `Season.auction_mode` (set at season create time) and is not denormalised onto `Auction`.

### AuctionCategory
| Field | Type | Notes |
|---|---|---|
| id | int, PK | |
| auction_id | FK auction | |
| name | str(100) | |
| display_order | int | |
| default_base_price | bigint | nullable (draft) |
| max_players | int | default 15; pool-size cap (null = no cap) |

Unique: `(auction_id, name)`.

### AuctionPlayer
| Field | Type | Notes |
|---|---|---|
| id | int, PK | |
| auction_id | FK auction | |
| category_id | FK auction_category | |
| master_player_id | FK master_players | nullable |
| user_player_id | FK user_players | nullable |
| name | str(100) | snapshot |
| role | enum | Batsman/Bowler/AllRounder/Wicketkeeper |
| batting/bowling/fielding_rating | int | snapshot |
| batting_hand / bowling_type / bowling_hand | str | snapshot |
| base_price_override | bigint | nullable |
| lot_order | int | ordering within category |
| status | enum | `upcoming` \| `live` \| `sold` \| `unsold` (populated in Phase 4) |
| sold_to_season_team_id | FK season_team | nullable (Phase 4) |
| sold_price | bigint | nullable (0 for draft) (Phase 4) |
| sold_in_round | int | nullable (Phase 4) |

Constraint: exactly one of `master_player_id` / `user_player_id` non-null (enforced via `@validates`).

### AuctionBid (traditional only)
| Field | Type | Notes |
|---|---|---|
| id | int, PK | |
| auction_player_id | FK | |
| season_team_id | FK | |
| amount | bigint | |
| round | int | |
| created_at | ts | |

### DraftPick (draft only)
| Field | Type | Notes |
|---|---|---|
| id | int, PK | |
| auction_id | FK | |
| round | int | |
| pick_order_in_round | int | snake order materialized |
| season_team_id | FK | |
| auction_player_id | FK | nullable if missed |
| category_id | FK | |
| is_carryover | bool | |
| carryover_from_round | int | nullable |
| picked_at | ts | nullable |
| status | enum | `pending` \| `picked` \| `missed` |

### ChatMessage
| Field | Type | Notes |
|---|---|---|
| id | int, PK | |
| auction_id | FK | |
| sender_type | enum | `organizer` \| `team` |
| season_team_id | FK | nullable (organizer) |
| body | text(500) | |
| created_at | ts | indexed |

### AuditLog
| Field | Type | Notes |
|---|---|---|
| id | int, PK | |
| auction_id | FK | |
| action | str(50) | |
| payload | json | |
| created_at | ts | |

### TeamPresence (ephemeral)
In-memory dict or Redis key `presence:auction:{id}:team:{team_id}` → last heartbeat. TTL 30s. Updated by Socket.IO connect/disconnect and 10s heartbeat.

---

## Module Split

1. **league_module** — League, Season, SeasonTeam CRUD. Creates empty Team rows. Generates manager access tokens.
2. **auction_setup_module** — Auction config, categories, player curation from pool, base prices, order randomization.
3. **auction_runtime_module** *(traditional)* — Player state machine, bid validation, timer, auto-sell, reverse-sale, reauction rounds.
4. **draft_runtime_module** *(draft)* — Snake-order generator, pick submission with row-lock, timeout→carryover, missed-pick catch-up rules.
5. **team_portal_module** — Token-gated SPA at `/s/<season_token>/team/<team_token>`. Lot/pick panel, roster, budget, preferences, other-team overview, chat, presence.
6. **realtime_module** — Flask-SocketIO namespace `/auction`, rooms per `auction_id`. Handshake validates token. Events: bid, sold, pick, chat, presence, state.
7. **organizer_console_module** — Organizer dashboard, controls (start/pause/reverse/skip/reauction/broadcast/wipe chat), audit log view, summary + export.
8. **integration_module** — On `auction_done`: sync `AuctionPlayer.sold_to_season_team_id` rosters into `Team.TeamProfile`. Stub "create fixtures" for later.

---

## Realtime Event Catalog (Socket.IO)

**Namespace**: `/auction` · **Room**: `auction:{id}`

**Server → Client**
- `state:update` — full snapshot
- `player:open` — new player live
- `bid:new` — traditional bid accepted
- `player:sold` / `player:unsold` / `player:reversed`
- `pick:turn` — draft team's turn
- `pick:submitted` / `pick:missed`
- `round:advance`
- `chat:new` / `chat:deleted` / `chat:wiped`
- `presence:update`
- `timer:tick` (throttled, 1Hz)

**Client → Server**
- `bid:place` (traditional)
- `pick:submit` (draft)
- `chat:send`
- `heartbeat`

All server-authoritative. Client math never trusted.

---

## Budget Invariant (Traditional)

A team's max valid bid is:
```
max_bid = purse_remaining - (mandatory_slots_left × min_future_base_price)
where mandatory_slots_left = max(0, min_players_per_team - players_bought - 1)
```
Enforced server-side on every bid. Draft mode: constraint is just `players_bought < max_players_per_team`.

---

## Phased Build Order

| Phase | Scope | Demo outcome |
|---|---|---|
| 1 | Module 1: League/Season/SeasonTeam CRUD + empty Team creation + tokens. Home nav → `/leagues`. Drop old auction tables. | Create league, season, empty teams, copy portal links. |
| 2 | Module 2: auction setup wizard (budget → categories → players → order → review). | Fully configured auction awaiting start. |
| 3 | Module 6 + Module 5 shell + Module 7 read-only. Connection, chat, presence. | Managers connect, chat, see each other online. |
| 4 | Module 3: traditional live auction end-to-end incl. reauction rounds. | Run a traditional auction to completion. |
| 5 | Module 4: draft mode with snake + carryover. | Run a draft to completion. |
| 6 | Module 8: roster sync to `Team.TeamProfile`, export. | Completed auction populates playable teams. |
| 7 *(later)* | Fixtures/matches from a season. | Season becomes a playable tournament. |

Each phase is shippable in isolation.

---

## Migration Notes

- **Drop** tables: `auction_events`, `auction_categories` (old), `auction_teams`, `auction_players` (old), `auction_bids` (old), `auction_player_categories`.
- **Add** tables: `leagues`, `seasons`, `season_teams`, `auctions`, `auction_categories`, `auction_players`, `auction_bids`, `draft_picks`, `auction_chat_messages`, `auction_audit_logs`.
- Add nullable `season_id` FK on `teams` (a team optionally belongs to a season).
- Migration file runs at app startup (idempotent), following existing pattern in `migrations/`.

---

## Open Items Deferred to Later Phases

- Multi-worker scaling → Redis adapter for Socket.IO
- Scheduled auto-start (APScheduler)
- Per-manager tokens (currently one-per-team)
- Cross-season player valuation history
- Rate-limiting infrastructure on bid endpoint
- Fixture generation / match integration

---

**Resume by saying**: "continue AUCTION-REDESIGN phase N".
