"""Player pool route registration — admin (MasterPlayer) + user (UserPlayer)."""

import csv
import io
import json

from flask import (
    Response, flash, jsonify, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required
from auth.decorators import admin_required
from utils.exception_tracker import log_exception

VALID_ROLES = {"Batsman", "Bowler", "All-rounder", "Wicketkeeper"}
VALID_BATTING_HANDS = {"Left", "Right", ""}
VALID_BOWLING_TYPES = {
    "Fast", "Fast-medium", "Medium-fast", "Medium",
    "Off spin", "Leg spin", "Finger spin", "Wrist spin", "",
}
VALID_BOWLING_HANDS = {"Left", "Right", ""}

PLAYER_FIELDS = [
    "name", "role", "batting_rating", "bowling_rating", "fielding_rating",
    "batting_hand", "bowling_type", "bowling_hand", "is_captain", "is_wicketkeeper",
]
STRICT_BOOL_TRUE = {"true", "1", "yes"}
STRICT_BOOL_FALSE = {"false", "0", "no"}


def register_player_pool_routes(app, *, db, DBMasterPlayer, DBUserPlayer):

    # ── Validation ───────────────────────────────────────────────────────────

    def _validate_player_dict(d, idx=1, strict_import=False):
        if strict_import:
            if not isinstance(d, dict):
                return None, f"Row {idx}: each row must be an object."
            missing_fields = [f for f in PLAYER_FIELDS if f not in d]
            extra_fields = [k for k in d.keys() if k not in PLAYER_FIELDS]
            if missing_fields:
                return None, f"Row {idx}: missing required field(s): {', '.join(missing_fields)}."
            if extra_fields:
                return None, f"Row {idx}: unexpected field(s): {', '.join(str(v) for v in extra_fields)}."

        name = str(d.get("name", "")).strip()
        if not name:
            return None, f"Row {idx}: name is required."
        role = str(d.get("role", "")).strip()
        if role not in VALID_ROLES:
            return None, f"Row {idx}: invalid role '{role}'."
        bat_raw = d.get("batting_rating", 50)
        bowl_raw = d.get("bowling_rating", 50)
        field_raw = d.get("fielding_rating", 50)
        if strict_import and (str(bat_raw).strip() == "" or str(bowl_raw).strip() == "" or str(field_raw).strip() == ""):
            return None, f"Row {idx}: batting_rating, bowling_rating, and fielding_rating are required."
        try:
            bat = int(bat_raw)
            bowl = int(bowl_raw)
            field = int(field_raw)
        except (TypeError, ValueError):
            return None, f"Row {idx}: ratings must be integers."
        if not (0 <= bat <= 100 and 0 <= bowl <= 100 and 0 <= field <= 100):
            return None, f"Row {idx}: ratings must be between 0 and 100."

        batting_hand = str(d.get("batting_hand", "")).strip()
        bowling_type = str(d.get("bowling_type", "")).strip()
        bowling_hand = str(d.get("bowling_hand", "")).strip()

        if batting_hand and batting_hand not in VALID_BATTING_HANDS:
            return None, f"Row {idx}: invalid batting_hand '{batting_hand}'."
        if bowling_type and bowling_type not in VALID_BOWLING_TYPES:
            return None, f"Row {idx}: invalid bowling_type '{bowling_type}'."
        if bowling_hand and bowling_hand not in VALID_BOWLING_HANDS:
            return None, f"Row {idx}: invalid bowling_hand '{bowling_hand}'."

        if strict_import:
            is_captain, ok_captain = _to_bool_strict(d.get("is_captain"))
            if not ok_captain:
                return None, f"Row {idx}: is_captain must be one of true/false/1/0/yes/no."
            is_wk, ok_wk = _to_bool_strict(d.get("is_wicketkeeper"))
            if not ok_wk:
                return None, f"Row {idx}: is_wicketkeeper must be one of true/false/1/0/yes/no."
        else:
            is_captain = _to_bool(d.get("is_captain", False))
            is_wk = _to_bool(d.get("is_wicketkeeper", False))

        if role == "Wicketkeeper":
            bowling_type = ""
            bowling_hand = ""

        return {
            "name": name, "role": role,
            "batting_rating": bat, "bowling_rating": bowl, "fielding_rating": field,
            "batting_hand": batting_hand, "bowling_type": bowling_type,
            "bowling_hand": bowling_hand,
            "is_captain": is_captain, "is_wicketkeeper": is_wk,
        }, None

    def _to_bool(val):
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("true", "1", "yes")
        return bool(val)

    def _to_bool_strict(val):
        if isinstance(val, bool):
            return val, True
        if isinstance(val, int) and val in (0, 1):
            return bool(val), True
        if isinstance(val, str):
            normalized = val.strip().lower()
            if normalized in STRICT_BOOL_TRUE:
                return True, True
            if normalized in STRICT_BOOL_FALSE:
                return False, True
        return False, False

    def _apply_fields(obj, data):
        for f in PLAYER_FIELDS:
            if f in data:
                setattr(obj, f, data[f])

    # ── Effective pool query ─────────────────────────────────────────────────

    def _get_effective_pool(user_id, search=None, role_filter=None, offset=0, limit=None):
        masters = DBMasterPlayer.query.order_by(DBMasterPlayer.name).all()

        overrides = {}
        customs = []
        if user_id:
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
                pool.append(_to_pool_dict(up, source="override", master_id=mp.id, user_player_id=up.id))
            else:
                pool.append(_to_pool_dict(mp, source="master", master_id=mp.id, user_player_id=None))

        for cp in customs:
            pool.append(_to_pool_dict(cp, source="custom", master_id=None, user_player_id=cp.id))

        if search:
            q = search.lower()
            pool = [p for p in pool if q in p["name"].lower()]
        if role_filter:
            pool = [p for p in pool if p["role"] == role_filter]

        pool.sort(key=lambda p: p["name"].lower())

        total = len(pool)
        if limit is not None:
            pool = pool[offset:offset + limit]

        return pool, total

    def _to_pool_dict(obj, source, master_id, user_player_id):
        return {
            "id": obj.id,
            "source": source,
            "master_player_id": master_id,
            "user_player_id": user_player_id,
            "name": obj.name,
            "role": obj.role or "",
            "batting_rating": obj.batting_rating or 0,
            "bowling_rating": obj.bowling_rating or 0,
            "fielding_rating": obj.fielding_rating or 0,
            "batting_hand": obj.batting_hand or "",
            "bowling_type": obj.bowling_type or "",
            "bowling_hand": obj.bowling_hand or "",
            "is_captain": obj.is_captain or False,
            "is_wicketkeeper": obj.is_wicketkeeper or False,
        }

    # ── Import helpers ───────────────────────────────────────────────────────

    def _parse_json_text(raw):
        try:
            data = json.loads(raw)
            if not isinstance(data, list):
                return None, "JSON must be an array of player objects."
            if not data:
                return None, "JSON is empty."
            return data, None
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return None, f"Invalid JSON data: {exc}"

    def _parse_json_upload(file_storage):
        try:
            raw = file_storage.read().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            return None, f"Invalid JSON file encoding: {exc}"
        return _parse_json_text(raw)

    def _normalize_csv_header(header):
        return [str(col).strip().lstrip("\ufeff") for col in header]

    def _parse_csv_text(raw):
        try:
            sample_rows = list(csv.reader(io.StringIO(raw)))
        except Exception as exc:
            return None, f"Invalid CSV data: {exc}"

        if not sample_rows:
            return None, "CSV data is empty."
        if len(sample_rows) < 2:
            return None, "CSV must include header + at least one data row."

        header = _normalize_csv_header(sample_rows[0])
        if header != PLAYER_FIELDS:
            return None, (
                "CSV header must match exactly: "
                + ", ".join(PLAYER_FIELDS)
            )

        rows = []
        for line_idx, row_values in enumerate(sample_rows[1:], start=2):
            if len(row_values) != len(PLAYER_FIELDS):
                return None, f"CSV row {line_idx}: column count does not match header."
            row_dict = {}
            for col_idx, field in enumerate(PLAYER_FIELDS):
                row_dict[field] = row_values[col_idx]
            rows.append(row_dict)
        return rows, None

    def _parse_csv_upload(file_storage):
        try:
            raw = file_storage.read().decode("utf-8-sig")
        except Exception as exc:
            return None, f"Invalid CSV file: {exc}"
        return _parse_csv_text(raw)

    def _bulk_import_master(rows):
        imported, skipped, errors = 0, 0, []
        for idx, raw in enumerate(rows, start=1):
            data, err = _validate_player_dict(raw, idx, strict_import=True)
            if err:
                errors.append(err)
                continue
            existing = DBMasterPlayer.query.filter_by(name=data["name"]).first()
            if existing:
                skipped += 1
                continue
            player = DBMasterPlayer()
            _apply_fields(player, data)
            db.session.add(player)
            imported += 1
        if imported:
            db.session.commit()
        return imported, skipped, errors

    def _bulk_import_user(rows, user_id):
        imported, skipped, errors = 0, 0, []
        for idx, raw in enumerate(rows, start=1):
            data, err = _validate_player_dict(raw, idx, strict_import=True)
            if err:
                errors.append(err)
                continue
            existing = DBUserPlayer.query.filter_by(
                user_id=user_id, name=data["name"], master_player_id=None,
            ).first()
            if existing:
                skipped += 1
                continue
            player = DBUserPlayer(user_id=user_id)
            _apply_fields(player, data)
            db.session.add(player)
            imported += 1
        if imported:
            db.session.commit()
        return imported, skipped, errors

    # ═══════════════════════════════════════════════════════════════════════
    #  ADMIN ROUTES — MasterPlayer CRUD
    # ═══════════════════════════════════════════════════════════════════════

    @app.route("/admin/player-pool")
    @admin_required
    def admin_player_pool():
        search = request.args.get("q", "").strip()
        role_filter = request.args.get("role", "").strip()
        query = DBMasterPlayer.query
        if search:
            query = query.filter(DBMasterPlayer.name.ilike(f"%{search}%"))
        if role_filter and role_filter in VALID_ROLES:
            query = query.filter_by(role=role_filter)
        total_filtered = query.count()
        players = query.order_by(DBMasterPlayer.name).limit(POOL_PAGE_SIZE).all()
        return render_template(
            "admin/player_pool.html",
            players=players,
            search=search,
            role_filter=role_filter,
            roles=sorted(VALID_ROLES),
            total=DBMasterPlayer.query.count(),
            total_filtered=total_filtered,
            page_size=POOL_PAGE_SIZE,
        )

    @app.route("/api/admin/player-pool/search")
    @admin_required
    def api_admin_player_pool_search():
        search = request.args.get("q", "").strip()
        role_filter = request.args.get("role", "").strip()
        try:
            offset = int(request.args.get("offset", 0))
        except ValueError:
            offset = 0
        try:
            limit = int(request.args.get("limit", POOL_PAGE_SIZE))
        except ValueError:
            limit = POOL_PAGE_SIZE
        query = DBMasterPlayer.query
        if search:
            query = query.filter(DBMasterPlayer.name.ilike(f"%{search}%"))
        if role_filter and role_filter in VALID_ROLES:
            query = query.filter_by(role=role_filter)
        total = query.count()
        players = query.order_by(DBMasterPlayer.name).offset(offset).limit(limit).all()
        data = []
        for p in players:
            data.append({
                "id": p.id, "name": p.name, "role": p.role or "",
                "batting_rating": p.batting_rating, "bowling_rating": p.bowling_rating,
                "fielding_rating": p.fielding_rating,
                "batting_hand": p.batting_hand or "", "bowling_type": p.bowling_type or "",
            })
        return jsonify({"players": data, "total": total, "offset": offset, "has_more": offset + len(data) < total})

    @app.route("/admin/player-pool/add", methods=["GET", "POST"])
    @admin_required
    def admin_player_pool_add():
        if request.method == "POST":
            data, err = _validate_player_dict(request.form)
            if err:
                flash(err, "danger")
                return redirect(url_for("admin_player_pool_add"))
            existing = DBMasterPlayer.query.filter_by(name=data["name"]).first()
            if existing:
                flash(f"Player '{data['name']}' already exists.", "warning")
                return redirect(url_for("admin_player_pool_add"))
            player = DBMasterPlayer()
            _apply_fields(player, data)
            db.session.add(player)
            db.session.commit()
            flash(f"Player '{data['name']}' added to global pool.", "success")
            return redirect(url_for("admin_player_pool"))
        return render_template(
            "admin/player_pool_form.html",
            edit=False, player=None, roles=sorted(VALID_ROLES),
            batting_hands=sorted(VALID_BATTING_HANDS - {""}),
            bowling_types=sorted(VALID_BOWLING_TYPES - {""}),
            bowling_hands=sorted(VALID_BOWLING_HANDS - {""}),
        )

    @app.route("/admin/player-pool/<int:player_id>/edit", methods=["GET", "POST"])
    @admin_required
    def admin_player_pool_edit(player_id):
        player = DBMasterPlayer.query.get_or_404(player_id)
        if request.method == "POST":
            data, err = _validate_player_dict(request.form)
            if err:
                flash(err, "danger")
                return redirect(url_for("admin_player_pool_edit", player_id=player_id))
            dupe = DBMasterPlayer.query.filter(
                DBMasterPlayer.name == data["name"],
                DBMasterPlayer.id != player_id,
            ).first()
            if dupe:
                flash(f"Another player named '{data['name']}' already exists.", "warning")
                return redirect(url_for("admin_player_pool_edit", player_id=player_id))
            _apply_fields(player, data)
            db.session.commit()
            flash(f"Player '{data['name']}' updated.", "success")
            return redirect(url_for("admin_player_pool"))
        return render_template(
            "admin/player_pool_form.html",
            edit=True, player=player, roles=sorted(VALID_ROLES),
            batting_hands=sorted(VALID_BATTING_HANDS - {""}),
            bowling_types=sorted(VALID_BOWLING_TYPES - {""}),
            bowling_hands=sorted(VALID_BOWLING_HANDS - {""}),
        )

    @app.route("/admin/player-pool/<int:player_id>/delete", methods=["POST"])
    @admin_required
    def admin_player_pool_delete(player_id):
        player = DBMasterPlayer.query.get_or_404(player_id)
        name = player.name
        db.session.delete(player)
        db.session.commit()
        flash(f"Player '{name}' deleted from global pool.", "success")
        return redirect(url_for("admin_player_pool"))

    @app.route("/admin/player-pool/bulk-delete", methods=["POST"])
    @admin_required
    def admin_player_pool_bulk_delete():
        data = request.get_json(silent=True) or {}
        ids = data.get("ids", [])
        if not ids:
            return jsonify({"error": "No players selected."}), 400
        deleted = 0
        for pid in ids:
            player = DBMasterPlayer.query.get(pid)
            if player:
                db.session.delete(player)
                deleted += 1
        db.session.commit()
        return jsonify({"ok": True, "deleted": deleted})

    @app.route("/admin/player-pool/import", methods=["GET", "POST"])
    @admin_required
    def admin_player_pool_import():
        if request.method == "POST":
            fmt = request.form.get("format", "json")
            pasted_data = request.form.get("pasted_data", "").strip()
            f = request.files.get("file")
            has_file = bool(f and f.filename)

            if not pasted_data and not has_file:
                flash("Provide either a file upload or pasted data.", "danger")
                return redirect(url_for("admin_player_pool_import"))

            if fmt == "csv":
                rows, err = _parse_csv_text(pasted_data) if pasted_data else _parse_csv_upload(f)
            else:
                rows, err = _parse_json_text(pasted_data) if pasted_data else _parse_json_upload(f)

            if err:
                flash(err, "danger")
                return redirect(url_for("admin_player_pool_import"))
            try:
                imported, skipped, errors = _bulk_import_master(rows)
            except Exception as exc:
                db.session.rollback()
                log_exception(exc, source="player_pool")
                flash(f"Import failed: {exc}", "danger")
                return redirect(url_for("admin_player_pool_import"))
            msg = f"Imported {imported}, skipped {skipped} duplicate(s)."
            if errors:
                msg += f" {len(errors)} error(s): " + "; ".join(errors[:5])
            flash(msg, "success" if not errors else "warning")
            return redirect(url_for("admin_player_pool"))
        return render_template(
            "admin/player_pool_import.html",
            player_fields=PLAYER_FIELDS,
            roles=sorted(VALID_ROLES),
            batting_hands=sorted(VALID_BATTING_HANDS - {""}),
            bowling_types=sorted(VALID_BOWLING_TYPES - {""}),
            bowling_hands=sorted(VALID_BOWLING_HANDS - {""}),
        )

    @app.route("/admin/player-pool/export/json")
    @admin_required
    def admin_player_pool_export_json():
        players = DBMasterPlayer.query.order_by(DBMasterPlayer.name).all()
        data = []
        for p in players:
            data.append({
                "name": p.name, "role": p.role,
                "batting_rating": p.batting_rating, "bowling_rating": p.bowling_rating,
                "fielding_rating": p.fielding_rating,
                "batting_hand": p.batting_hand or "", "bowling_type": p.bowling_type or "",
                "bowling_hand": p.bowling_hand or "",
                "is_captain": p.is_captain or False,
                "is_wicketkeeper": p.is_wicketkeeper or False,
            })
        return jsonify(data)

    # ═══════════════════════════════════════════════════════════════════════
    #  USER ROUTES — effective pool browse + UserPlayer CRUD
    # ═══════════════════════════════════════════════════════════════════════

    POOL_PAGE_SIZE = 50

    @app.route("/player-pool")
    @login_required
    def player_pool():
        search = request.args.get("q", "").strip()
        role_filter = request.args.get("role", "").strip() or None
        pool, total = _get_effective_pool(current_user.id, search=search, role_filter=role_filter, limit=POOL_PAGE_SIZE)
        return render_template(
            "player_pool.html",
            pool=pool,
            search=search,
            role_filter=role_filter or "",
            roles=sorted(VALID_ROLES),
            total=total,
            page_size=POOL_PAGE_SIZE,
        )

    @app.route("/player-pool/add", methods=["GET", "POST"])
    @login_required
    def player_pool_add():
        if request.method == "POST":
            data, err = _validate_player_dict(request.form)
            if err:
                flash(err, "danger")
                return redirect(url_for("player_pool_add"))
            existing = DBUserPlayer.query.filter_by(
                user_id=current_user.id, name=data["name"], master_player_id=None,
            ).first()
            if existing:
                flash(f"You already have a custom player named '{data['name']}'.", "warning")
                return redirect(url_for("player_pool_add"))
            player = DBUserPlayer(user_id=current_user.id)
            _apply_fields(player, data)
            db.session.add(player)
            db.session.commit()
            flash(f"Player '{data['name']}' added to your pool.", "success")
            return redirect(url_for("player_pool"))
        return render_template(
            "player_pool_form.html",
            edit=False, player=None, override=False,
            roles=sorted(VALID_ROLES),
            batting_hands=sorted(VALID_BATTING_HANDS - {""}),
            bowling_types=sorted(VALID_BOWLING_TYPES - {""}),
            bowling_hands=sorted(VALID_BOWLING_HANDS - {""}),
        )

    @app.route("/player-pool/<int:player_id>/edit", methods=["GET", "POST"])
    @login_required
    def player_pool_edit(player_id):
        player = DBUserPlayer.query.get_or_404(player_id)
        if player.user_id != current_user.id:
            return jsonify({"error": "Forbidden"}), 403
        if request.method == "POST":
            data, err = _validate_player_dict(request.form)
            if err:
                flash(err, "danger")
                return redirect(url_for("player_pool_edit", player_id=player_id))
            if player.master_player_id is None:
                dupe = DBUserPlayer.query.filter(
                    DBUserPlayer.user_id == current_user.id,
                    DBUserPlayer.name == data["name"],
                    DBUserPlayer.master_player_id.is_(None),
                    DBUserPlayer.id != player_id,
                ).first()
                if dupe:
                    flash(f"You already have a custom player named '{data['name']}'.", "warning")
                    return redirect(url_for("player_pool_edit", player_id=player_id))
            _apply_fields(player, data)
            db.session.commit()
            flash(f"Player '{data['name']}' updated.", "success")
            return redirect(url_for("player_pool"))
        return render_template(
            "player_pool_form.html",
            edit=True, player=player,
            override=player.master_player_id is not None,
            roles=sorted(VALID_ROLES),
            batting_hands=sorted(VALID_BATTING_HANDS - {""}),
            bowling_types=sorted(VALID_BOWLING_TYPES - {""}),
            bowling_hands=sorted(VALID_BOWLING_HANDS - {""}),
        )

    @app.route("/player-pool/<int:player_id>/delete", methods=["POST"])
    @login_required
    def player_pool_delete(player_id):
        player = DBUserPlayer.query.get_or_404(player_id)
        if player.user_id != current_user.id:
            return jsonify({"error": "Forbidden"}), 403
        name = player.name
        is_override = player.master_player_id is not None
        db.session.delete(player)
        db.session.commit()
        if is_override:
            flash(f"Override for '{name}' removed — reverted to global values.", "success")
        else:
            flash(f"Player '{name}' deleted from your pool.", "success")
        return redirect(url_for("player_pool"))

    @app.route("/player-pool/bulk-delete", methods=["POST"])
    @login_required
    def player_pool_bulk_delete():
        data = request.get_json(silent=True) or {}
        ids = data.get("ids", [])
        if not ids:
            return jsonify({"error": "No players selected."}), 400
        deleted = 0
        for pid in ids:
            player = DBUserPlayer.query.get(pid)
            if player and player.user_id == current_user.id:
                db.session.delete(player)
                deleted += 1
        db.session.commit()
        return jsonify({"ok": True, "deleted": deleted})

    @app.route("/player-pool/override/<int:master_id>", methods=["GET", "POST"])
    @login_required
    def player_pool_override(master_id):
        master = DBMasterPlayer.query.get_or_404(master_id)
        existing = DBUserPlayer.query.filter_by(
            user_id=current_user.id, master_player_id=master_id,
        ).first()
        if request.method == "POST":
            data, err = _validate_player_dict(request.form)
            if err:
                flash(err, "danger")
                return redirect(url_for("player_pool_override", master_id=master_id))
            if existing:
                _apply_fields(existing, data)
            else:
                existing = DBUserPlayer(user_id=current_user.id, master_player_id=master_id)
                _apply_fields(existing, data)
                db.session.add(existing)
            db.session.commit()
            flash(f"Your override for '{data['name']}' saved.", "success")
            return redirect(url_for("player_pool"))
        player = existing or master
        return render_template(
            "player_pool_form.html",
            edit=existing is not None, player=player, override=True,
            master=master,
            roles=sorted(VALID_ROLES),
            batting_hands=sorted(VALID_BATTING_HANDS - {""}),
            bowling_types=sorted(VALID_BOWLING_TYPES - {""}),
            bowling_hands=sorted(VALID_BOWLING_HANDS - {""}),
        )

    @app.route("/player-pool/import", methods=["GET", "POST"])
    @login_required
    def player_pool_import():
        if request.method == "POST":
            fmt = request.form.get("format", "json")
            pasted_data = request.form.get("pasted_data", "").strip()
            f = request.files.get("file")
            has_file = bool(f and f.filename)

            if not pasted_data and not has_file:
                flash("Provide either a file upload or pasted data.", "danger")
                return redirect(url_for("player_pool_import"))

            if fmt == "csv":
                if pasted_data:
                    rows, err = _parse_csv_text(pasted_data)
                else:
                    rows, err = _parse_csv_upload(f)
            else:
                if pasted_data:
                    rows, err = _parse_json_text(pasted_data)
                else:
                    rows, err = _parse_json_upload(f)
            if err:
                flash(err, "danger")
                return redirect(url_for("player_pool_import"))
            try:
                imported, skipped, errors = _bulk_import_user(rows, current_user.id)
            except Exception as exc:
                db.session.rollback()
                log_exception(exc, source="player_pool")
                flash(f"Import failed: {exc}", "danger")
                return redirect(url_for("player_pool_import"))
            msg = f"Imported {imported}, skipped {skipped} duplicate(s)."
            if errors:
                msg += f" {len(errors)} error(s): " + "; ".join(errors[:5])
            flash(msg, "success" if not errors else "warning")
            return redirect(url_for("player_pool"))
        return render_template(
            "player_pool_import.html",
            roles=sorted(VALID_ROLES),
            batting_hands=sorted(VALID_BATTING_HANDS),
            bowling_types=sorted(VALID_BOWLING_TYPES),
            bowling_hands=sorted(VALID_BOWLING_HANDS),
            player_fields=PLAYER_FIELDS,
        )

    @app.route("/player-pool/import/template/json")
    @login_required
    def player_pool_import_template_json():
        sample_data = [
            {
                "name": "Virat Kohli",
                "role": "Batsman",
                "batting_rating": 95,
                "bowling_rating": 20,
                "fielding_rating": 85,
                "batting_hand": "Right",
                "bowling_type": "Medium",
                "bowling_hand": "Right",
                "is_captain": False,
                "is_wicketkeeper": False,
            },
            {
                "name": "Jasprit Bumrah",
                "role": "Bowler",
                "batting_rating": 25,
                "bowling_rating": 97,
                "fielding_rating": 78,
                "batting_hand": "Right",
                "bowling_type": "Fast",
                "bowling_hand": "Right",
                "is_captain": False,
                "is_wicketkeeper": False,
            },
            {
                "name": "MS Dhoni",
                "role": "Wicketkeeper",
                "batting_rating": 88,
                "bowling_rating": 0,
                "fielding_rating": 92,
                "batting_hand": "Right",
                "bowling_type": "",
                "bowling_hand": "",
                "is_captain": True,
                "is_wicketkeeper": True,
            },
        ]
        return Response(
            json.dumps(sample_data, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=player_pool_template.json"},
        )

    @app.route("/player-pool/import/template/csv")
    @login_required
    def player_pool_import_template_csv():
        sample_rows = [
            PLAYER_FIELDS,
            ["Virat Kohli", "Batsman", "95", "20", "85", "Right", "Medium", "Right", "false", "false"],
            ["Jasprit Bumrah", "Bowler", "25", "97", "78", "Right", "Fast", "Right", "false", "false"],
            ["MS Dhoni", "Wicketkeeper", "88", "0", "92", "Right", "", "", "true", "true"],
        ]
        output = io.StringIO()
        writer = csv.writer(output)
        for row in sample_rows:
            writer.writerow(row)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=player_pool_template.csv"},
        )

    # ── AJAX search ──────────────────────────────────────────────────────────

    @app.route("/api/player-pool/search")
    @login_required
    def api_player_pool_search():
        search = request.args.get("q", "").strip()
        role_filter = request.args.get("role", "").strip() or None
        try:
            offset = int(request.args.get("offset", 0))
        except ValueError:
            offset = 0
        try:
            limit = int(request.args.get("limit", POOL_PAGE_SIZE))
        except ValueError:
            limit = POOL_PAGE_SIZE
        pool, total = _get_effective_pool(current_user.id, search=search, role_filter=role_filter, offset=offset, limit=limit)
        return jsonify({"players": pool, "total": total, "offset": offset, "has_more": offset + len(pool) < total})

    @app.route("/api/player-pool/check-name")
    @login_required
    def api_player_pool_check_name():
        name = request.args.get("name", "").strip()
        if not name:
            return jsonify({"exists": False})
        name_lower = name.lower()
        # Check admin pool
        master = DBMasterPlayer.query.filter(
            db.func.lower(DBMasterPlayer.name) == name_lower
        ).first()
        if master:
            return jsonify({"exists": True, "source": "global", "name": master.name})
        # Check user's custom players
        custom = DBUserPlayer.query.filter(
            DBUserPlayer.user_id == current_user.id,
            db.func.lower(DBUserPlayer.name) == name_lower,
            DBUserPlayer.master_player_id.is_(None),
        ).first()
        if custom:
            return jsonify({"exists": True, "source": "custom", "name": custom.name})
        return jsonify({"exists": False})
