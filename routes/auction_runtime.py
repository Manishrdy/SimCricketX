"""
AUCTION-REDESIGN Phase 4 — Live runtime for traditional auctions.

Owns the in-memory bid state and the server-authoritative per-lot timer.
HTTP organizer routes and the `bid:place` socket handler both call into
the functions exposed here so that the lot state has a single source of
truth in the process.

Bid history is intentionally NOT persisted in MVP — only the current
highest bid lives in process memory. On restart, mid-lot state resets to
the player's base price (DB still knows which lot was live so the
organizer can reopen it cleanly).
"""

import threading
import time
from datetime import datetime, timedelta

from flask import current_app
from utils.exception_tracker import log_exception

from routes import auction_engine as engine


# ── Module-level dependency injection ────────────────────────────────────────

_DEPS = {}                    # set by register_auction_runtime
_STATE = {}                   # auction_id -> {"current_bid", "current_bidder_id", "current_bidder_name", "last_sold_player_id"}
_TIMERS = {}                  # auction_id -> True while a timer task is alive
_STATE_LOCK = threading.RLock()


def register_auction_runtime(app, *, socketio, db,
                             DBSeason, DBSeasonTeam, DBLeague,
                             DBAuction, DBAuctionCategory, DBAuctionPlayer,
                             DBDraftPick=None,
                             DBTeam=None, DBTeamProfile=None, DBPlayer=None,
                             DBAuctionBid=None, DBAuctionAuditLog=None):
    _DEPS.update({
        "app": app,
        "socketio": socketio,
        "db": db,
        "DBSeason": DBSeason,
        "DBSeasonTeam": DBSeasonTeam,
        "DBLeague": DBLeague,
        "DBAuction": DBAuction,
        "DBAuctionCategory": DBAuctionCategory,
        "DBAuctionPlayer": DBAuctionPlayer,
        "DBDraftPick": DBDraftPick,
        "DBTeam": DBTeam,
        "DBTeamProfile": DBTeamProfile,
        "DBPlayer": DBPlayer,
        "DBAuctionBid": DBAuctionBid,
        "DBAuctionAuditLog": DBAuctionAuditLog,
    })
    app.logger.info("[auction_runtime] registered")


# ── Audit helpers (Phase 8) ──────────────────────────────────────────────────

def _log_audit(auction, action, payload=None, *,
               actor_type="system", actor_label=None, commit=True):
    """Insert one AuctionAuditLog row. Best-effort — failures are logged but
    never abort the operation that triggered the audit event."""
    DBAuctionAuditLog = _DEPS.get("DBAuctionAuditLog")
    db = _DEPS.get("db")
    if DBAuctionAuditLog is None or db is None:
        return
    try:
        import json as _json
        row = DBAuctionAuditLog(
            auction_id=auction.id,
            action=action,
            actor_type=actor_type,
            actor_label=actor_label,
            payload=_json.dumps(payload) if payload else None,
        )
        db.session.add(row)
        if commit:
            db.session.commit()
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        log_exception(exc, source="auction_runtime.audit",
                      context={"auction_id": auction.id, "action": action})


def _record_bid(auction, player, team, amount, round_num, commit=True):
    """Persist one accepted bid. Best-effort."""
    DBAuctionBid = _DEPS.get("DBAuctionBid")
    db = _DEPS.get("db")
    if DBAuctionBid is None or db is None:
        return
    try:
        db.session.add(DBAuctionBid(
            auction_id=auction.id,
            auction_player_id=player.id if player else None,
            season_team_id=team.id if team else None,
            amount=int(amount),
            round=int(round_num or 1),
        ))
        if commit:
            db.session.commit()
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        log_exception(exc, source="auction_runtime.bid_record",
                      context={"auction_id": auction.id})


# ── State helpers ────────────────────────────────────────────────────────────

def _state(auction_id):
    """Return (and lazily create) the in-memory state for an auction."""
    aid = int(auction_id)
    with _STATE_LOCK:
        st = _STATE.get(aid)
        if st is None:
            st = {
                "current_bid": None,
                "current_bidder_id": None,
                "current_bidder_name": None,
                "last_sold_player_id": None,
            }
            _STATE[aid] = st
        return st


def _reset_lot_state(auction_id):
    with _STATE_LOCK:
        st = _state(auction_id)
        st["current_bid"] = None
        st["current_bidder_id"] = None
        st["current_bidder_name"] = None


def _room(auction_id):
    return f"auction:{int(auction_id)}"


def _emit(event, payload, auction_id):
    socketio = _DEPS.get("socketio")
    if socketio is None:
        return
    try:
        socketio.emit(event, payload, to=_room(auction_id), namespace="/auction")
    except Exception as exc:
        log_exception(exc, source="auction_runtime.emit", context={"event": event})


