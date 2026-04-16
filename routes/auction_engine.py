"""
AUCTION-REDESIGN Phase 4 — Pure auction logic.

Stateless helpers shared by the live runtime and the organizer HTTP routes.
No Flask, no socket, no module-level state — just functions over models so
they remain testable in isolation.
"""

import json


# ── Lot ordering ──────────────────────────────────────────────────────────────

def _category_order_index(auction, cat_id):
    """Return the position of `cat_id` within the frozen `auction.category_order`.
    Unknown categories are pushed to the end so the sort is total."""
    try:
        order = json.loads(auction.category_order or "[]")
    except (TypeError, ValueError):
        order = []
    try:
        return order.index(int(cat_id))
    except (ValueError, TypeError):
        return 10**9


def _lot_sort_key(auction, ap, cat):
    return (
        _category_order_index(auction, cat.id),
        ap.lot_order if ap.lot_order is not None else 10**9,
        (ap.name or "").lower(),
        ap.id,
    )


def upcoming_players_sorted(db, auction, DBAuctionPlayer, DBAuctionCategory):
    """All `upcoming` players in this auction, ordered by category_order then lot_order."""
    rows = (
        db.session.query(DBAuctionPlayer, DBAuctionCategory)
        .join(DBAuctionCategory, DBAuctionPlayer.category_id == DBAuctionCategory.id)
        .filter(DBAuctionPlayer.auction_id == auction.id,
                DBAuctionPlayer.status == "upcoming")
        .all()
    )
    rows.sort(key=lambda r: _lot_sort_key(auction, r[0], r[1]))
    return rows


def next_upcoming_player(db, auction, DBAuctionPlayer, DBAuctionCategory):
    rows = upcoming_players_sorted(db, auction, DBAuctionPlayer, DBAuctionCategory)
    if not rows:
        return None, None
    return rows[0]


# ── Pricing ───────────────────────────────────────────────────────────────────

def effective_base_price(player, category):
    if player.base_price_override is not None:
        return int(player.base_price_override)
    return int(category.default_base_price or 0)


def min_next_bid(auction, player, category, current_highest):
    """Smallest legal bid for the next bidder."""
    if current_highest is None or current_highest <= 0:
        return effective_base_price(player, category)
    inc = int(auction.bid_increment or 0)
    if inc == 0:
        return int(current_highest) + 1
    return int(current_highest) + inc


def min_future_base_price(db, auction, DBAuctionPlayer, DBAuctionCategory,
                          exclude_player_id=None):
    """Smallest effective base price among the upcoming players (post-current)."""
    rows = upcoming_players_sorted(db, auction, DBAuctionPlayer, DBAuctionCategory)
    prices = []
    for ap, cat in rows:
        if exclude_player_id is not None and ap.id == exclude_player_id:
            continue
        prices.append(effective_base_price(ap, cat))
    return min(prices) if prices else 0


def max_bid_for_team(auction, team, future_min_price):
    """Largest bid this team can place without starving its mandatory slots."""
    bought = int(team.players_bought or 0)
    purse = int(team.purse_remaining or 0)
    min_required = int(auction.min_players_per_team or 0)
    mandatory_left = max(0, min_required - bought - 1)
    cap = purse - mandatory_left * int(future_min_price or 0)
    return max(0, cap)


# ── Bid validation ────────────────────────────────────────────────────────────

def validate_bid(auction, team, player, category, amount,
                 current_highest, current_bidder_id, future_min_price):
    """Return (ok: bool, reason: str). `reason` is short and enum-like."""
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return False, "invalid-amount"
    if amount <= 0:
        return False, "invalid-amount"
    if int(team.players_bought or 0) >= int(auction.max_players_per_team or 0):
        return False, "squad-full"
    if current_bidder_id is not None and current_bidder_id == team.id:
        return False, "already-highest"
    next_min = min_next_bid(auction, player, category, current_highest)
    if amount < next_min:
        return False, f"min-{next_min}"
    if amount > int(team.purse_remaining or 0):
        return False, "exceeds-purse"
    cap = max_bid_for_team(auction, team, future_min_price)
    if amount > cap:
        return False, f"max-{cap}"
    return True, "ok"


