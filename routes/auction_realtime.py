"""
AUCTION-REDESIGN Phase 3 — Realtime foundation.

Provides:
  - HTTP routes:
      /t/<access_token>                  → team manager portal shell
      /seasons/<id>/auction/live         → organizer live console
      /auctions/<auction_id>/chat        → last-N messages for initial chat load (JSON)

  - Socket.IO namespace "/auction":
      connect    (query string: token=<team_token> OR auction_id=<id> + logged-in)
      disconnect
      heartbeat
      chat:send
      chat:delete       (organizer only)
      chat:wipe         (organizer only)

  - In-memory presence map (single-worker only). Moves to Redis later.

No bidding or lot flow here — those land in Phase 4.
"""

import threading
import time
from collections import deque
from datetime import datetime

from flask import abort, jsonify, render_template, request
from flask_login import current_user, login_required
from utils.exception_tracker import log_exception


PRESENCE_TTL_SECONDS = 30
CHAT_HISTORY_LIMIT = 100
MAX_MESSAGE_LEN = 500

# Phase 8.5 — per-sid sliding-window rate limits. Keys are the same sids
# Socket.IO uses, so each physical connection gets its own bucket. On
# disconnect we drop the sid entry below in _presence_drop_sid.
#
# Limits chosen to be generous enough for a frantic human hitting "bid +5"
# or a quick-bid button repeatedly, but tight enough to contain a runaway
# client.
RATE_LIMITS = {
    "bid:place":   (5, 1.0),   # (events, window_seconds) — 5 bids per second
    "pick:submit": (2, 1.0),   # 2 picks per second (draft is one-shot anyway)
    "chat:send":   (4, 2.0),   # 4 chat messages per 2s
}
_RATE_STATE = {}               # sid -> {event_name: deque[timestamps]}
_RATE_LOCK = threading.Lock()


def _rate_allow(sid, event_name):
    """Return True if the event is allowed; False if the sid is over the limit.
    Unknown events are always allowed."""
    cfg = RATE_LIMITS.get(event_name)
    if not cfg:
        return True
    max_events, window_s = cfg
    now = time.time()
    cutoff = now - window_s
    with _RATE_LOCK:
        per_sid = _RATE_STATE.setdefault(sid, {})
        q = per_sid.setdefault(event_name, deque())
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= max_events:
            return False
        q.append(now)
        return True


def _rate_drop_sid(sid):
    with _RATE_LOCK:
        _RATE_STATE.pop(sid, None)


# ── Module-level shared state (process-local) ────────────────────────────────
# sid -> {role, auction_id, team_id?, team_name?, user_id?}
_SID_META = {}
# auction_id -> {team_id -> {"name": str, "last_seen": float}}
_PRESENCE = {}
# auction_id -> {user_id -> last_seen_ts}  (organizer presence)
_ORG_PRESENCE = {}
_STATE_LOCK = threading.Lock()


def _room(auction_id):
    return f"auction:{int(auction_id)}"


def _presence_snapshot(auction_id):
    """Returns {teams: [{id, name, online, last_seen_sec_ago}], organizer_online}."""
    now = time.time()
    with _STATE_LOCK:
        teams = []
        for tid, info in (_PRESENCE.get(auction_id) or {}).items():
            age = now - info["last_seen"]
            teams.append({
                "team_id": tid,
                "name": info["name"],
                "online": age < PRESENCE_TTL_SECONDS,
                "ago_s": int(age),
            })
        org_map = _ORG_PRESENCE.get(auction_id) or {}
        org_online = any((now - ts) < PRESENCE_TTL_SECONDS for ts in org_map.values())
    teams.sort(key=lambda t: t["name"].lower())
    return {"teams": teams, "organizer_online": org_online}


def _presence_touch_team(auction_id, team_id, team_name):
    with _STATE_LOCK:
        _PRESENCE.setdefault(auction_id, {})[team_id] = {
            "name": team_name,
            "last_seen": time.time(),
        }


def _presence_touch_org(auction_id, user_id):
    with _STATE_LOCK:
        _ORG_PRESENCE.setdefault(auction_id, {})[user_id] = time.time()