# ── Lot serialization ────────────────────────────────────────────────────────

def _serialize_lot(auction, player, category, ends_at):
    st = _state(auction.id)
    base = engine.effective_base_price(player, category)
    next_min = engine.min_next_bid(auction, player, category, st["current_bid"])
    return {
        "player": {
            "id": player.id,
            "name": player.name,
            "role": player.role,
            "category_id": category.id,
            "category_name": category.name,
            "base_price": base,
            "batting_rating": player.batting_rating,
            "bowling_rating": player.bowling_rating,
            "fielding_rating": player.fielding_rating,
            "batting_hand": player.batting_hand,
            "bowling_type": player.bowling_type,
            "bowling_hand": player.bowling_hand,
        },
        "current_bid": st["current_bid"],
        "current_bidder_id": st["current_bidder_id"],
        "current_bidder_name": st["current_bidder_name"],
        "min_next_bid": next_min,
        "bid_increment": int(auction.bid_increment or 0),
        "ends_at": ends_at.isoformat() if ends_at else None,
        "round": int(auction.current_round or 1),
    }


def lot_snapshot(auction):
    """Return the JSON-serializable snapshot of the live lot, or None."""
    if not auction.live_player_id:
        return None
    DBAuctionPlayer = _DEPS["DBAuctionPlayer"]
    DBAuctionCategory = _DEPS["DBAuctionCategory"]
    player = DBAuctionPlayer.query.get(auction.live_player_id)
    if player is None:
        return None
    cat = DBAuctionCategory.query.get(player.category_id)
    if cat is None:
        return None
    return _serialize_lot(auction, player, cat, auction.lot_ends_at)


# ── Status broadcast ─────────────────────────────────────────────────────────

def _emit_status(season, auction):
    _emit("status:update", {
        "status": season.status,
        "current_round": int(auction.current_round or 1),
        "reauction_rounds": int(auction.reauction_rounds or 0),
    }, auction.id)


def _emit_team_update(team, auction_id):
    _emit("team:update", {
        "team_id": team.id,
        "purse_remaining": int(team.purse_remaining or 0),
        "players_bought": int(team.players_bought or 0),
    }, auction_id)


# ── Timer task ───────────────────────────────────────────────────────────────

def _ensure_timer(auction_id):
    """Spawn the timer background task for this auction if not already running."""
    aid = int(auction_id)
    with _STATE_LOCK:
        if _TIMERS.get(aid):
            return
        _TIMERS[aid] = True
    socketio = _DEPS["socketio"]
    socketio.start_background_task(_timer_loop, aid)


def _timer_loop(auction_id):
    """1Hz tick: emit timer:tick or trigger expiry. Handles both traditional
    lots and draft picks based on season.auction_mode. Exits when the auction
    is no longer live (paused / done)."""
    app = _DEPS["app"]
    DBAuction = _DEPS["DBAuction"]
    DBSeason = _DEPS["DBSeason"]
    DBDraftPick = _DEPS.get("DBDraftPick")
    db = _DEPS["db"]
    try:
        while True:
            time.sleep(1.0)
            with app.app_context():
                auction = DBAuction.query.get(auction_id)
                if auction is None:
                    break
                season = DBSeason.query.get(auction.season_id)
                if season is None or season.status != "auction_live":
                    break
                is_draft = season.auction_mode == "draft"
                if auction.lot_ends_at is None:
                    # Idle between lots/picks — keep the loop alive at 1Hz.
                    continue
                now = datetime.utcnow()
                remaining = (auction.lot_ends_at - now).total_seconds()
                if remaining <= 0:
                    if is_draft:
                        _draft_timeout(auction, season)
                    else:
                        _expire_lot(auction)
                else:
                    if is_draft and DBDraftPick is not None:
                        pick = engine.next_pending_pick(db, auction, DBDraftPick)
                        if pick:
                            _emit("timer:tick", {
                                "pick_id": pick.id,
                                "seconds_remaining": int(round(remaining)),
                            }, auction.id)
                    elif auction.live_player_id:
                        _emit("timer:tick", {
                            "player_id": auction.live_player_id,
                            "seconds_remaining": int(round(remaining)),
                        }, auction.id)
    except Exception as exc:
        log_exception(exc, source="auction_runtime.timer", context={"auction_id": auction_id})
    finally:
        with _STATE_LOCK:
            _TIMERS.pop(int(auction_id), None)