# ── State mutations ───────────────────────────────────────────────────────────

def apply_sale(db, auction, player, team, amount):
    """Apply a sale: mark sold, debit team. Caller commits."""
    amount = int(amount)
    player.status = "sold"
    player.sold_to_season_team_id = team.id
    player.sold_price = amount
    player.sold_in_round = int(auction.current_round or 1)
    team.purse_remaining = int(team.purse_remaining or 0) - amount
    team.players_bought = int(team.players_bought or 0) + 1


def apply_unsold(player):
    """Mark a player unsold for the current round. Caller commits."""
    player.status = "unsold"
    player.sold_to_season_team_id = None
    player.sold_price = None
    player.sold_in_round = None


def apply_reverse_sale(db, player, team):
    """Inverse of apply_sale. Returns the refunded amount. Player goes back to
    `live` so bidding can resume on the same lot. Caller commits."""
    refund = int(player.sold_price or 0)
    team.purse_remaining = int(team.purse_remaining or 0) + refund
    team.players_bought = max(0, int(team.players_bought or 0) - 1)
    player.status = "live"
    player.sold_to_season_team_id = None
    player.sold_price = None
    player.sold_in_round = None
    return refund


# ═════════════════════════════════════════════════════════════════════════════
#  Phase 5 — Draft mode helpers
# ═════════════════════════════════════════════════════════════════════════════

def category_id_for_round(auction, round_number):
    """Return the category_id for the given 1-based draft round, or None if
    the round is past the configured category_order."""
    try:
        order = json.loads(auction.category_order or "[]")
    except (TypeError, ValueError):
        return None
    idx = int(round_number) - 1
    if idx < 0 or idx >= len(order):
        return None
    return int(order[idx])


def total_draft_rounds(auction):
    """Number of regular draft rounds = number of categories in the frozen order."""
    try:
        order = json.loads(auction.category_order or "[]")
    except (TypeError, ValueError):
        return 0
    return len(order)


def snake_team_order(team_ids, round_number):
    """Return team_ids in snake order for the given 1-based round.
    Odd rounds use natural order; even rounds reverse."""
    ordered = list(team_ids)
    if int(round_number) % 2 == 0:
        ordered.reverse()
    return ordered


def owed_carryovers(db, auction, DBDraftPick):
    """Return a set of (season_team_id, category_id) pairs still owed a pick
    because a miss in that category hasn't been reconciled by a later
    successful carryover."""
    missed = (
        db.session.query(DBDraftPick)
        .filter(DBDraftPick.auction_id == auction.id,
                DBDraftPick.status == "missed")
        .all()
    )
    obligations = {(m.season_team_id, m.category_id) for m in missed}

    picked_carries = (
        db.session.query(DBDraftPick)
        .filter(DBDraftPick.auction_id == auction.id,
                DBDraftPick.is_carryover == True,   # noqa: E712
                DBDraftPick.status == "picked")
        .all()
    )
    discharged = {(p.season_team_id, p.category_id) for p in picked_carries}
    return obligations - discharged


def generate_round_picks(db, auction, round_number, teams, regular_category_id,
                         DBDraftPick):
    """Insert pending DraftPick rows for a new round in snake order.

    For each team, any outstanding carryover obligations are queued BEFORE
    that team's regular pick. Caller commits.
    Returns the number of picks created.
    """
    team_by_id = {t.id: t for t in teams}
    ordered_ids = snake_team_order([t.id for t in teams], round_number)

    owed = owed_carryovers(db, auction, DBDraftPick)

    # Group owed obligations by team so we can place them in snake order.
    owed_by_team = {}
    for (tid, cat_id) in owed:
        owed_by_team.setdefault(tid, []).append(cat_id)

    order = 1
    created = 0
    for tid in ordered_ids:
        team = team_by_id.get(tid)
        if team is None:
            continue
        # Carryover picks first (earlier missed categories catch up).
        for cat_id in sorted(owed_by_team.get(tid, [])):
            db.session.add(DBDraftPick(
                auction_id=auction.id,
                round=int(round_number),
                pick_order_in_round=order,
                season_team_id=tid,
                category_id=int(cat_id),
                is_carryover=True,
                # carryover_from_round is set to the most recent miss round for
                # display purposes only; queue_missed_pick() always records
                # the correct original miss via owed_carryovers.
                carryover_from_round=int(round_number) - 1,
                status="pending",
            ))
            order += 1
            created += 1
        # Regular round-N pick.
        if regular_category_id is not None:
            db.session.add(DBDraftPick(
                auction_id=auction.id,
                round=int(round_number),
                pick_order_in_round=order,
                season_team_id=tid,
                category_id=int(regular_category_id),
                is_carryover=False,
                carryover_from_round=None,
                status="pending",
            ))
            order += 1
            created += 1
    return created