def _presence_drop_sid(sid):
    """Drop a single sid's presence entry. Returns (auction_id, dropped) so caller can broadcast."""
    _rate_drop_sid(sid)
    meta = _SID_META.pop(sid, None)
    if not meta:
        return None, False
    aid = meta.get("auction_id")
    with _STATE_LOCK:
        if meta.get("role") == "team":
            tid = meta.get("team_id")
            # Only drop if no other sid for the same team is still connected.
            other = any(m for s, m in _SID_META.items() if m.get("auction_id") == aid and m.get("team_id") == tid)
            if not other and aid in _PRESENCE:
                _PRESENCE[aid].pop(tid, None)
        elif meta.get("role") == "organizer":
            uid = meta.get("user_id")
            other = any(m for s, m in _SID_META.items() if m.get("auction_id") == aid and m.get("user_id") == uid and m.get("role") == "organizer")
            if not other and aid in _ORG_PRESENCE:
                _ORG_PRESENCE[aid].pop(uid, None)
    return aid, True


# ── Register HTTP + Socket handlers ──────────────────────────────────────────

def register_auction_realtime(
    app,
    *,
    socketio,
    db,
    DBLeague,
    DBSeason,
    DBSeasonTeam,
    DBAuction,
    DBAuctionCategory,
    DBAuctionPlayer,
    DBChatMessage,
):
    if socketio is None:
        app.logger.warning("[auction_realtime] SocketIO not available — realtime routes disabled.")
        return

    # ─── Helpers ────────────────────────────────────────────────────────────

    def _load_team_context(token):
        st = DBSeasonTeam.query.filter_by(access_token=token).first()
        if st is None:
            return None
        season = DBSeason.query.get(st.season_id)
        if season is None:
            return None
        league = DBLeague.query.get(season.league_id)
        auction = DBAuction.query.filter_by(season_id=season.id).first()
        if auction is None:
            return None
        return {
            "season_team": st,
            "season": season,
            "league": league,
            "auction": auction,
        }

    # Shared ownership guards — same module used by league_routes and
    # auction_routes so ownership semantics can't drift across files.
    from routes._auction_guards import make_guards
    guards = make_guards(
        DBLeague=DBLeague, DBSeason=DBSeason, DBSeasonTeam=DBSeasonTeam,
        DBAuction=DBAuction, DBAuctionCategory=DBAuctionCategory,
        DBAuctionPlayer=DBAuctionPlayer,
    )
    _own_auction = guards.own_auction

    def _serialize_msg(m):
        return {
            "id": m.id,
            "sender_type": m.sender_type,
            "season_team_id": m.season_team_id,
            "sender_label": m.sender_label,
            "body": m.body,
            "deleted": m.deleted_at is not None,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }

    # ─── HTTP: Team portal ──────────────────────────────────────────────────

    @app.route("/t/<token>")
    def team_portal(token):
        ctx = _load_team_context(token)
        if ctx is None:
            abort(404)
        st = ctx["season_team"]
        season = ctx["season"]
        league = ctx["league"]
        auction = ctx["auction"]

        # Sibling teams (for "other teams" strip)
        siblings = (
            DBSeasonTeam.query
            .filter(DBSeasonTeam.season_id == season.id, DBSeasonTeam.id != st.id)
            .order_by(DBSeasonTeam.created_at.asc())
            .all()
        )

        # Squad roster so far (populated during Phase 4+)
        roster = (
            DBAuctionPlayer.query
            .filter_by(auction_id=auction.id, sold_to_season_team_id=st.id)
            .order_by(DBAuctionPlayer.sold_price.desc().nullslast())
            .all()
        )

        # Initial chat history
        msgs = (
            DBChatMessage.query
            .filter_by(auction_id=auction.id)
            .order_by(DBChatMessage.created_at.asc())
            .limit(CHAT_HISTORY_LIMIT)
            .all()
        )

        # Category + players summary (read-only view)
        categories = (
            DBAuctionCategory.query
            .filter_by(auction_id=auction.id)
            .order_by(DBAuctionCategory.display_order.asc())
            .all()
        )

        return render_template(
            "auction/portal.html",
            season=season, league=league, auction=auction,
            season_team=st, siblings=siblings, roster=roster,
            chat_messages=[_serialize_msg(m) for m in msgs],
            categories=categories,
            presence=_presence_snapshot(auction.id),
            access_token=token,
        )

    # ─── HTTP: Organizer live console ───────────────────────────────────────

    @app.route("/seasons/<int:season_id>/auction/live")
    @login_required
    def auction_live(season_id):
        season, league, auction = _own_auction(season_id)
        season_teams = (
            DBSeasonTeam.query
            .filter_by(season_id=season.id)
            .order_by(DBSeasonTeam.created_at.asc())
            .all()
        )
        categories = (
            DBAuctionCategory.query
            .filter_by(auction_id=auction.id)
            .order_by(DBAuctionCategory.display_order.asc())
            .all()
        )
        msgs = (
            DBChatMessage.query
            .filter_by(auction_id=auction.id)
            .order_by(DBChatMessage.created_at.asc())
            .limit(CHAT_HISTORY_LIMIT)
            .all()
        )

        # Phase 6: post-auction summary (rendered when status == auction_done).
        summary_teams = None
        summary_unsold = None
        tournament_modes = None
        rosters_ready = False
        if season.status == "auction_done":
            cats_by_id = {c.id: c.name for c in categories}
            summary_teams = []
            for st in season_teams:
                roster_rows = (DBAuctionPlayer.query
                               .filter_by(auction_id=auction.id, sold_to_season_team_id=st.id)
                               .all())
                # Sort by price desc, then name — reads nicely top-to-bottom.
                roster_rows.sort(key=lambda ap: (-int(ap.sold_price or 0), (ap.name or "")))
                spend = sum(int(ap.sold_price or 0) for ap in roster_rows)
                summary_teams.append({
                    "season_team_id": st.id,
                    "team_id": st.team_id,
                    "display_name": st.display_name,
                    "purse_remaining": int(st.purse_remaining or 0),
                    "players_bought": int(st.players_bought or 0),
                    "total_spend": spend,
                    "roster": [{
                        "name": ap.name,
                        "role": ap.role,
                        "category_name": cats_by_id.get(ap.category_id, "?"),
                        "sold_price": int(ap.sold_price or 0),
                        "sold_in_round": ap.sold_in_round,
                        "batting_rating": ap.batting_rating,
                        "bowling_rating": ap.bowling_rating,
                        "fielding_rating": ap.fielding_rating,
                    } for ap in roster_rows],
                })
            summary_unsold = (DBAuctionPlayer.query
                              .filter(DBAuctionPlayer.auction_id == auction.id,
                                      DBAuctionPlayer.status == "unsold")
                              .order_by(DBAuctionPlayer.name.asc())
                              .all())

            # Phase 7 — tournament mode picker data. We read the concrete
            # Team rows (not just summary_teams counts) so the readiness gate
            # mirrors exactly what the HTTP route enforces.
            from engine.tournament_engine import TournamentEngine
            eng = TournamentEngine()
            tournament_modes = eng.get_available_modes(len(season_teams))
            rosters_ready = len(season_teams) >= 2 and all(
                (st.team is not None and not st.team.is_draft)
                for st in season_teams
            )

        return render_template(
            "auction/live.html",
            season=season, league=league, auction=auction,
            season_teams=season_teams, categories=categories,
            chat_messages=[_serialize_msg(m) for m in msgs],
            presence=_presence_snapshot(auction.id),
            summary_teams=summary_teams,
            summary_unsold=summary_unsold,
            tournament_modes=tournament_modes,
            rosters_ready=rosters_ready,
        )

    # ─── HTTP: chat history JSON (polling fallback / on-open refetch) ──────

    @app.route("/auctions/<int:auction_id>/chat")
    def auction_chat_history(auction_id):
        # Either a logged-in organizer who owns the league, or a team token.
        token = request.args.get("token")
        authorized = False
        if token:
            st = DBSeasonTeam.query.filter_by(access_token=token).first()
            if st:
                season = DBSeason.query.get(st.season_id)
                auction = DBAuction.query.filter_by(season_id=season.id).first()
                if auction and auction.id == auction_id:
                    authorized = True
        elif current_user.is_authenticated:
            auction = DBAuction.query.get(auction_id)
            if auction:
                season = DBSeason.query.get(auction.season_id)
                league = DBLeague.query.get(season.league_id) if season else None
                if league and league.user_id == current_user.id:
                    authorized = True
        if not authorized:
            abort(403)

        msgs = (
            DBChatMessage.query
            .filter_by(auction_id=auction_id)
            .order_by(DBChatMessage.created_at.asc())
            .limit(CHAT_HISTORY_LIMIT)
            .all()
        )
        return jsonify({"messages": [_serialize_msg(m) for m in msgs]})

    # ─── Socket.IO handlers on /auction namespace ───────────────────────────

    from flask_socketio import emit, join_room, leave_room, disconnect

    def _emit_initial_lot(auction):
        """Send the current live state (lot for traditional, pending pick for
        draft) to the just-connected sid so reconnects pick up the action."""
        try:
            from routes import auction_runtime as runtime
            season = DBSeason.query.get(auction.season_id)
            if season is not None and season.auction_mode == "draft":
                snap = runtime.draft_snapshot(auction)
                if snap:
                    emit("pick:turn", snap)
                return
            if auction.live_player_id:
                snap = runtime.lot_snapshot(auction)
                if snap:
                    emit("lot:open", snap)
        except Exception as exc:
            log_exception(exc, source="auction_realtime.initial_lot")

    @socketio.on("connect", namespace="/auction")
    def _ws_connect():
        sid = request.sid
        token = request.args.get("token")
        auction_id_raw = request.args.get("auction_id")

        if token:
            # Team manager
            st = DBSeasonTeam.query.filter_by(access_token=token).first()
            if not st:
                disconnect()
                return False
            season = DBSeason.query.get(st.season_id)
            if not season:
                disconnect()
                return False
            auction = DBAuction.query.filter_by(season_id=season.id).first()
            if not auction:
                disconnect()
                return False
            join_room(_room(auction.id))
            _SID_META[sid] = {
                "role": "team",
                "auction_id": auction.id,
                "team_id": st.id,
                "team_name": st.display_name,
            }
            _presence_touch_team(auction.id, st.id, st.display_name)
            emit("hello", {"role": "team", "team_id": st.id, "auction_id": auction.id})
            emit("presence:update", _presence_snapshot(auction.id), to=_room(auction.id))
            _emit_initial_lot(auction)
            return

        if auction_id_raw and current_user.is_authenticated:
            try:
                aid = int(auction_id_raw)
            except ValueError:
                disconnect()
                return False
            auction = DBAuction.query.get(aid)
            if not auction:
                disconnect()
                return False
            season = DBSeason.query.get(auction.season_id)
            if not season:
                disconnect()
                return False
            league = DBLeague.query.get(season.league_id)
            if not league or league.user_id != current_user.id:
                disconnect()
                return False
            join_room(_room(auction.id))
            _SID_META[sid] = {
                "role": "organizer",
                "auction_id": auction.id,
                "user_id": current_user.id,
            }
            _presence_touch_org(auction.id, current_user.id)
            emit("hello", {"role": "organizer", "auction_id": auction.id})
            emit("presence:update", _presence_snapshot(auction.id), to=_room(auction.id))
            _emit_initial_lot(auction)
            return

        disconnect()
        return False

    @socketio.on("disconnect", namespace="/auction")
    def _ws_disconnect():
        aid, dropped = _presence_drop_sid(request.sid)
        if dropped and aid is not None:
            emit("presence:update", _presence_snapshot(aid), to=_room(aid), namespace="/auction")

    @socketio.on("heartbeat", namespace="/auction")
    def _ws_heartbeat(_data=None):
        meta = _SID_META.get(request.sid)
        if not meta:
            return
        aid = meta["auction_id"]
        if meta["role"] == "team":
            _presence_touch_team(aid, meta["team_id"], meta["team_name"])
        else:
            _presence_touch_org(aid, meta["user_id"])
        emit("presence:update", _presence_snapshot(aid), to=_room(aid))

    @socketio.on("chat:send", namespace="/auction")
    def _ws_chat_send(data):
        meta = _SID_META.get(request.sid)
        if not meta:
            return
        if not _rate_allow(request.sid, "chat:send"):
            # Chat is lossy on flood — drop silently rather than notify.
            return
        body = (data or {}).get("body", "")
        if not isinstance(body, str):
            return
        body = body.strip()
        if not body:
            return
        if len(body) > MAX_MESSAGE_LEN:
            body = body[:MAX_MESSAGE_LEN]

        aid = meta["auction_id"]
        if meta["role"] == "team":
            sender_type = "team"
            season_team_id = meta["team_id"]
            sender_label = meta["team_name"]
        else:
            sender_type = "organizer"
            season_team_id = None
            sender_label = "Organizer"

        try:
            msg = DBChatMessage(
                auction_id=aid,
                sender_type=sender_type,
                season_team_id=season_team_id,
                sender_label=sender_label,
                body=body,
            )
            db.session.add(msg)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="chat:send")
            return

        payload = {
            "id": msg.id,
            "sender_type": msg.sender_type,
            "season_team_id": msg.season_team_id,
            "sender_label": msg.sender_label,
            "body": msg.body,
            "deleted": False,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        }
        emit("chat:new", payload, to=_room(aid))

    @socketio.on("chat:delete", namespace="/auction")
    def _ws_chat_delete(data):
        meta = _SID_META.get(request.sid)
        if not meta or meta["role"] != "organizer":
            return
        msg_id = (data or {}).get("id")
        if not isinstance(msg_id, int):
            return
        msg = DBChatMessage.query.get(msg_id)
        if not msg or msg.auction_id != meta["auction_id"]:
            return
        try:
            msg.deleted_at = datetime.utcnow()
            db.session.commit()
            emit("chat:deleted", {"id": msg.id}, to=_room(meta["auction_id"]))
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="chat:delete")

    @socketio.on("bid:place", namespace="/auction")
    def _ws_bid_place(data):
        """Phase 4 — team places a bid on the live lot."""
        from routes import auction_runtime as runtime
        meta = _SID_META.get(request.sid)
        if not meta or meta.get("role") != "team":
            emit("bid:reject", {"reason": "not-team"})
            return
        if not _rate_allow(request.sid, "bid:place"):
            emit("bid:reject", {"reason": "rate-limited"})
            return
        amount = (data or {}).get("amount")
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            emit("bid:reject", {"reason": "invalid-amount"})
            return
        ok, reason = runtime.place_bid(meta["auction_id"], meta["team_id"], amount)
        if not ok:
            emit("bid:reject", {"reason": reason, "amount": amount})

    @socketio.on("pick:submit", namespace="/auction")
    def _ws_pick_submit(data):
        """Phase 5 — team manager submits a draft pick."""
        from routes import auction_runtime as runtime
        meta = _SID_META.get(request.sid)
        if not meta or meta.get("role") != "team":
            emit("pick:reject", {"reason": "not-team"})
            return
        if not _rate_allow(request.sid, "pick:submit"):
            emit("pick:reject", {"reason": "rate-limited"})
            return
        player_id = (data or {}).get("player_id")
        try:
            player_id = int(player_id)
        except (TypeError, ValueError):
            emit("pick:reject", {"reason": "invalid-player"})
            return
        ok, reason = runtime.submit_pick(meta["auction_id"], meta["team_id"], player_id)
        if not ok:
            emit("pick:reject", {"reason": reason, "player_id": player_id})

    @socketio.on("chat:wipe", namespace="/auction")
    def _ws_chat_wipe(_data=None):
        meta = _SID_META.get(request.sid)
        if not meta or meta["role"] != "organizer":
            return
        aid = meta["auction_id"]
        try:
            DBChatMessage.query.filter_by(auction_id=aid).delete(synchronize_session=False)
            db.session.commit()
            emit("chat:wiped", {}, to=_room(aid))
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="chat:wipe")

    app.logger.info("[auction_realtime] namespace /auction handlers registered")
