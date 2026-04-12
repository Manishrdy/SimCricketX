"""Auction module route registration — organizer setup + live auction + team portal."""

import json
import queue
import threading
import time as time_mod
import uuid

from flask import (
    Response, abort, flash, jsonify, redirect, render_template, request,
    stream_with_context, url_for,
)
from flask_login import current_user, login_required
from utils.exception_tracker import log_exception

# Per-auction SSE broadcast: { event_id: [queue.Queue, ...] }
_auction_listeners = {}
_auction_listeners_lock = threading.Lock()


def _broadcast(event_id, event_type, data):
    with _auction_listeners_lock:
        queues = _auction_listeners.get(event_id, [])
        dead = []
        for q in queues:
            try:
                q.put_nowait((event_type, data))
            except queue.Full:
                dead.append(q)
        for q in dead:
            queues.remove(q)


def _subscribe(event_id):
    q = queue.Queue(maxsize=256)
    with _auction_listeners_lock:
        _auction_listeners.setdefault(event_id, []).append(q)
    return q


def _unsubscribe(event_id, q):
    with _auction_listeners_lock:
        queues = _auction_listeners.get(event_id, [])
        if q in queues:
            queues.remove(q)


def register_auction_routes(
    app,
    *,
    db,
    AuctionEvent,
    AuctionCategory,
    AuctionTeam,
    AuctionPlayer,
    AuctionBid,
    DBMasterPlayer,
    DBUserPlayer,
):

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _require_organizer(event_id):
        event = AuctionEvent.query.get_or_404(event_id)
        if event.user_id != current_user.id:
            abort(403)
        return event

    def _get_effective_pool(user_id):
        masters = DBMasterPlayer.query.order_by(DBMasterPlayer.name).all()
        overrides = {}
        customs = []
        user_players = DBUserPlayer.query.filter_by(user_id=user_id).all()
        for up in user_players:
            if up.master_player_id is not None:
                overrides[up.master_player_id] = up
            else:
                customs.append(up)

        pool = []
        for mp in masters:
            if mp.id in overrides:
                up = overrides[mp.id]
                pool.append({
                    "source": "override", "master_player_id": mp.id,
                    "user_player_id": up.id, "obj": up,
                })
            else:
                pool.append({
                    "source": "master", "master_player_id": mp.id,
                    "user_player_id": None, "obj": mp,
                })
        for cp in customs:
            pool.append({
                "source": "custom", "master_player_id": None,
                "user_player_id": cp.id, "obj": cp,
            })
        return pool

    def _parse_increment_tiers(raw):
        try:
            tiers = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(tiers, list):
                return None, "Increment tiers must be a JSON array."
            cleaned = []
            for t in tiers:
                up_to = int(t.get("up_to", 0))
                increment = int(t.get("increment", 0))
                if increment <= 0:
                    return None, "Each increment must be > 0."
                cleaned.append({"up_to": up_to, "increment": increment})
            return cleaned, None
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return None, f"Invalid increment tiers: {exc}"

    # ═══════════════════════════════════════════════════════════════════════
    #  AUCTION LIST + CREATE
    # ═══════════════════════════════════════════════════════════════════════

    @app.route("/auctions")
    @login_required
    def auction_list():
        events = AuctionEvent.query.filter_by(user_id=current_user.id).order_by(
            AuctionEvent.created_at.desc()
        ).all()
        return render_template("auction/list.html", events=events)

    @app.route("/auction/create", methods=["GET", "POST"])
    @login_required
    def auction_create():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            fmt = request.form.get("format", "T20").strip()
            num_teams = request.form.get("num_teams", "8").strip()
            if not name:
                flash("Event name is required.", "danger")
                return redirect(url_for("auction_create"))
            try:
                num_teams = int(num_teams)
                if num_teams < 2 or num_teams > 20:
                    raise ValueError
            except ValueError:
                flash("Number of teams must be between 2 and 20.", "danger")
                return redirect(url_for("auction_create"))
            event = AuctionEvent(
                user_id=current_user.id,
                name=name,
                format=fmt,
                num_teams=num_teams,
            )
            db.session.add(event)
            db.session.commit()
            flash(f"Auction '{name}' created.", "success")
            return redirect(url_for("auction_setup", event_id=event.id))
        return render_template("auction/create.html")

    # ═══════════════════════════════════════════════════════════════════════
    #  SETUP — event configuration
    # ═══════════════════════════════════════════════════════════════════════

    @app.route("/auction/<int:event_id>/setup", methods=["GET", "POST"])
    @login_required
    def auction_setup(event_id):
        event = _require_organizer(event_id)
        if event.status != "setup":
            flash("Cannot edit configuration after auction has started.", "warning")
            return redirect(url_for("auction_dashboard", event_id=event_id))
        if request.method == "POST":
            event.budget_mode = request.form.get("budget_mode", "uniform")
            try:
                event.uniform_budget = int(request.form.get("uniform_budget", 0))
            except ValueError:
                event.uniform_budget = 0
            try:
                event.min_players_per_team = int(request.form.get("min_players_per_team", 12))
                event.max_players_per_team = int(request.form.get("max_players_per_team", 25))
            except ValueError:
                pass
            tiers_raw = request.form.get("bid_increment_tiers", "[]")
            tiers, err = _parse_increment_tiers(tiers_raw)
            if err:
                flash(err, "danger")
                return redirect(url_for("auction_setup", event_id=event_id))
            event.bid_increment_tiers = json.dumps(tiers)
            event.reauction_enabled = request.form.get("reauction_enabled") == "on"
            try:
                event.max_reauction_rounds = int(request.form.get("max_reauction_rounds", 0)) or None
            except ValueError:
                event.max_reauction_rounds = None
            try:
                event.reauction_base_price_reduction_pct = int(request.form.get("reauction_base_price_reduction_pct", 0))
            except ValueError:
                event.reauction_base_price_reduction_pct = 0
            db.session.commit()
            flash("Configuration saved.", "success")
            return redirect(url_for("auction_setup", event_id=event_id))
        return render_template("auction/setup.html", event=event)

    # ═══════════════════════════════════════════════════════════════════════
    #  SETUP — teams
    # ═══════════════════════════════════════════════════════════════════════

    @app.route("/auction/<int:event_id>/setup/teams", methods=["GET", "POST"])
    @login_required
    def auction_setup_teams(event_id):
        event = _require_organizer(event_id)
        if event.status != "setup":
            flash("Cannot edit teams after auction has started.", "warning")
            return redirect(url_for("auction_dashboard", event_id=event_id))
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                name = request.form.get("team_name", "").strip()
                if not name:
                    flash("Team name is required.", "danger")
                elif AuctionTeam.query.filter_by(event_id=event_id, name=name).first():
                    flash(f"Team '{name}' already exists.", "warning")
                elif len(event.teams) >= event.num_teams:
                    flash(f"Maximum {event.num_teams} teams allowed.", "warning")
                else:
                    budget = event.uniform_budget if event.budget_mode == "uniform" else 0
                    custom_budget = None
                    if event.budget_mode == "custom":
                        try:
                            custom_budget = int(request.form.get("custom_budget", 0))
                        except ValueError:
                            custom_budget = 0
                        budget = custom_budget
                    team = AuctionTeam(
                        event_id=event_id,
                        name=name,
                        custom_budget=custom_budget,
                        purse_remaining=budget,
                    )
                    db.session.add(team)
                    db.session.commit()
                    flash(f"Team '{name}' added.", "success")
            elif action == "delete":
                team_id = request.form.get("team_id")
                team = AuctionTeam.query.get(team_id)
                if team and team.event_id == event_id:
                    db.session.delete(team)
                    db.session.commit()
                    flash(f"Team '{team.name}' removed.", "success")
            elif action == "update_budget":
                team_id = request.form.get("team_id")
                team = AuctionTeam.query.get(team_id)
                if team and team.event_id == event_id and event.budget_mode == "custom":
                    try:
                        team.custom_budget = int(request.form.get("custom_budget", 0))
                        team.purse_remaining = team.custom_budget
                        db.session.commit()
                        flash(f"Budget updated for '{team.name}'.", "success")
                    except ValueError:
                        flash("Invalid budget value.", "danger")
            return redirect(url_for("auction_setup_teams", event_id=event_id))
        return render_template("auction/setup_teams.html", event=event)

    # ═══════════════════════════════════════════════════════════════════════
    #  SETUP — categories
    # ═══════════════════════════════════════════════════════════════════════

    @app.route("/auction/<int:event_id>/setup/categories", methods=["GET", "POST"])
    @login_required
    def auction_setup_categories(event_id):
        event = _require_organizer(event_id)
        if event.status != "setup":
            flash("Cannot edit categories after auction has started.", "warning")
            return redirect(url_for("auction_dashboard", event_id=event_id))
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                name = request.form.get("category_name", "").strip()
                if not name:
                    flash("Category name is required.", "danger")
                elif AuctionCategory.query.filter_by(event_id=event_id, name=name).first():
                    flash(f"Category '{name}' already exists.", "warning")
                else:
                    try:
                        base_price = int(request.form.get("default_base_price", 0))
                    except ValueError:
                        base_price = 0
                    max_per = request.form.get("max_per_team", "").strip()
                    max_per_team = int(max_per) if max_per else None
                    cat = AuctionCategory(
                        event_id=event_id,
                        name=name,
                        default_base_price=base_price,
                        max_per_team=max_per_team,
                    )
                    db.session.add(cat)
                    db.session.commit()
                    flash(f"Category '{name}' added.", "success")
            elif action == "delete":
                cat_id = request.form.get("category_id")
                cat = AuctionCategory.query.get(cat_id)
                if cat and cat.event_id == event_id:
                    db.session.delete(cat)
                    db.session.commit()
                    flash(f"Category '{cat.name}' removed.", "success")
            return redirect(url_for("auction_setup_categories", event_id=event_id))
        return render_template("auction/setup_categories.html", event=event)

    # ═══════════════════════════════════════════════════════════════════════
    #  SETUP — player pool curation
    # ═══════════════════════════════════════════════════════════════════════

    @app.route("/auction/<int:event_id>/setup/players", methods=["GET", "POST"])
    @login_required
    def auction_setup_players(event_id):
        event = _require_organizer(event_id)
        if event.status != "setup":
            flash("Cannot edit players after auction has started.", "warning")
            return redirect(url_for("auction_dashboard", event_id=event_id))
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add_players":
                selected_ids = request.form.getlist("player_ids")
                pool = _get_effective_pool(current_user.id)
                pool_by_key = {}
                for p in pool:
                    key = f"{p['source']}_{p['obj'].id}"
                    pool_by_key[key] = p
                added = 0
                for pid in selected_ids:
                    entry = pool_by_key.get(pid)
                    if not entry:
                        continue
                    obj = entry["obj"]
                    already = AuctionPlayer.query.filter_by(event_id=event_id, name=obj.name).first()
                    if already:
                        continue
                    ap = AuctionPlayer(
                        event_id=event_id,
                        master_player_id=entry["master_player_id"],
                        user_player_id=entry["user_player_id"],
                        name=obj.name,
                        role=obj.role,
                        batting_rating=obj.batting_rating,
                        bowling_rating=obj.bowling_rating,
                        fielding_rating=obj.fielding_rating,
                        batting_hand=obj.batting_hand or "",
                        bowling_type=obj.bowling_type or "",
                        bowling_hand=obj.bowling_hand or "",
                    )
                    db.session.add(ap)
                    added += 1
                db.session.commit()
                flash(f"{added} player(s) added to auction pool.", "success")
            elif action == "remove_player":
                ap_id = request.form.get("auction_player_id")
                ap = AuctionPlayer.query.get(ap_id)
                if ap and ap.event_id == event_id:
                    db.session.delete(ap)
                    db.session.commit()
                    flash(f"'{ap.name}' removed from auction pool.", "success")
            elif action == "set_base_price":
                ap_id = request.form.get("auction_player_id")
                ap = AuctionPlayer.query.get(ap_id)
                if ap and ap.event_id == event_id:
                    try:
                        ap.base_price = int(request.form.get("base_price", 0))
                        db.session.commit()
                        flash(f"Base price set for '{ap.name}'.", "success")
                    except ValueError:
                        flash("Invalid price.", "danger")
            elif action == "set_lot_order":
                ap_id = request.form.get("auction_player_id")
                ap = AuctionPlayer.query.get(ap_id)
                if ap and ap.event_id == event_id:
                    order_val = request.form.get("lot_order", "").strip()
                    ap.lot_order = int(order_val) if order_val else None
                    db.session.commit()
                    flash(f"Lot order updated for '{ap.name}'.", "success")
            elif action == "assign_category":
                ap_id = request.form.get("auction_player_id")
                cat_id = request.form.get("category_id")
                ap = AuctionPlayer.query.get(ap_id)
                cat = AuctionCategory.query.get(cat_id)
                if ap and cat and ap.event_id == event_id and cat.event_id == event_id:
                    if cat not in ap.categories:
                        ap.categories.append(cat)
                        db.session.commit()
                        flash(f"Category '{cat.name}' assigned to '{ap.name}'.", "success")
            elif action == "remove_category":
                ap_id = request.form.get("auction_player_id")
                cat_id = request.form.get("category_id")
                ap = AuctionPlayer.query.get(ap_id)
                cat = AuctionCategory.query.get(cat_id)
                if ap and cat and cat in ap.categories:
                    ap.categories.remove(cat)
                    db.session.commit()
                    flash(f"Category '{cat.name}' removed from '{ap.name}'.", "success")
            return redirect(url_for("auction_setup_players", event_id=event_id))

        pool = _get_effective_pool(current_user.id)
        already_ids = set()
        for ap in event.players:
            if ap.master_player_id:
                already_ids.add(f"master_{ap.master_player_id}")
                already_ids.add(f"override_{ap.master_player_id}")
            if ap.user_player_id:
                already_ids.add(f"custom_{ap.user_player_id}")
        available = [p for p in pool if f"{p['source']}_{p['obj'].id}" not in already_ids]

        return render_template(
            "auction/setup_players.html",
            event=event,
            available_pool=available,
            auction_players=event.players,
            categories=event.categories,
        )

    # ── Bid validation helpers ───────────────────────────────────────────

    def _get_min_next_bid(event, current_highest, base_price):
        if current_highest is None:
            return base_price or 0
        tiers = json.loads(event.bid_increment_tiers or "[]")
        increment = 0
        for tier in sorted(tiers, key=lambda t: t.get("up_to", 0)):
            increment = tier.get("increment", 0)
            if current_highest < tier.get("up_to", 0):
                break
        if increment <= 0:
            increment = 1
        return current_highest + increment

    def _effective_base_price(player, event):
        if player.base_price is not None:
            return player.base_price
        if player.categories:
            prices = [c.default_base_price for c in player.categories if c.default_base_price]
            if prices:
                return max(prices)
        return 0

    def _get_current_lot(event_id):
        return AuctionPlayer.query.filter_by(event_id=event_id, status="live").first()

    def _get_highest_bid(player_id):
        return AuctionBid.query.filter_by(auction_player_id=player_id).order_by(
            AuctionBid.amount.desc()
        ).first()

    def _build_lot_payload(player, event):
        base = _effective_base_price(player, event)
        highest = _get_highest_bid(player.id)
        return {
            "player_id": player.id,
            "name": player.name,
            "role": player.role or "",
            "batting_rating": player.batting_rating,
            "bowling_rating": player.bowling_rating,
            "fielding_rating": player.fielding_rating,
            "batting_hand": player.batting_hand or "",
            "bowling_type": player.bowling_type or "",
            "base_price": base,
            "current_bid": highest.amount if highest else None,
            "current_bidder": highest.team.name if highest else None,
            "current_bidder_id": highest.team_id if highest else None,
            "min_next_bid": _get_min_next_bid(event, highest.amount if highest else None, base),
            "categories": [c.name for c in player.categories],
        }

    def _build_teams_payload(event):
        teams = []
        for t in event.teams:
            teams.append({
                "id": t.id,
                "name": t.name,
                "purse_remaining": t.purse_remaining,
                "players_bought": t.players_bought,
            })
        return teams

    # ═══════════════════════════════════════════════════════════════════════
    #  ORGANIZER DASHBOARD
    # ═══════════════════════════════════════════════════════════════════════

    @app.route("/auction/<int:event_id>/dashboard")
    @login_required
    def auction_dashboard(event_id):
        event = _require_organizer(event_id)
        current_lot = _get_current_lot(event_id)
        lot_payload = _build_lot_payload(current_lot, event) if current_lot else None
        upcoming = AuctionPlayer.query.filter_by(event_id=event_id, status="upcoming").order_by(
            AuctionPlayer.lot_order.asc().nullslast(), AuctionPlayer.name
        ).all()
        return render_template(
            "auction/dashboard.html",
            event=event,
            current_lot=current_lot,
            lot_payload=lot_payload,
            upcoming_players=upcoming,
        )

    # ═══════════════════════════════════════════════════════════════════════
    #  LIVE AUCTION CONTROLS (organizer)
    # ═══════════════════════════════════════════════════════════════════════

    @app.route("/auction/<int:event_id>/start", methods=["POST"])
    @login_required
    def auction_start(event_id):
        event = _require_organizer(event_id)
        if event.status != "setup":
            return jsonify({"error": "Auction already started."}), 400
        if not event.teams:
            return jsonify({"error": "Add at least one team first."}), 400
        if not event.players:
            return jsonify({"error": "Add players to the auction pool first."}), 400
        if event.budget_mode == "uniform":
            for t in event.teams:
                t.purse_remaining = event.uniform_budget
        event.status = "live"
        event.current_round = 1
        db.session.commit()
        _broadcast(event_id, "status", {"status": "live", "round": 1})
        return jsonify({"ok": True})

    @app.route("/auction/<int:event_id>/lot/<int:player_id>/open", methods=["POST"])
    @login_required
    def auction_lot_open(event_id, player_id):
        event = _require_organizer(event_id)
        if event.status not in ("live", "paused"):
            return jsonify({"error": "Auction is not active."}), 400
        current = _get_current_lot(event_id)
        if current:
            return jsonify({"error": f"'{current.name}' is still live. Sell or mark unsold first."}), 400
        player = AuctionPlayer.query.get_or_404(player_id)
        if player.event_id != event_id or player.status != "upcoming":
            return jsonify({"error": "Player not available."}), 400
        player.status = "live"
        if event.status == "paused":
            event.status = "live"
        db.session.commit()
        payload = _build_lot_payload(player, event)
        _broadcast(event_id, "lot", payload)
        _broadcast(event_id, "status", {"status": "live"})
        return jsonify({"ok": True, "lot": payload})

    @app.route("/auction/<int:event_id>/lot/<int:player_id>/sell", methods=["POST"])
    @login_required
    def auction_lot_sell(event_id, player_id):
        event = _require_organizer(event_id)
        player = AuctionPlayer.query.get_or_404(player_id)
        if player.event_id != event_id or player.status != "live":
            return jsonify({"error": "Player is not currently live."}), 400
        highest = _get_highest_bid(player.id)
        if not highest:
            return jsonify({"error": "No bids placed. Mark as unsold instead."}), 400
        team = AuctionTeam.query.get(highest.team_id)
        player.status = "sold"
        player.sold_to = team.id
        player.sold_price = highest.amount
        player.sold_in_round = event.current_round
        team.purse_remaining -= highest.amount
        team.players_bought += 1
        db.session.commit()
        _broadcast(event_id, "sold", {
            "player_id": player.id,
            "player_name": player.name,
            "team_id": team.id,
            "team_name": team.name,
            "price": highest.amount,
            "round": event.current_round,
        })
        _broadcast(event_id, "purse", _build_teams_payload(event))
        return jsonify({"ok": True})

    @app.route("/auction/<int:event_id>/lot/<int:player_id>/unsold", methods=["POST"])
    @login_required
    def auction_lot_unsold(event_id, player_id):
        event = _require_organizer(event_id)
        player = AuctionPlayer.query.get_or_404(player_id)
        if player.event_id != event_id or player.status != "live":
            return jsonify({"error": "Player is not currently live."}), 400
        player.status = "unsold"
        db.session.commit()
        _broadcast(event_id, "unsold", {
            "player_id": player.id,
            "player_name": player.name,
        })
        return jsonify({"ok": True})

    @app.route("/auction/<int:event_id>/pause", methods=["POST"])
    @login_required
    def auction_pause(event_id):
        event = _require_organizer(event_id)
        if event.status != "live":
            return jsonify({"error": "Auction is not live."}), 400
        event.status = "paused"
        db.session.commit()
        _broadcast(event_id, "status", {"status": "paused"})
        return jsonify({"ok": True})

    @app.route("/auction/<int:event_id>/resume", methods=["POST"])
    @login_required
    def auction_resume(event_id):
        event = _require_organizer(event_id)
        if event.status != "paused":
            return jsonify({"error": "Auction is not paused."}), 400
        event.status = "live"
        db.session.commit()
        _broadcast(event_id, "status", {"status": "live"})
        return jsonify({"ok": True})

    @app.route("/auction/<int:event_id>/complete", methods=["POST"])
    @login_required
    def auction_complete(event_id):
        event = _require_organizer(event_id)
        if event.status not in ("live", "paused"):
            return jsonify({"error": "Auction is not active."}), 400
        current = _get_current_lot(event_id)
        if current:
            return jsonify({"error": f"'{current.name}' is still live. Resolve it first."}), 400
        event.status = "completed"
        db.session.commit()
        _broadcast(event_id, "status", {"status": "completed"})
        return jsonify({"ok": True})

    # ═══════════════════════════════════════════════════════════════════════
    #  RE-AUCTION ROUNDS
    # ═══════════════════════════════════════════════════════════════════════

    @app.route("/auction/<int:event_id>/reauction", methods=["POST"])
    @login_required
    def auction_reauction(event_id):
        event = _require_organizer(event_id)
        if event.status not in ("live", "paused"):
            return jsonify({"error": "Auction must be live or paused to trigger re-auction."}), 400
        if not event.reauction_enabled:
            return jsonify({"error": "Re-auction is not enabled for this event."}), 400
        current = _get_current_lot(event_id)
        if current:
            return jsonify({"error": f"'{current.name}' is still live. Resolve it first."}), 400
        next_round = event.current_round + 1
        if event.max_reauction_rounds and (next_round - 1) > event.max_reauction_rounds:
            return jsonify({"error": f"Maximum re-auction rounds ({event.max_reauction_rounds}) reached."}), 400
        unsold = AuctionPlayer.query.filter_by(event_id=event_id, status="unsold").all()
        if not unsold:
            return jsonify({"error": "No unsold players to re-auction."}), 400
        reduction_pct = event.reauction_base_price_reduction_pct or 0
        reactivated = 0
        for p in unsold:
            p.status = "upcoming"
            if reduction_pct > 0 and p.base_price is not None and p.base_price > 0:
                p.base_price = max(0, int(p.base_price * (100 - reduction_pct) / 100))
            reactivated += 1
        event.current_round = next_round
        event.status = "live"
        db.session.commit()
        _broadcast(event_id, "status", {
            "status": "live",
            "round": next_round,
            "reauction": True,
            "players_reactivated": reactivated,
        })
        return jsonify({"ok": True, "round": next_round, "reactivated": reactivated})

    # ═══════════════════════════════════════════════════════════════════════
    #  AUCTION SUMMARY & EXPORT
    # ═══════════════════════════════════════════════════════════════════════

    @app.route("/auction/<int:event_id>/summary")
    @login_required
    def auction_summary(event_id):
        event = _require_organizer(event_id)
        sold = AuctionPlayer.query.filter_by(event_id=event_id, status="sold").order_by(
            AuctionPlayer.sold_price.desc()
        ).all()
        unsold = AuctionPlayer.query.filter_by(event_id=event_id, status="unsold").all()
        remaining = AuctionPlayer.query.filter_by(event_id=event_id, status="upcoming").all()
        total_spent = sum(p.sold_price or 0 for p in sold)
        return render_template(
            "auction/summary.html",
            event=event,
            sold_players=sold,
            unsold_players=unsold,
            remaining_players=remaining,
            total_spent=total_spent,
        )

    @app.route("/auction/<int:event_id>/export/json")
    @login_required
    def auction_export_json(event_id):
        event = _require_organizer(event_id)
        result = {"event": event.name, "format": event.format, "status": event.status, "teams": []}
        for team in event.teams:
            won = AuctionPlayer.query.filter_by(event_id=event_id, sold_to=team.id).order_by(
                AuctionPlayer.sold_price.desc()
            ).all()
            team_data = {
                "name": team.name,
                "budget": team.custom_budget if team.custom_budget is not None else event.uniform_budget,
                "purse_remaining": team.purse_remaining,
                "players": [{
                    "name": p.name,
                    "role": p.role or "",
                    "batting_rating": p.batting_rating,
                    "bowling_rating": p.bowling_rating,
                    "fielding_rating": p.fielding_rating,
                    "sold_price": p.sold_price,
                    "round": p.sold_in_round,
                } for p in won],
            }
            result["teams"].append(team_data)
        unsold = AuctionPlayer.query.filter_by(event_id=event_id, status="unsold").all()
        result["unsold"] = [{"name": p.name, "role": p.role or ""} for p in unsold]
        return jsonify(result)

    # ═══════════════════════════════════════════════════════════════════════
    #  TEAM PORTAL + BIDDING
    # ═══════════════════════════════════════════════════════════════════════

    @app.route("/auction/<int:event_id>/team/<token>")
    def auction_team_portal(event_id, token):
        team = AuctionTeam.query.filter_by(event_id=event_id, access_token=token).first_or_404()
        event = team.event
        current_lot = _get_current_lot(event_id)
        lot_payload = _build_lot_payload(current_lot, event) if current_lot else None
        won = AuctionPlayer.query.filter_by(event_id=event_id, sold_to=team.id).order_by(
            AuctionPlayer.sold_in_round, AuctionPlayer.sold_price.desc()
        ).all()
        return render_template(
            "auction/team_portal.html",
            event=event,
            team=team,
            won_players=won,
            current_lot=current_lot,
            lot_payload=lot_payload,
        )

    @app.route("/auction/<int:event_id>/team/<token>/bid", methods=["POST"])
    def auction_team_bid(event_id, token):
        team = AuctionTeam.query.filter_by(event_id=event_id, access_token=token).first_or_404()
        event = team.event
        if event.status != "live":
            return jsonify({"error": "Auction is not live."}), 400
        current = _get_current_lot(event_id)
        if not current:
            return jsonify({"error": "No player is currently up for bidding."}), 400
        if team.players_bought >= event.max_players_per_team:
            return jsonify({"error": "Squad is full."}), 400

        # Category quota check
        for cat in current.categories:
            if cat.max_per_team is not None:
                count = 0
                for wp in team.won_players:
                    if cat in wp.categories:
                        count += 1
                if count >= cat.max_per_team:
                    return jsonify({"error": f"Category quota reached for '{cat.name}'."}), 400

        base = _effective_base_price(current, event)
        highest = _get_highest_bid(current.id)
        min_bid = _get_min_next_bid(event, highest.amount if highest else None, base)

        data = request.get_json(silent=True) or {}
        try:
            amount = int(data.get("amount", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid bid amount."}), 400

        if amount < min_bid:
            return jsonify({"error": f"Bid must be at least {min_bid:,}."}), 400

        # Purse check — must retain enough for remaining mandatory slots
        slots_needed = max(0, event.min_players_per_team - team.players_bought - 1)
        reserved = 0
        if slots_needed > 0:
            all_base_prices = [_effective_base_price(p, event) for p in event.players if p.status == "upcoming"]
            all_base_prices.sort()
            reserved = sum(all_base_prices[:slots_needed]) if all_base_prices else 0
        if amount > team.purse_remaining - reserved:
            return jsonify({"error": "Insufficient purse (must reserve budget for remaining slots)."}), 400

        bid = AuctionBid(
            auction_player_id=current.id,
            team_id=team.id,
            amount=amount,
            round=event.current_round,
        )
        db.session.add(bid)
        db.session.commit()

        _broadcast(event_id, "bid", {
            "player_id": current.id,
            "team_id": team.id,
            "team_name": team.name,
            "amount": amount,
            "min_next_bid": _get_min_next_bid(event, amount, base),
        })
        return jsonify({"ok": True, "amount": amount})

    # ═══════════════════════════════════════════════════════════════════════
    #  SSE STREAM
    # ═══════════════════════════════════════════════════════════════════════

    @app.route("/auction/<int:event_id>/stream")
    def auction_stream(event_id):
        AuctionEvent.query.get_or_404(event_id)
        q = _subscribe(event_id)

        @stream_with_context
        def generate():
            try:
                while True:
                    try:
                        event_type, data = q.get(timeout=15)
                        yield f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
                    except queue.Empty:
                        yield ": keep-alive\n\n"
            except GeneratorExit:
                pass
            finally:
                _unsubscribe(event_id, q)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ═══════════════════════════════════════════════════════════════════════
    #  API — auction state (for AJAX polling fallback)
    # ═══════════════════════════════════════════════════════════════════════

    @app.route("/auction/<int:event_id>/state")
    def auction_state(event_id):
        event = AuctionEvent.query.get_or_404(event_id)
        current = _get_current_lot(event_id)
        return jsonify({
            "status": event.status,
            "round": event.current_round,
            "lot": _build_lot_payload(current, event) if current else None,
            "teams": _build_teams_payload(event),
        })