def next_pending_pick(db, auction, DBDraftPick):
    """Return the next pending DraftPick to act on (lowest round, then lowest
    pick_order_in_round), or None if all picks are resolved."""
    return (
        db.session.query(DBDraftPick)
        .filter(DBDraftPick.auction_id == auction.id,
                DBDraftPick.status == "pending")
        .order_by(DBDraftPick.round.asc(),
                  DBDraftPick.pick_order_in_round.asc(),
                  DBDraftPick.id.asc())
        .first()
    )


def validate_pick(auction, team, pick, player, category):
    """Return (ok: bool, reason: str)."""
    if pick is None or pick.status != "pending":
        return False, "no-pick"
    if team is None or team.id != pick.season_team_id:
        return False, "not-your-turn"
    if int(team.players_bought or 0) >= int(auction.max_players_per_team or 0):
        return False, "squad-full"
    if player is None or player.status != "upcoming":
        return False, "player-unavailable"
    if category is None or int(player.category_id) != int(pick.category_id):
        return False, "wrong-category"
    return True, "ok"


def apply_draft_pick(db, auction, pick, player, team):
    """Apply a successful draft pick. Draft sales are $0. Caller commits."""
    from datetime import datetime as _dt
    player.status = "sold"
    player.sold_to_season_team_id = team.id
    player.sold_price = 0
    player.sold_in_round = int(pick.round)
    team.players_bought = int(team.players_bought or 0) + 1
    pick.auction_player_id = player.id
    pick.status = "picked"
    pick.picked_at = _dt.utcnow()


def apply_missed_pick(pick):
    """Mark a pick as missed. Carryover is generated when the next round
    starts — see owed_carryovers + generate_round_picks."""
    pick.status = "missed"
    pick.auction_player_id = None


def draft_upcoming_for_category(db, auction, category_id, DBAuctionPlayer):
    """Return upcoming auction players in the given category, sorted by
    lot_order then name. Used by the portal to populate the pick picker."""
    rows = (
        db.session.query(DBAuctionPlayer)
        .filter(DBAuctionPlayer.auction_id == auction.id,
                DBAuctionPlayer.category_id == int(category_id),
                DBAuctionPlayer.status == "upcoming")
        .all()
    )
    rows.sort(key=lambda p: (
        p.lot_order if p.lot_order is not None else 10**9,
        (p.name or "").lower(),
        p.id,
    ))
    return rows


def reset_unsold_for_reauction(db, auction, DBAuctionPlayer, DBAuctionCategory, reduction_pct):
    """Flip every `unsold` player in this auction back to `upcoming` and apply
    the configured price reduction. Returns the count touched. Caller commits."""
    pct = max(0, min(100, int(reduction_pct or 0)))
    factor = (100 - pct) / 100.0
    rows = (
        db.session.query(DBAuctionPlayer, DBAuctionCategory)
        .join(DBAuctionCategory, DBAuctionPlayer.category_id == DBAuctionCategory.id)
        .filter(DBAuctionPlayer.auction_id == auction.id,
                DBAuctionPlayer.status == "unsold")
        .all()
    )
    n = 0
    for ap, cat in rows:
        base = effective_base_price(ap, cat)
        if pct > 0:
            ap.base_price_override = max(0, int(round(base * factor)))
        ap.status = "upcoming"
        n += 1
    return n
