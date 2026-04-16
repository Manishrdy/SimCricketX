"""
AUCTION-REDESIGN Phase 2 — Auction setup routes.

Configures the auction attached to a season: budgets, categories, player
curation from the master + user pool, bid-increment tiers, timers, and
re-auction rules. No runtime (live bidding) here — that lands in Phase 4.
"""

import json
import random
from datetime import datetime

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from utils.exception_tracker import log_exception


VALID_BUDGET_MODES = ("uniform", "custom")
VALID_CATEGORY_ORDER_MODES = ("manual", "random")
SETUP_EDITABLE_STATUSES = {"setup", "teams_ready", "auction_ready"}  # can still be reopened/tweaked before start


def register_auction_routes(
    app,
    *,
    db,
    DBSeason,
    DBSeasonTeam,
    DBLeague,
    DBTeam,
    DBAuction,
    DBAuctionCategory,
    DBAuctionPlayer,
    DBMasterPlayer,
    DBUserPlayer,
):
    # ─── Helpers ────────────────────────────────────────────────────────────

    def _own_season_or_404(season_id):
        s = DBSeason.query.get(season_id)
        if s is None:
            abort(404)
        lg = DBLeague.query.get(s.league_id)
        if lg is None or lg.user_id != current_user.id:
            abort(404)
        return s, lg

    def _get_or_create_auction(season):
        auc = DBAuction.query.filter_by(season_id=season.id).first()
        if auc is None:
            auc = DBAuction(season_id=season.id)
            db.session.add(auc)
            db.session.commit()
        return auc

    def _require_editable(season):
        """Block mutations once the live auction has started or finished."""
        if season.status not in SETUP_EDITABLE_STATUSES:
            abort(409, description=f"Season status '{season.status}' does not allow setup edits.")

    def _own_category(cat_id):
        cat = DBAuctionCategory.query.get(cat_id)
        if cat is None:
            abort(404)
        auc = DBAuction.query.get(cat.auction_id)
        season = DBSeason.query.get(auc.season_id)
        _own_season_or_404(season.id)  # enforces ownership
        return cat, auc, season

    def _own_ap(ap_id):
        ap = DBAuctionPlayer.query.get(ap_id)
        if ap is None:
            abort(404)
        auc = DBAuction.query.get(ap.auction_id)
        season = DBSeason.query.get(auc.season_id)
        _own_season_or_404(season.id)
        return ap, auc, season

    def _get_pool(user_id):
        """Merge master pool with this user's overrides/custom players.

        Returns a list of dicts each with source ('master'|'user'), an id
        string like 'master:42' or 'user:17', and a flat row dict suitable
        for rendering and snapshotting.
        """
        pool = []
        overridden_master_ids = set()

        # User rows override master ones when master_player_id is set.
        user_rows = DBUserPlayer.query.filter_by(user_id=user_id).all()
        for u in user_rows:
            if u.master_player_id is not None:
                overridden_master_ids.add(u.master_player_id)
            pool.append({
                "source": "user",
                "ref": f"user:{u.id}",
                "id": u.id,
                "name": u.name,
                "role": u.role,
                "batting_rating": u.batting_rating,
                "bowling_rating": u.bowling_rating,
                "fielding_rating": u.fielding_rating,
                "batting_hand": u.batting_hand,
                "bowling_type": u.bowling_type,
                "bowling_hand": u.bowling_hand,
            })

        for m in DBMasterPlayer.query.all():
            if m.id in overridden_master_ids:
                continue
            pool.append({
                "source": "master",
                "ref": f"master:{m.id}",
                "id": m.id,
                "name": m.name,
                "role": m.role,
                "batting_rating": m.batting_rating,
                "bowling_rating": m.bowling_rating,
                "fielding_rating": m.fielding_rating,
                "batting_hand": m.batting_hand,
                "bowling_type": m.bowling_type,
                "bowling_hand": m.bowling_hand,
            })

        pool.sort(key=lambda r: (r["name"] or "").lower())
        return pool

    def _parse_int(raw, default=None):
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except (ValueError, TypeError):
            return default

    def _curated_refs(auction_id):
        """Returns a set of 'master:N' / 'user:N' refs already in the curated pool."""
        refs = set()
        for ap in DBAuctionPlayer.query.filter_by(auction_id=auction_id).all():
            if ap.master_player_id is not None:
                refs.add(f"master:{ap.master_player_id}")
            elif ap.user_player_id is not None:
                refs.add(f"user:{ap.user_player_id}")
        return refs

    # ─── Setup hub ──────────────────────────────────────────────────────────

    @app.route("/seasons/<int:season_id>/auction")
    @login_required
    def auction_setup(season_id):
        season, league = _own_season_or_404(season_id)
        auction = _get_or_create_auction(season)

        categories = (
            DBAuctionCategory.query
            .filter_by(auction_id=auction.id)
            .order_by(DBAuctionCategory.display_order.asc(), DBAuctionCategory.id.asc())
            .all()
        )
        players = (
            DBAuctionPlayer.query
            .filter_by(auction_id=auction.id)
            .order_by(DBAuctionPlayer.category_id.asc(), DBAuctionPlayer.lot_order.asc().nullslast(), DBAuctionPlayer.name.asc())
            .all()
        )
        season_teams = (
            DBSeasonTeam.query
            .filter_by(season_id=season.id)
            .order_by(DBSeasonTeam.created_at.asc())
            .all()
        )

        curated_refs = _curated_refs(auction.id)
        pool = _get_pool(current_user.id)
        pool_available = [p for p in pool if p["ref"] not in curated_refs]

        # Group curated players by category for the right-hand pane.
        players_by_cat = {}
        for p in players:
            players_by_cat.setdefault(p.category_id, []).append(p)

        # Pre-compute finalize readiness.
        errors = _finalize_errors(season, auction, categories, players, season_teams)
        is_draft_mode = season.auction_mode == "draft"

        return render_template(
            "auction/setup.html",
            season=season,
            league=league,
            auction=auction,
            categories=categories,
            players=players,
            players_by_cat=players_by_cat,
            season_teams=season_teams,
            pool_available=pool_available,
            finalize_errors=errors,
            is_draft_mode=is_draft_mode,
        )

    # ─── Config ─────────────────────────────────────────────────────────────

    @app.route("/seasons/<int:season_id>/auction/config", methods=["POST"])
    @login_required
    def auction_config(season_id):
        season, _ = _own_season_or_404(season_id)
        _require_editable(season)
        auction = _get_or_create_auction(season)

        budget_mode = (request.form.get("budget_mode") or "uniform").strip()
        uniform_budget = _parse_int(request.form.get("uniform_budget"), 0)
        min_players = _parse_int(request.form.get("min_players_per_team"), 12)
        max_players = _parse_int(request.form.get("max_players_per_team"), 25)
        per_player_timer = _parse_int(request.form.get("per_player_timer_seconds"), 20)
        draft_pick_timer = _parse_int(request.form.get("draft_pick_timer_seconds"), 30)
        reauction_rounds = _parse_int(request.form.get("reauction_rounds"), 0)
        reauction_pct = _parse_int(request.form.get("reauction_price_reduction_pct"), 0)
        cat_order_mode = (request.form.get("category_order_mode") or "manual").strip()
        bid_increment = _parse_int(request.form.get("bid_increment"), 0)

        if budget_mode not in VALID_BUDGET_MODES:
            flash("Invalid budget mode.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        if cat_order_mode not in VALID_CATEGORY_ORDER_MODES:
            flash("Invalid category order mode.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        if uniform_budget is None or uniform_budget < 0:
            flash("Uniform budget must be a non-negative integer.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        if min_players is None or max_players is None or min_players < 1 or max_players < min_players:
            flash("Player limits are invalid: need 1 ≤ min ≤ max.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        if per_player_timer is None or per_player_timer < 3 or per_player_timer > 600:
            flash("Per-player timer must be between 3 and 600 seconds.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        if draft_pick_timer is None or draft_pick_timer < 3 or draft_pick_timer > 600:
            flash("Draft pick timer must be between 3 and 600 seconds.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        if reauction_rounds is None or reauction_rounds < 0 or reauction_rounds > 10:
            flash("Re-auction rounds must be 0-10.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        if reauction_pct is None or not (0 <= reauction_pct <= 100):
            flash("Re-auction reduction must be 0-100%.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        if bid_increment is None or bid_increment < 0:
            flash("Bid increment must be a non-negative integer.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))

        season_teams = DBSeasonTeam.query.filter_by(season_id=season.id).all()

        # Pre-parse any per-team budgets posted with the form (only applied in custom mode).
        custom_budgets = {}
        if budget_mode == "custom":
            for st in season_teams:
                raw = request.form.get(f"team_budget_{st.id}")
                if raw is None:
                    continue
                val = _parse_int(raw, None)
                if val is None or val < 0:
                    flash(f"Budget for '{st.display_name}' must be a non-negative integer.", "error")
                    return redirect(url_for("auction_setup", season_id=season.id))
                custom_budgets[st.id] = val

        try:
            auction.budget_mode = budget_mode
            auction.uniform_budget = uniform_budget
            auction.min_players_per_team = min_players
            auction.max_players_per_team = max_players
            auction.per_player_timer_seconds = per_player_timer
            auction.draft_pick_timer_seconds = draft_pick_timer
            auction.category_order_mode = cat_order_mode
            auction.reauction_rounds = reauction_rounds
            auction.reauction_price_reduction_pct = reauction_pct
            auction.bid_increment = bid_increment

            # Propagate purses to teams based on mode.
            if budget_mode == "uniform":
                for st in season_teams:
                    st.custom_budget = None
                    st.purse_remaining = uniform_budget
            else:
                for st in season_teams:
                    if st.id in custom_budgets:
                        st.custom_budget = custom_budgets[st.id]
                        st.purse_remaining = custom_budgets[st.id]

            db.session.commit()
            flash("Auction configuration saved.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_config")
            flash("Could not save configuration.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    @app.route("/season-teams/<int:season_team_id>/budget", methods=["POST"])
    @login_required
    def season_team_budget(season_team_id):
        st = DBSeasonTeam.query.get(season_team_id)
        if st is None:
            abort(404)
        season, _ = _own_season_or_404(st.season_id)
        _require_editable(season)
        auction = _get_or_create_auction(season)
        if auction.budget_mode != "custom":
            flash("Switch to custom budget mode to edit per-team budgets.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))

        custom = _parse_int(request.form.get("custom_budget"), 0)
        if custom is None or custom < 0:
            flash("Budget must be a non-negative integer.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))

        try:
            st.custom_budget = custom
            st.purse_remaining = custom
            db.session.commit()
            flash(f"Budget set for {st.display_name}.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="season_team_budget")
            flash("Could not set budget.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    # ─── Categories ─────────────────────────────────────────────────────────

    @app.route("/seasons/<int:season_id>/auction/categories/create", methods=["POST"])
    @login_required
    def auction_category_create(season_id):
        season, _ = _own_season_or_404(season_id)
        _require_editable(season)
        auction = _get_or_create_auction(season)

        name = (request.form.get("name") or "").strip()
        default_price = _parse_int(request.form.get("default_base_price"), None)
        max_players_raw = request.form.get("max_players")
        if max_players_raw is None or max_players_raw == "":
            max_players = None
        else:
            max_players = _parse_int(max_players_raw, None)

        if not name:
            flash("Category name is required.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        if DBAuctionCategory.query.filter_by(auction_id=auction.id, name=name).first():
            flash(f"A category named '{name}' already exists.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        if season.auction_mode == "traditional" and (default_price is None or default_price < 0):
            flash("Default base price is required for traditional auctions.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        if max_players is not None and max_players < 0:
            flash("Max players in category must be non-negative.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))

        try:
            max_order = db.session.query(func.coalesce(func.max(DBAuctionCategory.display_order), 0))\
                .filter(DBAuctionCategory.auction_id == auction.id).scalar()
            cat = DBAuctionCategory(
                auction_id=auction.id,
                name=name,
                default_base_price=default_price,
                max_players=max_players,
                display_order=(max_order or 0) + 10,
            )
            db.session.add(cat)
            db.session.commit()
            flash(f"Category '{name}' added.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_category_create")
            flash("Could not create category.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    @app.route("/auction-categories/<int:cat_id>/edit", methods=["POST"])
    @login_required
    def auction_category_edit(cat_id):
        cat, auction, season = _own_category(cat_id)
        _require_editable(season)

        name = (request.form.get("name") or "").strip()
        default_price = _parse_int(request.form.get("default_base_price"), None)
        max_players_raw = request.form.get("max_players")
        if max_players_raw is None or max_players_raw == "":
            max_players = None
        else:
            max_players = _parse_int(max_players_raw, None)

        if not name:
            flash("Category name is required.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        collision = (
            DBAuctionCategory.query
            .filter(DBAuctionCategory.auction_id == auction.id,
                    DBAuctionCategory.name == name,
                    DBAuctionCategory.id != cat.id)
            .first()
        )
        if collision:
            flash(f"Another category named '{name}' already exists.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        if max_players is not None and max_players < 0:
            flash("Max players in category must be non-negative.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        if max_players is not None:
            current_count = DBAuctionPlayer.query.filter_by(category_id=cat.id).count()
            if current_count > max_players:
                flash(
                    f"Cannot lower the cap to {max_players}: '{cat.name}' already has {current_count} curated players. "
                    "Remove players first.",
                    "error",
                )
                return redirect(url_for("auction_setup", season_id=season.id))

        try:
            cat.name = name
            cat.default_base_price = default_price
            cat.max_players = max_players
            db.session.commit()
            flash(f"Category '{name}' updated.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_category_edit")
            flash("Could not update category.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    @app.route("/auction-categories/<int:cat_id>/delete", methods=["POST"])
    @login_required
    def auction_category_delete(cat_id):
        cat, auction, season = _own_category(cat_id)
        _require_editable(season)
        try:
            db.session.delete(cat)
            db.session.commit()
            flash(f"Category '{cat.name}' deleted.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_category_delete")
            flash("Could not delete category.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    @app.route("/auction-categories/<int:cat_id>/move", methods=["POST"])
    @login_required
    def auction_category_move(cat_id):
        cat, auction, season = _own_category(cat_id)
        _require_editable(season)
        direction = (request.form.get("direction") or "").strip()
        if direction not in ("up", "down"):
            flash("Invalid direction.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))

        siblings = (
            DBAuctionCategory.query
            .filter_by(auction_id=auction.id)
            .order_by(DBAuctionCategory.display_order.asc(), DBAuctionCategory.id.asc())
            .all()
        )
        idx = next((i for i, s in enumerate(siblings) if s.id == cat.id), None)
        if idx is None:
            return redirect(url_for("auction_setup", season_id=season.id))
        swap_idx = idx - 1 if direction == "up" else idx + 1
        if not (0 <= swap_idx < len(siblings)):
            return redirect(url_for("auction_setup", season_id=season.id))

        try:
            a, b = siblings[idx], siblings[swap_idx]
            a.display_order, b.display_order = b.display_order, a.display_order
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_category_move")
            flash("Could not reorder.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    @app.route("/seasons/<int:season_id>/auction/categories/randomize", methods=["POST"])
    @login_required
    def auction_categories_randomize(season_id):
        season, _ = _own_season_or_404(season_id)
        _require_editable(season)
        auction = _get_or_create_auction(season)
        cats = DBAuctionCategory.query.filter_by(auction_id=auction.id).all()
        if len(cats) < 2:
            flash("Need at least two categories to randomize.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        try:
            random.shuffle(cats)
            for i, c in enumerate(cats):
                c.display_order = (i + 1) * 10
            auction.category_order_mode = "random"
            db.session.commit()
            flash("Category order randomized.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_categories_randomize")
            flash("Could not randomize.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    # ─── Players ────────────────────────────────────────────────────────────

    @app.route("/seasons/<int:season_id>/auction/players/add", methods=["POST"])
    @login_required
    def auction_players_add(season_id):
        season, _ = _own_season_or_404(season_id)
        _require_editable(season)
        auction = _get_or_create_auction(season)

        category_id = _parse_int(request.form.get("category_id"), None)
        refs = request.form.getlist("refs")  # list of 'master:N' / 'user:N'
        if not category_id:
            flash("Choose a category first.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        cat = DBAuctionCategory.query.get(category_id)
        if cat is None or cat.auction_id != auction.id:
            abort(404)
        if not refs:
            flash("Select at least one player to add.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))

        existing = _curated_refs(auction.id)
        # Index pool for snapshotting.
        pool = _get_pool(current_user.id)
        pool_by_ref = {p["ref"]: p for p in pool}

        # Enforce category pool-size cap.
        cat_count = DBAuctionPlayer.query.filter_by(category_id=cat.id).count()
        cap = cat.max_players
        remaining = (cap - cat_count) if cap is not None else None

        added = 0
        skipped = 0
        try:
            for ref in refs:
                if ref in existing or ref not in pool_by_ref:
                    skipped += 1
                    continue
                if remaining is not None and remaining <= 0:
                    skipped += 1
                    continue
                src = pool_by_ref[ref]
                ap = DBAuctionPlayer(
                    auction_id=auction.id,
                    category_id=cat.id,
                    master_player_id=src["id"] if src["source"] == "master" else None,
                    user_player_id=src["id"] if src["source"] == "user" else None,
                    name=src["name"],
                    role=src["role"],
                    batting_rating=src["batting_rating"] or 50,
                    bowling_rating=src["bowling_rating"] or 50,
                    fielding_rating=src["fielding_rating"] or 50,
                    batting_hand=src["batting_hand"],
                    bowling_type=src["bowling_type"],
                    bowling_hand=src["bowling_hand"],
                )
                db.session.add(ap)
                existing.add(ref)
                added += 1
                if remaining is not None:
                    remaining -= 1
            db.session.commit()
            msg = f"Added {added} player(s) to '{cat.name}'."
            if skipped:
                reason = "already curated, not found, or category cap reached"
                msg += f" Skipped {skipped} ({reason})."
            flash(msg, "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_players_add")
            flash("Could not add players.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    @app.route("/auction-players/<int:ap_id>/base-price", methods=["POST"])
    @login_required
    def auction_player_base_price(ap_id):
        ap, auction, season = _own_ap(ap_id)
        _require_editable(season)
        raw = request.form.get("base_price_override")
        if raw is None or raw == "":
            override = None
        else:
            override = _parse_int(raw, None)
            if override is None or override < 0:
                flash("Base price must be a non-negative integer or blank.", "error")
                return redirect(url_for("auction_setup", season_id=season.id))
        try:
            ap.base_price_override = override
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_player_base_price")
            flash("Could not set base price.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    @app.route("/auction-players/<int:ap_id>/category", methods=["POST"])
    @login_required
    def auction_player_category(ap_id):
        ap, auction, season = _own_ap(ap_id)
        _require_editable(season)
        new_cat_id = _parse_int(request.form.get("category_id"), None)
        if new_cat_id is None:
            return redirect(url_for("auction_setup", season_id=season.id))
        new_cat = DBAuctionCategory.query.get(new_cat_id)
        if new_cat is None or new_cat.auction_id != auction.id:
            abort(404)
        if new_cat.id == ap.category_id:
            return redirect(url_for("auction_setup", season_id=season.id))
        if new_cat.max_players is not None:
            current_count = DBAuctionPlayer.query.filter_by(category_id=new_cat.id).count()
            if current_count >= new_cat.max_players:
                flash(f"Category '{new_cat.name}' is full ({new_cat.max_players} max).", "error")
                return redirect(url_for("auction_setup", season_id=season.id))
        try:
            ap.category_id = new_cat.id
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_player_category")
            flash("Could not move player.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    @app.route("/auction-players/<int:ap_id>/delete", methods=["POST"])
    @login_required
    def auction_player_delete(ap_id):
        ap, auction, season = _own_ap(ap_id)
        _require_editable(season)
        try:
            db.session.delete(ap)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_player_delete")
            flash("Could not remove player.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    # ─── Finalize / Reopen ──────────────────────────────────────────────────

    @app.route("/seasons/<int:season_id>/auction/finalize", methods=["POST"])
    @login_required
    def auction_finalize(season_id):
        season, _ = _own_season_or_404(season_id)
        if season.status in ("auction_live", "auction_paused", "auction_done", "archived"):
            flash("Auction has already started or ended.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        auction = _get_or_create_auction(season)
        categories = (
            DBAuctionCategory.query
            .filter_by(auction_id=auction.id)
            .order_by(DBAuctionCategory.display_order.asc(), DBAuctionCategory.id.asc())
            .all()
        )
        players = DBAuctionPlayer.query.filter_by(auction_id=auction.id).all()
        season_teams = DBSeasonTeam.query.filter_by(season_id=season.id).all()

        errors = _finalize_errors(season, auction, categories, players, season_teams)
        if errors:
            for e in errors:
                flash(e, "error")
            return redirect(url_for("auction_setup", season_id=season.id))

        try:
            # Freeze category_order to the current display_order.
            auction.category_order = json.dumps([c.id for c in categories])
            season.status = "auction_ready"
            db.session.commit()
            flash("Auction finalized. Ready to start.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_finalize")
            flash("Could not finalize auction.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    @app.route("/seasons/<int:season_id>/auction/reopen", methods=["POST"])
    @login_required
    def auction_reopen(season_id):
        season, _ = _own_season_or_404(season_id)
        if season.status != "auction_ready":
            flash("Auction is not in a finalized state.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        try:
            season.status = "setup"
            auction = _get_or_create_auction(season)
            auction.category_order = "[]"
            db.session.commit()
            flash("Auction reopened for editing.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_reopen")
            flash("Could not reopen.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    # ─── Finalize validation ────────────────────────────────────────────────

    def _finalize_errors(season, auction, categories, players, season_teams):
        errs = []
        if len(season_teams) < 2:
            errs.append("Need at least 2 teams.")
        if len(categories) < 1:
            errs.append("Need at least 1 category.")
        if len(players) < 1:
            errs.append("Curate at least 1 player.")
        # Each team must be able to fill the minimum squad size from the pool.
        required_pool = len(season_teams) * auction.min_players_per_team
        if len(players) < required_pool:
            errs.append(f"Pool too small: need ≥ {required_pool} players (teams × min-per-team), have {len(players)}.")
        # Budgets
        if auction.budget_mode == "uniform":
            if auction.uniform_budget is None or auction.uniform_budget <= 0:
                errs.append("Uniform budget must be > 0.")
        else:  # custom
            zero_budget = [st for st in season_teams if (st.custom_budget or 0) <= 0]
            if zero_budget:
                names = ", ".join(st.display_name for st in zero_budget)
                errs.append(f"Custom budget missing for: {names}")
        # Category defaults (traditional only)
        if season.auction_mode == "traditional":
            missing = [c.name for c in categories if c.default_base_price is None or c.default_base_price < 0]
            if missing:
                errs.append(f"Categories missing default base price: {', '.join(missing)}")
        return errs