def _expire_lot(auction):
    """Called from the timer thread when ends_at passes. Auto-sells to the
    current highest bidder, or marks unsold. Stops the timer (organizer must
    explicitly open the next player)."""
    db = _DEPS["db"]
    DBAuctionPlayer = _DEPS["DBAuctionPlayer"]
    DBAuctionCategory = _DEPS["DBAuctionCategory"]
    DBSeasonTeam = _DEPS["DBSeasonTeam"]

    with _STATE_LOCK:
        st = _state(auction.id)
        bidder_id = st["current_bidder_id"]
        amount = st["current_bid"]
        # Re-load fresh, hold lock through DB writes so a late bid can't race.
        player = DBAuctionPlayer.query.get(auction.live_player_id) if auction.live_player_id else None
        if player is None or player.status != "live":
            # Already resolved by some other path; just clear lot and exit.
            auction.live_player_id = None
            auction.lot_ends_at = None
            db.session.commit()
            return

        cat = DBAuctionCategory.query.get(player.category_id)
        if bidder_id and amount:
            team = DBSeasonTeam.query.get(bidder_id)
            if team is not None:
                engine.apply_sale(db, auction, player, team, amount)
                auction.live_player_id = None
                auction.lot_ends_at = None
                st["last_sold_player_id"] = player.id
                db.session.commit()
                _reset_lot_state(auction.id)
                _emit("lot:sold", {
                    "player_id": player.id,
                    "team_id": team.id,
                    "team_name": team.display_name,
                    "amount": int(amount),
                }, auction.id)
                _emit_team_update(team, auction.id)
                _log_audit(auction, "lot.sold",
                           {"player_id": player.id, "player_name": player.name,
                            "team_id": team.id, "team_name": team.display_name,
                            "amount": int(amount), "forced": False},
                           actor_type="system")
                return
        # No bids — unsold.
        engine.apply_unsold(player)
        auction.live_player_id = None
        auction.lot_ends_at = None
        db.session.commit()
        _reset_lot_state(auction.id)
        _emit("lot:unsold", {"player_id": player.id}, auction.id)
        _log_audit(auction, "lot.unsold",
                   {"player_id": player.id, "player_name": player.name, "forced": False},
                   actor_type="system")


# ── Public operations (called from HTTP routes & socket handlers) ────────────

class RuntimeError_(Exception):
    """Raised by runtime ops to signal a user-facing failure."""

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def start_auction(season, auction, *, actor_label=None):
    """Flip the season to auction_live and spawn the timer.

    Traditional: does NOT auto-open the first lot — the organizer must click
    'Open next player' for each lot.
    Draft: auto-generates round-1 pick slots and starts the first pick's
    timer immediately, since pick order is pre-determined by snake rules.
    """
    db = _DEPS["db"]
    if season.status not in ("auction_ready", "auction_paused"):
        raise RuntimeError_("not-ready", f"Auction is in '{season.status}' — cannot start.")
    season.status = "auction_live"
    if auction.started_at is None:
        auction.started_at = datetime.utcnow()
    db.session.commit()
    _log_audit(auction, "auction.start", {"mode": season.auction_mode},
               actor_type="organizer", actor_label=actor_label)
    _ensure_timer(auction.id)
    _emit_status(season, auction)

    if season.auction_mode == "draft":
        _ensure_draft_round_generated(auction, int(auction.current_round or 1))
        _advance_draft(season, auction, kickoff=True)


def pause_auction(season, auction, *, actor_label=None):
    db = _DEPS["db"]
    if season.status != "auction_live":
        raise RuntimeError_("not-live", "Auction is not live.")
    # Snapshot remaining time on the live lot if any.
    if auction.live_player_id and auction.lot_ends_at:
        remaining = (auction.lot_ends_at - datetime.utcnow()).total_seconds()
        auction.lot_paused_remaining_ms = max(0, int(remaining * 1000))
        auction.lot_ends_at = None
    season.status = "auction_paused"
    db.session.commit()
    _log_audit(auction, "auction.pause",
               {"paused_remaining_ms": auction.lot_paused_remaining_ms},
               actor_type="organizer", actor_label=actor_label)
    _emit_status(season, auction)


def resume_auction(season, auction, *, actor_label=None):
    db = _DEPS["db"]
    if season.status != "auction_paused":
        raise RuntimeError_("not-paused", "Auction is not paused.")
    # Restore remaining time on whatever action was in flight (lot or pick).
    if auction.lot_paused_remaining_ms is not None:
        ms = max(1000, int(auction.lot_paused_remaining_ms))
        auction.lot_ends_at = datetime.utcnow() + timedelta(milliseconds=ms)
    auction.lot_paused_remaining_ms = None
    season.status = "auction_live"
    db.session.commit()
    _log_audit(auction, "auction.resume", None,
               actor_type="organizer", actor_label=actor_label)
    _ensure_timer(auction.id)
    _emit_status(season, auction)
    # Re-broadcast the current snapshot so reconnected clients pick up the new ends_at.
    if season.auction_mode == "draft":
        snap = draft_snapshot(auction)
        if snap:
            _emit("pick:turn", snap, auction.id)
    else:
        snap = lot_snapshot(auction)
        if snap:
            _emit("lot:open", snap, auction.id)


