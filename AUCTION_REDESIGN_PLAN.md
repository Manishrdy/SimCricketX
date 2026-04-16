# Auction Module — Redesign Plan

**Status**: Phases 1–2 shipped (2026-04-15). Phase 3 next.
**Date**: 2026-04-15
**Reference**: `AUCTION-REDESIGN`

## Phase Status
- [x] **Phase 1** — League/Season/SeasonTeam CRUD, empty Team creation, manager tokens, home nav repointed, legacy auction tables dropped. Migration `redesign_auction_phase1`.
- [x] **Phase 2** — Auction setup wizard end-to-end. Models: `Auction`, `AuctionCategory`, `AuctionPlayer`. Migration `auction_setup_phase2`. Routes: `/seasons/<id>/auction` hub + config + category CRUD/reorder/randomize + player curation (two-pane master+user pool, search+filter, bulk add) + finalize/reopen with server-side readiness validation. Legacy `add_auction` migration removed from precheck registry.
- [ ] Phase 3 — Socket.IO + chat + presence (team portal shell, organizer read-only)
- [ ] Phase 4 — Traditional live auction
- [ ] Phase 5 — Draft live auction
- [ ] Phase 6 — Roster sync to `Team.TeamProfile`, export
- [ ] Phase 7 — Fixtures/matches from a completed season

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
| status | enum | `upcoming` \| `live` \| `sold` \| `unsold` |
| sold_to_season_team_id | FK season_team | nullable |
| sold_price | bigint | nullable (0 for draft) |
| sold_in_round | int | nullable |
| lot_order | int | |

Constraint: exactly one of `master_player_id` / `user_player_id` non-null.

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
