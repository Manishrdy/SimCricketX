"""
AUCTION-REDESIGN Phase 2 — Auction setup routes.

Configures the auction attached to a season: budgets, categories, player
curation from the master + user pool, bid-increment tiers, timers, and
re-auction rules. No runtime (live bidding) here — that lands in Phase 4.
"""

import io
import json
import random
from datetime import datetime

from flask import Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from utils.exception_tracker import log_exception


VALID_BUDGET_MODES = ("uniform", "custom")
VALID_CATEGORY_ORDER_MODES = ("manual", "random")
SETUP_EDITABLE_STATUSES = {"setup", "teams_ready", "auction_ready"}  # can still be reopened/tweaked before start
STRICT_SETUP_EDITABLE_STATUSES = {"setup", "teams_ready"}
STARTER_CATEGORIES = [
    ("Platinum", 300),
    ("Gold", 200),
    ("Silver", 120),
    ("Emerging", 80),
]


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

    # Shared ownership guards — single source of truth across
    # league_routes / auction_routes / auction_realtime.
    from routes._auction_guards import make_guards
    guards = make_guards(
        DBLeague=DBLeague, DBSeason=DBSeason, DBSeasonTeam=DBSeasonTeam,
        DBAuction=DBAuction, DBAuctionCategory=DBAuctionCategory,
        DBAuctionPlayer=DBAuctionPlayer,
    )
    _own_season_or_404 = guards.own_season
    _own_category = guards.own_category
    _own_ap = guards.own_ap

    def _get_or_create_auction(season):
        auc = DBAuction.query.filter_by(season_id=season.id).first()
        if auc is None:
            auc = DBAuction(season_id=season.id)
            db.session.add(auc)
            db.session.commit()
        return auc

    def _require_editable(season):
        """Block mutations once the live auction has started or finished."""
        strict_lock = bool(current_app.config.get("AUCTION_SIMPLIFIED_FLOW", False))
        editable_statuses = STRICT_SETUP_EDITABLE_STATUSES if strict_lock else SETUP_EDITABLE_STATUSES
        if season.status not in editable_statuses:
            msg = f"Season status '{season.status}' does not allow setup edits."
            if strict_lock and season.status == "auction_ready":
                msg += " Reopen setup first."
            abort(409, description=msg)

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

    def _starter_price_for(category_name):
        key = (category_name or "").strip().lower()
        for name, price in STARTER_CATEGORIES:
            if key == name.lower():
                return price
        return 100

    def _apply_starter_setup(season, auction):
        """Populate starter defaults for quicker first-time setup."""
        created_categories = 0
        updated_fields = 0

        if auction.budget_mode == "uniform" and (auction.uniform_budget is None or auction.uniform_budget <= 0):
            auction.uniform_budget = 1000
            updated_fields += 1

        existing = (
            DBAuctionCategory.query
            .filter_by(auction_id=auction.id)
            .order_by(DBAuctionCategory.display_order.asc(), DBAuctionCategory.id.asc())
            .all()
        )
        existing_by_name = {(c.name or "").strip().lower() for c in existing}
        max_order = db.session.query(func.coalesce(func.max(DBAuctionCategory.display_order), 0))\
            .filter(DBAuctionCategory.auction_id == auction.id).scalar() or 0
        next_order = int(max_order)

        for category_name, default_price in STARTER_CATEGORIES:
            if category_name.lower() in existing_by_name:
                continue
            next_order += 10
            db.session.add(
                DBAuctionCategory(
                    auction_id=auction.id,
                    name=category_name,
                    default_base_price=(default_price if season.auction_mode == "traditional" else None),
                    max_players=15,
                    display_order=next_order,
                )
            )
            created_categories += 1

        # In traditional mode, fill missing default prices with safe starter values.
        if season.auction_mode == "traditional":
            all_categories = DBAuctionCategory.query.filter_by(auction_id=auction.id).all()
            for cat in all_categories:
                if cat.default_base_price is None or cat.default_base_price < 0:
                    cat.default_base_price = _starter_price_for(cat.name)
                    updated_fields += 1

        return created_categories, updated_fields

    def _curated_refs(auction_id):
        """Returns a set of 'master:N' / 'user:N' refs already in the curated pool."""
        refs = set()
        for ap in DBAuctionPlayer.query.filter_by(auction_id=auction_id).all():
            if ap.master_player_id is not None:
                refs.add(f"master:{ap.master_player_id}")
            elif ap.user_player_id is not None:
                refs.add(f"user:{ap.user_player_id}")
        return refs

    def _autofill_to_min_pool(season, auction, user_id):
        """Best-effort fill from available pool to satisfy minimum finalize requirement."""
        season_teams = DBSeasonTeam.query.filter_by(season_id=season.id).all()
        categories = (
            DBAuctionCategory.query
            .filter_by(auction_id=auction.id)
            .order_by(DBAuctionCategory.display_order.asc(), DBAuctionCategory.id.asc())
            .all()
        )
        if not categories:
            return {"status": "missing_categories"}

        existing_players = DBAuctionPlayer.query.filter_by(auction_id=auction.id).all()
        required_pool = len(season_teams) * int(auction.min_players_per_team or 0)
        if required_pool < 1:
            required_pool = 1
        if len(existing_players) >= required_pool:
            return {
                "status": "already_sufficient",
                "added": 0,
                "final_count": len(existing_players),
                "required_pool": required_pool,
            }

        curated_refs = _curated_refs(auction.id)
        pool_available = [p for p in _get_pool(user_id) if p["ref"] not in curated_refs]
        if not pool_available:
            return {
                "status": "no_available",
                "added": 0,
                "final_count": len(existing_players),
                "required_pool": required_pool,
            }

        cat_counts = {c.id: DBAuctionPlayer.query.filter_by(category_id=c.id).count() for c in categories}

        def _pick_category(cursor):
            for offset in range(len(categories)):
                idx = (cursor + offset) % len(categories)
                cat = categories[idx]
                cap = cat.max_players
                if cap is None or cat_counts.get(cat.id, 0) < cap:
                    return idx, cat
            return None, None

        need = required_pool - len(existing_players)
        added = 0
        cat_cursor = 0
        for pool_row in pool_available:
            if added >= need:
                break
            cat_idx, target_cat = _pick_category(cat_cursor)
            if target_cat is None:
                break
            cat_cursor = (cat_idx + 1) % len(categories)

            source = pool_row["source"]
            raw_id = pool_row["id"]
            db.session.add(
                DBAuctionPlayer(
                    auction_id=auction.id,
                    category_id=target_cat.id,
                    master_player_id=(raw_id if source == "master" else None),
                    user_player_id=(raw_id if source == "user" else None),
                    name=pool_row["name"] or "",
                    role=pool_row.get("role"),
                    batting_rating=pool_row.get("batting_rating"),
                    bowling_rating=pool_row.get("bowling_rating"),
                    fielding_rating=pool_row.get("fielding_rating"),
                    batting_hand=pool_row.get("batting_hand"),
                    bowling_type=pool_row.get("bowling_type"),
                    bowling_hand=pool_row.get("bowling_hand"),
                )
            )
            cat_counts[target_cat.id] = cat_counts.get(target_cat.id, 0) + 1
            added += 1

        final_count = len(existing_players) + added
        status = "filled" if final_count >= required_pool else "short"
        return {
            "status": status,
            "added": added,
            "final_count": final_count,
            "required_pool": required_pool,
        }

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
        simplified_flow = bool(current_app.config.get("AUCTION_SIMPLIFIED_FLOW", False))
        setup_progress = (
            _build_setup_progress(season, auction, categories, players, season_teams, errors)
            if simplified_flow else None
        )

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
            strict_finalize_lock=simplified_flow,
            setup_progress=setup_progress,
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

    @app.route("/seasons/<int:season_id>/auction/quickstart", methods=["POST"])
    @login_required
    def auction_quickstart(season_id):
        season, _ = _own_season_or_404(season_id)
        if not bool(current_app.config.get("AUCTION_SIMPLIFIED_FLOW", False)):
            flash("Starter setup is available only in simplified flow mode.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        _require_editable(season)
        auction = _get_or_create_auction(season)

        try:
            created_categories, updated_fields = _apply_starter_setup(season, auction)
            db.session.commit()
            if created_categories == 0 and updated_fields == 0:
                flash("Starter setup already applied.", "success")
            else:
                flash(
                    f"Starter setup applied. Added {created_categories} categories and updated {updated_fields} field(s).",
                    "success",
                )
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_quickstart")
            flash("Could not apply starter setup.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    @app.route("/seasons/<int:season_id>/auction/autofill-min-pool", methods=["POST"])
    @login_required
    def auction_autofill_min_pool(season_id):
        season, _ = _own_season_or_404(season_id)
        if not bool(current_app.config.get("AUCTION_SIMPLIFIED_FLOW", False)):
            flash("Auto-fill is available only in simplified flow mode.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        _require_editable(season)
        auction = _get_or_create_auction(season)

        try:
            fill = _autofill_to_min_pool(season, auction, current_user.id)
            status = fill.get("status")
            if status == "missing_categories":
                flash("Create at least one category before auto-filling players.", "error")
                return redirect(url_for("auction_setup", season_id=season.id))
            if status == "already_sufficient":
                flash(
                    f"Pool already has {fill['final_count']} players (minimum required: {fill['required_pool']}).",
                    "success",
                )
                return redirect(url_for("auction_setup", season_id=season.id))
            if status == "no_available":
                flash("No available players left to auto-fill.", "error")
                return redirect(url_for("auction_setup", season_id=season.id))

            db.session.commit()
            if fill["status"] == "filled":
                flash(
                    f"Auto-filled {fill['added']} players. Pool now meets minimum "
                    f"({fill['final_count']}/{fill['required_pool']}).",
                    "success",
                )
            else:  # short
                flash(
                    f"Auto-filled {fill['added']} players, but pool is still short "
                    f"({fill['final_count']}/{fill['required_pool']}). "
                    "Add more players or increase category caps.",
                    "error",
                )
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_autofill_min_pool")
            flash("Could not auto-fill players.", "error")
        return redirect(url_for("auction_setup", season_id=season.id))

    @app.route("/seasons/<int:season_id>/auction/auto-prepare", methods=["POST"])
    @login_required
    def auction_auto_prepare(season_id):
        season, _ = _own_season_or_404(season_id)
        if not bool(current_app.config.get("AUCTION_SIMPLIFIED_FLOW", False)):
            flash("Auto-prepare is available only in simplified flow mode.", "error")
            return redirect(url_for("auction_setup", season_id=season.id))
        _require_editable(season)
        auction = _get_or_create_auction(season)

        try:
            created_categories, updated_fields = _apply_starter_setup(season, auction)
            fill = _autofill_to_min_pool(season, auction, current_user.id)
            status = fill.get("status")
            if status == "missing_categories":
                # Should not normally happen after starter setup, but keep fail-safe.
                db.session.rollback()
                flash("Auto-prepare needs at least one category.", "error")
                return redirect(url_for("auction_setup", season_id=season.id))

            db.session.commit()
            added_players = int(fill.get("added", 0))
            flash(
                f"Auto-prepare applied: {created_categories} category(ies) added, "
                f"{updated_fields} default field(s) updated, {added_players} player(s) auto-filled.",
                "success",
            )

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
                flash(f"{len(errors)} finalize blocker(s) remain.", "error")
                for msg in errors[:2]:
                    flash(msg, "error")
            else:
                flash("Setup is ready to finalize.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_auto_prepare")
            flash("Could not auto-prepare setup.", "error")
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

    # ─── Phase 4: organizer live-control routes ─────────────────────────────

    from routes import auction_runtime as runtime
    from routes.auction_runtime import RuntimeError_ as _RuntimeOpError

    def _flash_runtime(exc):
        flash(exc.message or exc.code, "error")

    @app.route("/seasons/<int:season_id>/auction/start", methods=["POST"])
    @login_required
    def auction_start(season_id):
        season, _ = _own_season_or_404(season_id)
        auction = _get_or_create_auction(season)
        try:
            runtime.start_auction(season, auction, actor_label=current_user.id)
            flash("Auction is live. Open the next player when ready.", "success")
        except _RuntimeOpError as exc:
            _flash_runtime(exc)
        except Exception as exc:
            log_exception(exc, source="auction_start")
            flash("Could not start auction.", "error")
        return redirect(url_for("auction_live", season_id=season.id))

    @app.route("/seasons/<int:season_id>/auction/pause", methods=["POST"])
    @login_required
    def auction_pause(season_id):
        season, _ = _own_season_or_404(season_id)
        auction = _get_or_create_auction(season)
        try:
            runtime.pause_auction(season, auction, actor_label=current_user.id)
        except _RuntimeOpError as exc:
            _flash_runtime(exc)
        except Exception as exc:
            log_exception(exc, source="auction_pause")
            flash("Could not pause auction.", "error")
        return redirect(url_for("auction_live", season_id=season.id))

    @app.route("/seasons/<int:season_id>/auction/resume", methods=["POST"])
    @login_required
    def auction_resume(season_id):
        season, _ = _own_season_or_404(season_id)
        auction = _get_or_create_auction(season)
        try:
            runtime.resume_auction(season, auction, actor_label=current_user.id)
        except _RuntimeOpError as exc:
            _flash_runtime(exc)
        except Exception as exc:
            log_exception(exc, source="auction_resume")
            flash("Could not resume auction.", "error")
        return redirect(url_for("auction_live", season_id=season.id))

    @app.route("/seasons/<int:season_id>/auction/complete", methods=["POST"])
    @login_required
    def auction_complete(season_id):
        season, _ = _own_season_or_404(season_id)
        auction = _get_or_create_auction(season)
        try:
            runtime.complete_auction(season, auction, actor_label=current_user.id)
            flash("Auction marked complete.", "success")
        except _RuntimeOpError as exc:
            _flash_runtime(exc)
        except Exception as exc:
            log_exception(exc, source="auction_complete")
            flash("Could not complete auction.", "error")
        return redirect(url_for("auction_live", season_id=season.id))

    @app.route("/seasons/<int:season_id>/auction/next-lot", methods=["POST"])
    @login_required
    def auction_next_lot(season_id):
        season, _ = _own_season_or_404(season_id)
        auction = _get_or_create_auction(season)
        try:
            runtime.open_next_lot(season, auction, actor_label=current_user.id)
        except _RuntimeOpError as exc:
            _flash_runtime(exc)
        except Exception as exc:
            log_exception(exc, source="auction_next_lot")
            flash("Could not open next lot.", "error")
        return redirect(url_for("auction_live", season_id=season.id))

    @app.route("/seasons/<int:season_id>/auction/next-round", methods=["POST"])
    @login_required
    def auction_next_round(season_id):
        season, _ = _own_season_or_404(season_id)
        auction = _get_or_create_auction(season)
        try:
            n = runtime.next_round(season, auction, actor_label=current_user.id)
            flash(f"Round {auction.current_round} started — {n} player(s) re-listed.", "success")
        except _RuntimeOpError as exc:
            _flash_runtime(exc)
        except Exception as exc:
            log_exception(exc, source="auction_next_round")
            flash("Could not advance round.", "error")
        return redirect(url_for("auction_live", season_id=season.id))

    @app.route("/seasons/<int:season_id>/auction/force-sell", methods=["POST"])
    @login_required
    def auction_force_sell(season_id):
        season, _ = _own_season_or_404(season_id)
        auction = _get_or_create_auction(season)
        try:
            runtime.force_sell(season, auction, actor_label=current_user.id)
        except _RuntimeOpError as exc:
            _flash_runtime(exc)
        except Exception as exc:
            log_exception(exc, source="auction_force_sell")
            flash("Could not force-sell.", "error")
        return redirect(url_for("auction_live", season_id=season.id))

    @app.route("/seasons/<int:season_id>/auction/force-unsold", methods=["POST"])
    @login_required
    def auction_force_unsold(season_id):
        season, _ = _own_season_or_404(season_id)
        auction = _get_or_create_auction(season)
        try:
            runtime.force_unsold(season, auction, actor_label=current_user.id)
        except _RuntimeOpError as exc:
            _flash_runtime(exc)
        except Exception as exc:
            log_exception(exc, source="auction_force_unsold")
            flash("Could not mark unsold.", "error")
        return redirect(url_for("auction_live", season_id=season.id))

    @app.route("/seasons/<int:season_id>/auction/reverse-sale", methods=["POST"])
    @login_required
    def auction_reverse_sale(season_id):
        season, _ = _own_season_or_404(season_id)
        auction = _get_or_create_auction(season)
        try:
            runtime.reverse_last_sale(season, auction, actor_label=current_user.id)
            flash("Last sale reversed; lot reopened.", "success")
        except _RuntimeOpError as exc:
            _flash_runtime(exc)
        except Exception as exc:
            log_exception(exc, source="auction_reverse_sale")
            flash("Could not reverse sale.", "error")
        return redirect(url_for("auction_live", season_id=season.id))

    # ─── Phase 6: roster sync + export ──────────────────────────────────────

    from routes import auction_sync as sync

    @app.route("/seasons/<int:season_id>/auction/sync-rosters", methods=["POST"])
    @login_required
    def auction_sync_rosters(season_id):
        """Manual re-sync — useful if the organizer tweaks captain/WK in
        TeamProfile after the auto-sync, or re-runs after deleting a team."""
        season, _ = _own_season_or_404(season_id)
        auction = _get_or_create_auction(season)
        if season.status != "auction_done":
            flash("Rosters can only be synced after the auction is complete.", "error")
            return redirect(url_for("auction_live", season_id=season.id))
        try:
            report = runtime.run_roster_sync(season, auction)
            ready = sum(1 for r in report if r["publish_ready"])
            runtime._log_audit(
                auction, "roster.synced",
                {"teams_synced": len(report), "teams_ready": ready, "manual": True},
                actor_type="organizer", actor_label=current_user.id,
            )
            flash(f"Rosters synced for {len(report)} team(s). {ready} ready to publish.", "success")
        except Exception as exc:
            log_exception(exc, source="auction_sync_rosters")
            flash("Roster sync failed.", "error")
        return redirect(url_for("auction_live", season_id=season.id))

    @app.route("/seasons/<int:season_id>/auction/export.json")
    @login_required
    def auction_export_json(season_id):
        season, _ = _own_season_or_404(season_id)
        auction = _get_or_create_auction(season)
        data = sync.export_rosters_json(
            db, season, auction,
            DBSeasonTeam=DBSeasonTeam,
            DBAuctionPlayer=DBAuctionPlayer,
            DBAuctionCategory=DBAuctionCategory,
        )
        filename = f"auction_{season.id}_{season.name}.json".replace(" ", "_")
        resp = jsonify(data)
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp

    @app.route("/seasons/<int:season_id>/auction/create-tournament", methods=["POST"])
    @login_required
    def auction_create_tournament(season_id):
        """Phase 7 — materialize a Tournament + fixtures from the completed
        season so the drafted teams can play matches. Each call creates a new
        tournament; organizers may generate multiple (e.g., a round-robin and
        a knockout) from the same season."""
        season, league = _own_season_or_404(season_id)
        if season.status != "auction_done":
            flash("Tournament can only be created after the auction completes.", "error")
            return redirect(url_for("auction_live", season_id=season.id))

        season_teams = (DBSeasonTeam.query
                        .filter_by(season_id=season.id)
                        .order_by(DBSeasonTeam.id.asc())
                        .all())
        teams = [DBTeam.query.get(st.team_id) for st in season_teams]
        teams = [t for t in teams if t is not None]

        # Readiness gate: every team must have a publish-ready roster for the
        # season's format, otherwise match sim will fail once fixtures run.
        unready = [t.name for t in teams if t.is_draft]
        if unready:
            flash(
                "Some rosters aren't publish-ready: "
                + ", ".join(unready)
                + ". Re-sync rosters or edit the profiles, then try again.",
                "error",
            )
            return redirect(url_for("auction_live", season_id=season.id))

        mode = (request.form.get("mode") or "round_robin").strip()
        try:
            playoff_teams = int(request.form.get("playoff_teams", "4"))
        except (TypeError, ValueError):
            playoff_teams = 4

        name = (request.form.get("name") or "").strip() or f"{league.name} — {season.name}"
        team_ids = [t.id for t in teams]

        try:
            from engine.tournament_engine import TournamentEngine
            engine_inst = TournamentEngine()
            min_teams = engine_inst.MIN_TEAMS.get(mode, 2)
            if len(team_ids) < min_teams:
                flash(
                    f"'{mode}' needs at least {min_teams} teams; this season has {len(team_ids)}.",
                    "error",
                )
                return redirect(url_for("auction_live", season_id=season.id))

            t = engine_inst.create_tournament(
                name=name,
                user_id=current_user.id,
                team_ids=team_ids,
                mode=mode,
                playoff_teams=playoff_teams,
                format_type=season.format,
            )
            runtime._log_audit(
                auction, "tournament.created",
                {"tournament_id": t.id, "tournament_name": t.name,
                 "mode": mode, "team_count": len(team_ids),
                 "playoff_teams": playoff_teams},
                actor_type="organizer", actor_label=current_user.id,
            )
            flash(f"Tournament '{t.name}' created from season {season.name}.", "success")
            return redirect(url_for("tournament_dashboard", tournament_id=t.id))
        except ValueError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="auction_create_tournament")
            flash("Could not create tournament.", "error")
        return redirect(url_for("auction_live", season_id=season.id))

    @app.route("/seasons/<int:season_id>/auction/history.json")
    @login_required
    def auction_history_json(season_id):
        """Organizer-only read-back of the bid log and audit trail."""
        from database.models import AuctionBid as _DBAuctionBid, AuctionAuditLog as _DBAuctionAuditLog
        season, _ = _own_season_or_404(season_id)
        auction = _get_or_create_auction(season)
        try:
            limit = max(1, min(500, int(request.args.get("limit", 100))))
        except (TypeError, ValueError):
            limit = 100

        bids = (_DBAuctionBid.query
                .filter_by(auction_id=auction.id)
                .order_by(_DBAuctionBid.created_at.desc(), _DBAuctionBid.id.desc())
                .limit(limit)
                .all())
        audit = (_DBAuctionAuditLog.query
                 .filter_by(auction_id=auction.id)
                 .order_by(_DBAuctionAuditLog.created_at.desc(), _DBAuctionAuditLog.id.desc())
                 .limit(limit)
                 .all())

        # Resolve nicer labels via in-process lookup (one query each).
        ap_by_id = {}
        st_by_id = {}
        if bids:
            ap_ids = {b.auction_player_id for b in bids if b.auction_player_id}
            st_ids = {b.season_team_id for b in bids if b.season_team_id}
            if ap_ids:
                ap_by_id = {p.id: p for p in DBAuctionPlayer.query.filter(DBAuctionPlayer.id.in_(ap_ids)).all()}
            if st_ids:
                st_by_id = {s.id: s for s in DBSeasonTeam.query.filter(DBSeasonTeam.id.in_(st_ids)).all()}

        return jsonify({
            "auction_id": auction.id,
            "bids": [{
                "id": b.id,
                "auction_player_id": b.auction_player_id,
                "player_name": ap_by_id.get(b.auction_player_id).name if ap_by_id.get(b.auction_player_id) else None,
                "season_team_id": b.season_team_id,
                "team_name": st_by_id.get(b.season_team_id).display_name if st_by_id.get(b.season_team_id) else None,
                "amount": int(b.amount),
                "round": int(b.round),
                "created_at": b.created_at.isoformat() if b.created_at else None,
            } for b in bids],
            "audit": [{
                "id": a.id,
                "action": a.action,
                "actor_type": a.actor_type,
                "actor_label": a.actor_label,
                "payload": json.loads(a.payload) if a.payload else None,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            } for a in audit],
        })

    @app.route("/seasons/<int:season_id>/auction/export.csv")
    @login_required
    def auction_export_csv(season_id):
        season, _ = _own_season_or_404(season_id)
        auction = _get_or_create_auction(season)
        csv_text = sync.export_rosters_csv(
            db, season, auction,
            DBSeasonTeam=DBSeasonTeam,
            DBAuctionPlayer=DBAuctionPlayer,
            DBAuctionCategory=DBAuctionCategory,
        )
        filename = f"auction_{season.id}_{season.name}.csv".replace(" ", "_")
        return Response(
            csv_text,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ─── Finalize validation ────────────────────────────────────────────────

    def _build_setup_progress(season, auction, categories, players, season_teams, finalize_errors):
        """Build a guided checklist for the setup page without changing rules."""
        team_count = len(season_teams)
        category_count = len(categories)
        player_count = len(players)
        min_players = max(0, int(auction.min_players_per_team or 0))
        required_pool = max(1, team_count * min_players)

        teams_ready = team_count >= 2
        teams_detail = f"{team_count} team(s) ready." if teams_ready else f"Need 2+ teams, currently {team_count}."

        minmax_ready = (
            auction.min_players_per_team is not None
            and auction.max_players_per_team is not None
            and int(auction.min_players_per_team) >= 1
            and int(auction.max_players_per_team) >= int(auction.min_players_per_team)
        )
        if auction.budget_mode == "uniform":
            budget_ready = auction.uniform_budget is not None and int(auction.uniform_budget) > 0
            config_detail = (
                f"Uniform budget ${int(auction.uniform_budget)}."
                if budget_ready else "Set uniform budget above 0."
            )
        else:
            missing_custom = [st.display_name for st in season_teams if (st.custom_budget or 0) <= 0]
            budget_ready = len(missing_custom) == 0
            if budget_ready:
                config_detail = f"Custom budgets set for {team_count} team(s)."
            else:
                names = ", ".join(missing_custom[:3])
                if len(missing_custom) > 3:
                    names += f" +{len(missing_custom) - 3} more"
                config_detail = f"Set custom budgets for: {names}."
        config_ready = bool(budget_ready and minmax_ready)
        if config_ready:
            config_detail += f" Min/Max squad: {int(auction.min_players_per_team)}/{int(auction.max_players_per_team)}."
        elif not minmax_ready:
            config_detail += " Fix min/max squad limits."

        missing_price = []
        if season.auction_mode == "traditional":
            missing_price = [c.name for c in categories if c.default_base_price is None or c.default_base_price < 0]
        categories_ready = category_count >= 1 and not missing_price
        if category_count < 1:
            categories_detail = "Need at least 1 category."
        elif missing_price:
            categories_detail = f"Missing default price: {', '.join(missing_price[:3])}."
        else:
            categories_detail = f"{category_count} category(ies) configured."

        players_ready = player_count >= required_pool
        players_detail = (
            f"{player_count} curated player(s); need >= {required_pool}."
            if players_ready else
            f"Need >= {required_pool} curated players, currently {player_count}."
        )

        finalize_ready = len(finalize_errors) == 0
        finalize_detail = "Ready to finalize." if finalize_ready else f"{len(finalize_errors)} blocker(s) left."

        steps = [
            {
                "id": "teams",
                "title": "Teams",
                "ready": teams_ready,
                "detail": teams_detail,
                "action_label": "Open season teams",
                "href": url_for("season_detail", season_id=season.id),
            },
            {
                "id": "config",
                "title": "Configuration",
                "ready": config_ready,
                "detail": config_detail,
                "action_label": "Review configuration",
                "href": "#config",
            },
            {
                "id": "categories",
                "title": "Categories",
                "ready": categories_ready,
                "detail": categories_detail,
                "action_label": "Add categories",
                "href": "#categories",
            },
            {
                "id": "players",
                "title": "Player Pool",
                "ready": players_ready,
                "detail": players_detail,
                "action_label": "Curate players",
                "href": "#players",
            },
            {
                "id": "finalize",
                "title": "Finalize",
                "ready": finalize_ready,
                "detail": finalize_detail,
                "action_label": "Resolve finalize blockers",
                "href": "#top-finalize",
            },
        ]
        next_step = next((s for s in steps if not s["ready"]), None)
        return {"steps": steps, "next_step": next_step}

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