def complete_auction(season, auction, *, actor_label=None):
    db = _DEPS["db"]
    DBAuctionPlayer = _DEPS["DBAuctionPlayer"]
    if season.status not in ("auction_live", "auction_paused"):
        raise RuntimeError_("not-live", "Auction has not started.")
    auction.live_player_id = None
    auction.lot_ends_at = None
    auction.lot_paused_remaining_ms = None
    auction.ended_at = datetime.utcnow()
    # Sweep any still-upcoming players into `unsold` so the DB reflects final state.
    upcoming = (DBAuctionPlayer.query
                .filter_by(auction_id=auction.id, status="upcoming")
                .all())
    for p in upcoming:
        engine.apply_unsold(p)
    season.status = "auction_done"
    db.session.commit()
    _reset_lot_state(auction.id)

    _log_audit(auction, "auction.complete",
               {"swept_upcoming": len(upcoming)},
               actor_type="organizer" if actor_label else "system",
               actor_label=actor_label)

    # Phase 6: roster sync — populate Team.TeamProfile from the final auction
    # state so each empty team becomes a playable squad. Non-fatal if the sync
    # fails; the auction itself is still complete and can be re-synced manually.
    try:
        sync_summary = run_roster_sync(season, auction)
        _emit("roster:synced", {"teams": sync_summary}, auction.id)
        _log_audit(auction, "roster.synced",
                   {"teams_synced": len(sync_summary),
                    "teams_ready": sum(1 for r in sync_summary if r.get("publish_ready"))},
                   actor_type="system")
    except Exception as exc:
        log_exception(exc, source="auction_runtime.roster_sync",
                      context={"auction_id": auction.id})

    _emit_status(season, auction)


def run_roster_sync(season, auction):
    """Invoke the sync module with the already-injected dependencies.
    Returns the per-team report list. Commits on success."""
    from routes import auction_sync
    db = _DEPS["db"]
    DBSeasonTeam = _DEPS["DBSeasonTeam"]
    DBTeam = _DEPS.get("DBTeam")
    DBTeamProfile = _DEPS.get("DBTeamProfile")
    DBPlayer = _DEPS.get("DBPlayer")
    DBAuctionPlayer = _DEPS["DBAuctionPlayer"]
    DBAuctionCategory = _DEPS["DBAuctionCategory"]
    if DBTeam is None or DBTeamProfile is None or DBPlayer is None:
        return []
    try:
        report = auction_sync.sync_season_rosters(
            db, season, auction,
            DBSeasonTeam=DBSeasonTeam, DBTeam=DBTeam,
            DBTeamProfile=DBTeamProfile, DBPlayer=DBPlayer,
            DBAuctionPlayer=DBAuctionPlayer, DBAuctionCategory=DBAuctionCategory,
        )
        db.session.commit()
        return report
    except Exception:
        db.session.rollback()
        raise


def open_next_lot(season, auction, *, actor_label=None):
    """Pull the next upcoming player and broadcast lot:open."""
    db = _DEPS["db"]
    DBAuctionPlayer = _DEPS["DBAuctionPlayer"]
    DBAuctionCategory = _DEPS["DBAuctionCategory"]
    if season.status != "auction_live":
        raise RuntimeError_("not-live", "Auction is not live.")
    if auction.live_player_id:
        raise RuntimeError_("lot-open", "A lot is already open.")
    player, category = engine.next_upcoming_player(db, auction, DBAuctionPlayer, DBAuctionCategory)
    if player is None:
        raise RuntimeError_("no-upcoming", "No upcoming players in this round.")
    timer = max(3, int(auction.per_player_timer_seconds or 20))
    ends_at = datetime.utcnow() + timedelta(seconds=timer)
    player.status = "live"
    auction.live_player_id = player.id
    auction.lot_ends_at = ends_at
    auction.lot_paused_remaining_ms = None
    db.session.commit()
    _reset_lot_state(auction.id)
    _ensure_timer(auction.id)
    _emit("lot:open", _serialize_lot(auction, player, category, ends_at), auction.id)
    _log_audit(auction, "lot.open",
               {"player_id": player.id, "player_name": player.name,
                "category": category.name if category else None,
                "base_price": engine.effective_base_price(player, category) if category else 0,
                "round": int(auction.current_round or 1)},
               actor_type="organizer" if actor_label else "system",
               actor_label=actor_label)
    return player


def next_round(season, auction, *, actor_label=None):
    """Advance to the next reauction round: flip unsold→upcoming and apply
    the configured price reduction. Errors if the cap is reached or no
    unsold players exist."""
    db = _DEPS["db"]
    DBAuctionPlayer = _DEPS["DBAuctionPlayer"]
    DBAuctionCategory = _DEPS["DBAuctionCategory"]
    if season.status != "auction_live":
        raise RuntimeError_("not-live", "Auction is not live.")
    if auction.live_player_id:
        raise RuntimeError_("lot-open", "Resolve the open lot before starting a new round.")
    cur = int(auction.current_round or 1)
    cap = 1 + int(auction.reauction_rounds or 0)
    if cur >= cap:
        raise RuntimeError_("rounds-exhausted", f"All {cap} round(s) have been used.")
    n = engine.reset_unsold_for_reauction(
        db, auction, DBAuctionPlayer, DBAuctionCategory,
        int(auction.reauction_price_reduction_pct or 0),
    )
    if n == 0:
        raise RuntimeError_("nothing-to-reauction", "No unsold players to re-auction.")
    auction.current_round = cur + 1
    db.session.commit()
    with _STATE_LOCK:
        _state(auction.id)["last_sold_player_id"] = None
    _log_audit(auction, "round.advance",
               {"to_round": cur + 1, "replayed": n,
                "price_reduction_pct": int(auction.reauction_price_reduction_pct or 0)},
               actor_type="organizer" if actor_label else "system",
               actor_label=actor_label)
    _emit_status(season, auction)
    return n


def force_sell(season, auction, *, actor_label=None):
    """Organizer override — immediately sell to the current highest bidder.
    Errors if there is no active bid."""
    db = _DEPS["db"]
    DBAuctionPlayer = _DEPS["DBAuctionPlayer"]
    DBSeasonTeam = _DEPS["DBSeasonTeam"]
    if season.status != "auction_live":
        raise RuntimeError_("not-live", "Auction is not live.")
    if not auction.live_player_id:
        raise RuntimeError_("no-lot", "No lot is currently live.")
    with _STATE_LOCK:
        st = _state(auction.id)
        bidder_id = st["current_bidder_id"]
        amount = st["current_bid"]
        if not bidder_id or not amount:
            raise RuntimeError_("no-bid", "No active bid to sell on.")
        player = DBAuctionPlayer.query.get(auction.live_player_id)
        team = DBSeasonTeam.query.get(bidder_id)
        if player is None or team is None:
            raise RuntimeError_("not-found", "Lot or team disappeared.")
        engine.apply_sale(db, auction, player, team, amount)
        auction.live_player_id = None
        auction.lot_ends_at = None
        st["last_sold_player_id"] = player.id
        db.session.commit()
        _reset_lot_state(auction.id)
    _emit("lot:sold", {
        "player_id": player.id,
        "team_id": team.id,
        "team_name": team.display_name,
        "amount": int(amount),
    }, auction.id)
    _emit_team_update(team, auction.id)
    _log_audit(auction, "lot.sold",
               {"player_id": player.id, "player_name": player.name,
                "team_id": team.id, "team_name": team.display_name,
                "amount": int(amount), "forced": True},
               actor_type="organizer" if actor_label else "system",
               actor_label=actor_label)


def force_unsold(season, auction, *, actor_label=None):
    """Organizer override — close the live lot as unsold regardless of bids."""
    db = _DEPS["db"]
    DBAuctionPlayer = _DEPS["DBAuctionPlayer"]
    if season.status != "auction_live":
        raise RuntimeError_("not-live", "Auction is not live.")
    if not auction.live_player_id:
        raise RuntimeError_("no-lot", "No lot is currently live.")
    with _STATE_LOCK:
        player = DBAuctionPlayer.query.get(auction.live_player_id)
        if player is None:
            raise RuntimeError_("not-found", "Lot disappeared.")
        engine.apply_unsold(player)
        auction.live_player_id = None
        auction.lot_ends_at = None
        db.session.commit()
        _reset_lot_state(auction.id)
    _emit("lot:unsold", {"player_id": player.id}, auction.id)
    _log_audit(auction, "lot.unsold",
               {"player_id": player.id, "player_name": player.name, "forced": True},
               actor_type="organizer" if actor_label else "system",
               actor_label=actor_label)


def reverse_last_sale(season, auction, *, actor_label=None):
    """Refund the team that bought the most recent lot (this round) and
    reopen that player for bidding. Only works when no other lot is live."""
    db = _DEPS["db"]
    DBAuctionPlayer = _DEPS["DBAuctionPlayer"]
    DBAuctionCategory = _DEPS["DBAuctionCategory"]
    DBSeasonTeam = _DEPS["DBSeasonTeam"]
    if season.status != "auction_live":
        raise RuntimeError_("not-live", "Auction is not live.")
    if auction.live_player_id:
        raise RuntimeError_("lot-open", "Close the current lot before reversing.")
    with _STATE_LOCK:
        st = _state(auction.id)
        last_id = st.get("last_sold_player_id")
        if not last_id:
            raise RuntimeError_("no-history", "No sale to reverse in this round.")
        player = DBAuctionPlayer.query.get(last_id)
        if player is None or player.status != "sold":
            raise RuntimeError_("not-found", "That sale is no longer reversible.")
        if int(player.sold_in_round or 0) != int(auction.current_round or 1):
            raise RuntimeError_("not-this-round", "Can only reverse a sale made in the current round.")
        team = DBSeasonTeam.query.get(player.sold_to_season_team_id)
        if team is None:
            raise RuntimeError_("not-found", "Buying team disappeared.")
        cat = DBAuctionCategory.query.get(player.category_id)
        refund = engine.apply_reverse_sale(db, player, team)
        # Reopen as the live lot so bidding can resume on the same player.
        timer = max(3, int(auction.per_player_timer_seconds or 20))
        ends_at = datetime.utcnow() + timedelta(seconds=timer)
        auction.live_player_id = player.id
        auction.lot_ends_at = ends_at
        st["current_bid"] = None
        st["current_bidder_id"] = None
        st["current_bidder_name"] = None
        st["last_sold_player_id"] = None
        db.session.commit()
    _emit_team_update(team, auction.id)
    _emit("lot:reversed", {
        "player_id": player.id,
        "restored_to_team_id": team.id,
        "refund": int(refund),
    }, auction.id)
    _log_audit(auction, "lot.reversed",
               {"player_id": player.id, "player_name": player.name,
                "team_id": team.id, "team_name": team.display_name,
                "refund": int(refund)},
               actor_type="organizer" if actor_label else "system",
               actor_label=actor_label)
    _ensure_timer(auction.id)
    _emit("lot:open", _serialize_lot(auction, player, cat, ends_at), auction.id)


# ── Bid placement ────────────────────────────────────────────────────────────

def place_bid(auction_id, team_id, amount):
    """Validate and accept a bid from a team. Returns (ok, reason). On
    success, broadcasts bid:new and resets the timer."""
    db = _DEPS["db"]
    DBAuction = _DEPS["DBAuction"]
    DBSeason = _DEPS["DBSeason"]
    DBSeasonTeam = _DEPS["DBSeasonTeam"]
    DBAuctionPlayer = _DEPS["DBAuctionPlayer"]
    DBAuctionCategory = _DEPS["DBAuctionCategory"]

    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return False, "invalid-amount"

    auction = DBAuction.query.get(int(auction_id))
    if auction is None:
        return False, "not-found"
    season = DBSeason.query.get(auction.season_id)
    if season is None or season.status != "auction_live":
        return False, "not-live"
    if not auction.live_player_id:
        return False, "no-lot"
    player = DBAuctionPlayer.query.get(auction.live_player_id)
    if player is None or player.status != "live":
        return False, "no-lot"
    category = DBAuctionCategory.query.get(player.category_id)
    if category is None:
        return False, "not-found"
    team = DBSeasonTeam.query.get(int(team_id))
    if team is None or team.season_id != season.id:
        return False, "not-found"

    with _STATE_LOCK:
        st = _state(auction.id)
        future_min = engine.min_future_base_price(
            db, auction, DBAuctionPlayer, DBAuctionCategory,
            exclude_player_id=player.id,
        )
        ok, reason = engine.validate_bid(
            auction, team, player, category, amount,
            current_highest=st["current_bid"],
            current_bidder_id=st["current_bidder_id"],
            future_min_price=future_min,
        )
        if not ok:
            return False, reason

        # Accept — reset timer per anti-sniping rule.
        st["current_bid"] = amount
        st["current_bidder_id"] = team.id
        st["current_bidder_name"] = team.display_name

        timer = max(3, int(auction.per_player_timer_seconds or 20))
        ends_at = datetime.utcnow() + timedelta(seconds=timer)
        auction.lot_ends_at = ends_at
        db.session.commit()

    next_min = engine.min_next_bid(auction, player, category, amount)
    _emit("bid:new", {
        "player_id": player.id,
        "team_id": team.id,
        "team_name": team.display_name,
        "amount": int(amount),
        "min_next_bid": int(next_min),
        "ends_at": ends_at.isoformat(),
    }, auction.id)
    _record_bid(auction, player, team, amount, int(auction.current_round or 1))
    return True, "ok"


# ═════════════════════════════════════════════════════════════════════════════
#  Phase 5 — Draft mode runtime
# ═════════════════════════════════════════════════════════════════════════════

def _season_teams_ordered(auction):
    """Teams for this auction's season, ordered deterministically by id so
    snake_team_order() gives a stable natural sequence."""
    db = _DEPS["db"]
    DBSeasonTeam = _DEPS["DBSeasonTeam"]
    return (db.session.query(DBSeasonTeam)
              .filter(DBSeasonTeam.season_id == auction.season_id)
              .order_by(DBSeasonTeam.id.asc())
              .all())


def _ensure_draft_round_generated(auction, round_number):
    """Materialize DraftPick rows for the given round if none exist yet.
    Idempotent — safe to call on resume / reconnect."""
    db = _DEPS["db"]
    DBDraftPick = _DEPS["DBDraftPick"]
    existing = (db.session.query(DBDraftPick)
                .filter_by(auction_id=auction.id, round=int(round_number))
                .first())
    if existing is not None:
        return 0
    category_id = engine.category_id_for_round(auction, round_number)
    if category_id is None:
        return 0
    teams = _season_teams_ordered(auction)
    created = engine.generate_round_picks(
        db, auction, round_number, teams, category_id, DBDraftPick,
    )
    db.session.commit()
    return created


def _serialize_pick(auction, pick, team, category, upcoming_players, ends_at):
    players_payload = [{
        "id": p.id,
        "name": p.name,
        "role": p.role,
        "batting_rating": p.batting_rating,
        "bowling_rating": p.bowling_rating,
        "fielding_rating": p.fielding_rating,
        "batting_hand": p.batting_hand,
        "bowling_type": p.bowling_type,
        "bowling_hand": p.bowling_hand,
    } for p in upcoming_players]
    return {
        "pick_id": pick.id,
        "round": int(pick.round),
        "pick_order_in_round": int(pick.pick_order_in_round),
        "team_id": team.id if team else None,
        "team_name": team.display_name if team else None,
        "category_id": category.id if category else None,
        "category_name": category.name if category else None,
        "is_carryover": bool(pick.is_carryover),
        "carryover_from_round": pick.carryover_from_round,
        "ends_at": ends_at.isoformat() if ends_at else None,
        "players": players_payload,
        "total_rounds": engine.total_draft_rounds(auction),
    }


def draft_snapshot(auction):
    """Serialize the current pending pick, or None if no pick is pending."""
    db = _DEPS["db"]
    DBDraftPick = _DEPS["DBDraftPick"]
    DBSeasonTeam = _DEPS["DBSeasonTeam"]
    DBAuctionPlayer = _DEPS["DBAuctionPlayer"]
    DBAuctionCategory = _DEPS["DBAuctionCategory"]
    if DBDraftPick is None:
        return None
    pick = engine.next_pending_pick(db, auction, DBDraftPick)
    if pick is None:
        return None
    team = DBSeasonTeam.query.get(pick.season_team_id)
    category = DBAuctionCategory.query.get(pick.category_id)
    upcoming = engine.draft_upcoming_for_category(db, auction, pick.category_id, DBAuctionPlayer)
    return _serialize_pick(auction, pick, team, category, upcoming, auction.lot_ends_at)


def _start_pick_timer(auction, seconds=None):
    """Reset the lot_ends_at clock for the current pending pick."""
    db = _DEPS["db"]
    timer = seconds if seconds is not None else int(auction.draft_pick_timer_seconds or 30)
    timer = max(3, int(timer))
    auction.lot_ends_at = datetime.utcnow() + timedelta(seconds=timer)
    auction.lot_paused_remaining_ms = None
    db.session.commit()
    return auction.lot_ends_at


def _emit_pick_turn(auction, pick):
    """Broadcast the current pick. Safe to call on reconnects / resumes too."""
    DBSeasonTeam = _DEPS["DBSeasonTeam"]
    DBAuctionCategory = _DEPS["DBAuctionCategory"]
    DBAuctionPlayer = _DEPS["DBAuctionPlayer"]
    db = _DEPS["db"]
    team = DBSeasonTeam.query.get(pick.season_team_id)
    category = DBAuctionCategory.query.get(pick.category_id)
    upcoming = engine.draft_upcoming_for_category(db, auction, pick.category_id, DBAuctionPlayer)
    payload = _serialize_pick(auction, pick, team, category, upcoming, auction.lot_ends_at)
    _emit("pick:turn", payload, auction.id)


def _advance_draft(season, auction, kickoff=False):
    """Drive the draft forward: pick next pending slot; if none, advance
    round; if all rounds exhausted, complete. `kickoff=True` is used from
    start_auction so we still emit pick:turn for the very first pick."""
    db = _DEPS["db"]
    DBDraftPick = _DEPS["DBDraftPick"]

    pick = engine.next_pending_pick(db, auction, DBDraftPick)
    if pick is None:
        # Advance round.
        next_round = int(auction.current_round or 1) + 1
        total = engine.total_draft_rounds(auction)
        if next_round > total:
            complete_auction(season, auction)
            return
        auction.current_round = next_round
        db.session.commit()
        _ensure_draft_round_generated(auction, next_round)
        _emit_status(season, auction)
        _emit("round:advance", {"round": next_round}, auction.id)
        _log_audit(auction, "round.advance",
                   {"to_round": next_round}, actor_type="system")
        pick = engine.next_pending_pick(db, auction, DBDraftPick)
        if pick is None:
            # Empty round (no categories beyond this), safety: complete.
            complete_auction(season, auction)
            return

    _start_pick_timer(auction)
    _emit_pick_turn(auction, pick)
    _ = kickoff  # currently no divergent behavior; kept for intent clarity


def _draft_timeout(auction, season):
    """Timer expired on the current pending pick. Mark it missed and advance."""
    db = _DEPS["db"]
    DBDraftPick = _DEPS["DBDraftPick"]
    with _STATE_LOCK:
        pick = engine.next_pending_pick(db, auction, DBDraftPick)
        if pick is None:
            # Nothing to miss — let the advance loop sort it out.
            auction.lot_ends_at = None
            db.session.commit()
            _advance_draft(season, auction)
            return
        engine.apply_missed_pick(pick)
        auction.lot_ends_at = None
        db.session.commit()
    _emit("pick:missed", {
        "pick_id": pick.id,
        "round": int(pick.round),
        "team_id": pick.season_team_id,
        "category_id": pick.category_id,
        "is_carryover": bool(pick.is_carryover),
    }, auction.id)
    _log_audit(auction, "pick.missed",
               {"pick_id": pick.id, "round": int(pick.round),
                "team_id": pick.season_team_id,
                "category_id": pick.category_id,
                "is_carryover": bool(pick.is_carryover)},
               actor_type="system")
    _advance_draft(season, auction)


def submit_pick(auction_id, team_id, player_id):
    """Team manager submits a pick via socket. Returns (ok, reason)."""
    db = _DEPS["db"]
    DBAuction = _DEPS["DBAuction"]
    DBSeason = _DEPS["DBSeason"]
    DBSeasonTeam = _DEPS["DBSeasonTeam"]
    DBAuctionPlayer = _DEPS["DBAuctionPlayer"]
    DBAuctionCategory = _DEPS["DBAuctionCategory"]
    DBDraftPick = _DEPS["DBDraftPick"]
    if DBDraftPick is None:
        return False, "draft-disabled"

    try:
        player_id = int(player_id)
    except (TypeError, ValueError):
        return False, "invalid-player"

    auction = DBAuction.query.get(int(auction_id))
    if auction is None:
        return False, "not-found"
    season = DBSeason.query.get(auction.season_id)
    if season is None or season.status != "auction_live" or season.auction_mode != "draft":
        return False, "not-live"

    with _STATE_LOCK:
        pick = engine.next_pending_pick(db, auction, DBDraftPick)
        if pick is None or pick.season_team_id != int(team_id):
            return False, "not-your-turn"
        team = DBSeasonTeam.query.get(pick.season_team_id)
        player = DBAuctionPlayer.query.get(player_id)
        if player is None or player.auction_id != auction.id:
            return False, "not-found"
        category = DBAuctionCategory.query.get(pick.category_id)

        ok, reason = engine.validate_pick(auction, team, pick, player, category)
        if not ok:
            return False, reason

        engine.apply_draft_pick(db, auction, pick, player, team)
        auction.lot_ends_at = None
        db.session.commit()

    _emit("pick:submitted", {
        "pick_id": pick.id,
        "round": int(pick.round),
        "team_id": team.id,
        "team_name": team.display_name,
        "player_id": player.id,
        "player_name": player.name,
        "category_id": category.id,
        "category_name": category.name,
        "is_carryover": bool(pick.is_carryover),
    }, auction.id)
    _emit_team_update(team, auction.id)
    _log_audit(auction, "pick.submitted",
               {"pick_id": pick.id, "round": int(pick.round),
                "team_id": team.id, "team_name": team.display_name,
                "player_id": player.id, "player_name": player.name,
                "category_name": category.name,
                "is_carryover": bool(pick.is_carryover)},
               actor_type="team", actor_label=team.display_name)
    _advance_draft(season, auction)
    return True, "ok"
