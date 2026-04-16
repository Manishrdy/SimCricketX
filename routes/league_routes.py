"""
AUCTION-REDESIGN Phase 1 — League / Season / SeasonTeam route registration.

Provides CRUD for:
  - League   (the brand, e.g. "IPL")
  - Season   (one run, e.g. "IPL 2026")
  - SeasonTeam (empty Team row under a season, with a manager access token)

Manager portal URLs are emitted for copy/paste; the portal itself is wired up
in a later phase. Tokens are UUIDs; one token per team for v1.
"""

import re
import uuid

from flask import flash, redirect, render_template, request, url_for, abort
from flask_login import current_user, login_required
from utils.exception_tracker import log_exception


VALID_FREQUENCIES = ("one_time", "recurring")
VALID_FORMATS = ("T20", "ListA")
VALID_AUCTION_MODES = ("traditional", "draft")
SHORT_CODE_RE = re.compile(r"^[A-Z0-9]{2,10}$")


def register_league_routes(
    app,
    *,
    db,
    DBLeague,
    DBSeason,
    DBSeasonTeam,
    DBTeam,
):
    # ─── Helpers ────────────────────────────────────────────────────────────

    def _own_league_or_404(league_id):
        lg = DBLeague.query.get(league_id)
        if lg is None or lg.user_id != current_user.id:
            abort(404)
        return lg

    def _own_season_or_404(season_id):
        s = DBSeason.query.get(season_id)
        if s is None:
            abort(404)
        lg = DBLeague.query.get(s.league_id)
        if lg is None or lg.user_id != current_user.id:
            abort(404)
        return s, lg

    def _own_season_team_or_404(season_team_id):
        st = DBSeasonTeam.query.get(season_team_id)
        if st is None:
            abort(404)
        season, league = _own_season_or_404(st.season_id)
        return st, season, league

    def _derive_short_code(user_id, display_name):
        base = re.sub(r"[^A-Z0-9]", "", (display_name or "").upper())[:5] or "TM"
        candidate = base
        n = 1
        while DBTeam.query.filter_by(user_id=user_id, short_code=candidate).first():
            suffix = str(n)
            candidate = base[: max(1, 5 - len(suffix))] + suffix
            n += 1
            if n > 999:
                raise ValueError("Could not derive a unique short code")
        return candidate

    def _portal_path(token):
        return f"/t/{token}"

    # ─── League routes ──────────────────────────────────────────────────────

    @app.route("/leagues")
    @login_required
    def leagues_list():
        leagues = (
            DBLeague.query
            .filter_by(user_id=current_user.id)
            .order_by(DBLeague.created_at.desc())
            .all()
        )
        return render_template("leagues/list.html", leagues=leagues)

    @app.route("/leagues/create", methods=["POST"])
    @login_required
    def leagues_create():
        name = (request.form.get("name") or "").strip()
        short_code = (request.form.get("short_code") or "").strip().upper() or None
        frequency = (request.form.get("frequency") or "one_time").strip()

        if not name:
            flash("League name is required.", "error")
            return redirect(url_for("leagues_list"))
        if frequency not in VALID_FREQUENCIES:
            flash("Invalid frequency.", "error")
            return redirect(url_for("leagues_list"))
        if short_code and not SHORT_CODE_RE.match(short_code):
            flash("Short code must be 2-10 uppercase letters/digits.", "error")
            return redirect(url_for("leagues_list"))
        if DBLeague.query.filter_by(user_id=current_user.id, name=name).first():
            flash(f"A league named '{name}' already exists.", "error")
            return redirect(url_for("leagues_list"))

        try:
            lg = DBLeague(
                user_id=current_user.id,
                name=name,
                short_code=short_code,
                frequency=frequency,
            )
            db.session.add(lg)
            db.session.commit()
            flash(f"League '{name}' created.", "success")
            return redirect(url_for("league_detail", league_id=lg.id))
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="league_create")
            flash("Could not create league.", "error")
            return redirect(url_for("leagues_list"))

    @app.route("/leagues/<int:league_id>")
    @login_required
    def league_detail(league_id):
        lg = _own_league_or_404(league_id)
        seasons = (
            DBSeason.query
            .filter_by(league_id=lg.id)
            .order_by(DBSeason.created_at.desc())
            .all()
        )
        return render_template("leagues/detail.html", league=lg, seasons=seasons)

    @app.route("/leagues/<int:league_id>/edit", methods=["POST"])
    @login_required
    def league_edit(league_id):
        lg = _own_league_or_404(league_id)
        name = (request.form.get("name") or "").strip()
        short_code = (request.form.get("short_code") or "").strip().upper() or None
        frequency = (request.form.get("frequency") or "one_time").strip()

        if not name:
            flash("League name is required.", "error")
            return redirect(url_for("league_detail", league_id=lg.id))
        if frequency not in VALID_FREQUENCIES:
            flash("Invalid frequency.", "error")
            return redirect(url_for("league_detail", league_id=lg.id))
        if short_code and not SHORT_CODE_RE.match(short_code):
            flash("Short code must be 2-10 uppercase letters/digits.", "error")
            return redirect(url_for("league_detail", league_id=lg.id))

        collision = (
            DBLeague.query
            .filter(DBLeague.user_id == current_user.id,
                    DBLeague.name == name,
                    DBLeague.id != lg.id)
            .first()
        )
        if collision:
            flash(f"Another league named '{name}' already exists.", "error")
            return redirect(url_for("league_detail", league_id=lg.id))

        try:
            lg.name = name
            lg.short_code = short_code
            lg.frequency = frequency
            db.session.commit()
            flash("League updated.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="league_edit")
            flash("Could not update league.", "error")
        return redirect(url_for("league_detail", league_id=lg.id))

    @app.route("/leagues/<int:league_id>/delete", methods=["POST"])
    @login_required
    def league_delete(league_id):
        lg = _own_league_or_404(league_id)
        try:
            db.session.delete(lg)
            db.session.commit()
            flash(f"League '{lg.name}' deleted.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="league_delete")
            flash("Could not delete league.", "error")
        return redirect(url_for("leagues_list"))

    # ─── Season routes ──────────────────────────────────────────────────────

    @app.route("/leagues/<int:league_id>/seasons/create", methods=["POST"])
    @login_required
    def season_create(league_id):
        lg = _own_league_or_404(league_id)

        name = (request.form.get("name") or "").strip()
        fmt = (request.form.get("format") or "T20").strip()
        mode = (request.form.get("auction_mode") or "traditional").strip()

        if not name:
            flash("Season name is required.", "error")
            return redirect(url_for("league_detail", league_id=lg.id))
        if fmt not in VALID_FORMATS:
            flash("Invalid format.", "error")
            return redirect(url_for("league_detail", league_id=lg.id))
        if mode not in VALID_AUCTION_MODES:
            flash("Invalid auction mode.", "error")
            return redirect(url_for("league_detail", league_id=lg.id))
        if DBSeason.query.filter_by(league_id=lg.id, name=name).first():
            flash(f"A season named '{name}' already exists in this league.", "error")
            return redirect(url_for("league_detail", league_id=lg.id))

        try:
            season = DBSeason(
                league_id=lg.id,
                name=name,
                format=fmt,
                auction_mode=mode,
                status="setup",
            )
            db.session.add(season)
            db.session.commit()
            flash(f"Season '{name}' created.", "success")
            return redirect(url_for("season_detail", season_id=season.id))
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="season_create")
            flash("Could not create season.", "error")
            return redirect(url_for("league_detail", league_id=lg.id))

    @app.route("/seasons/<int:season_id>")
    @login_required
    def season_detail(season_id):
        season, league = _own_season_or_404(season_id)
        season_teams = (
            DBSeasonTeam.query
            .filter_by(season_id=season.id)
            .order_by(DBSeasonTeam.created_at.asc())
            .all()
        )
        return render_template(
            "leagues/season.html",
            league=league,
            season=season,
            season_teams=season_teams,
            portal_path=_portal_path,
        )

    @app.route("/seasons/<int:season_id>/edit", methods=["POST"])
    @login_required
    def season_edit(season_id):
        season, league = _own_season_or_404(season_id)

        name = (request.form.get("name") or "").strip()
        fmt = (request.form.get("format") or "T20").strip()
        mode = (request.form.get("auction_mode") or "traditional").strip()

        if not name:
            flash("Season name is required.", "error")
            return redirect(url_for("season_detail", season_id=season.id))
        if fmt not in VALID_FORMATS:
            flash("Invalid format.", "error")
            return redirect(url_for("season_detail", season_id=season.id))
        if mode not in VALID_AUCTION_MODES:
            flash("Invalid auction mode.", "error")
            return redirect(url_for("season_detail", season_id=season.id))

        collision = (
            DBSeason.query
            .filter(DBSeason.league_id == league.id,
                    DBSeason.name == name,
                    DBSeason.id != season.id)
            .first()
        )
        if collision:
            flash(f"Another season named '{name}' already exists.", "error")
            return redirect(url_for("season_detail", season_id=season.id))

        try:
            season.name = name
            season.format = fmt
            season.auction_mode = mode
            db.session.commit()
            flash("Season updated.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="season_edit")
            flash("Could not update season.", "error")
        return redirect(url_for("season_detail", season_id=season.id))

    @app.route("/seasons/<int:season_id>/delete", methods=["POST"])
    @login_required
    def season_delete(season_id):
        season, league = _own_season_or_404(season_id)
        try:
            db.session.delete(season)
            db.session.commit()
            flash(f"Season '{season.name}' deleted.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="season_delete")
            flash("Could not delete season.", "error")
        return redirect(url_for("league_detail", league_id=league.id))

    # ─── Season-team routes ─────────────────────────────────────────────────

    @app.route("/seasons/<int:season_id>/teams/create", methods=["POST"])
    @login_required
    def season_team_create(season_id):
        season, league = _own_season_or_404(season_id)

        display_name = (request.form.get("display_name") or "").strip()
        if not display_name:
            flash("Team name is required.", "error")
            return redirect(url_for("season_detail", season_id=season.id))
        if DBSeasonTeam.query.filter_by(season_id=season.id, display_name=display_name).first():
            flash(f"A team named '{display_name}' already exists in this season.", "error")
            return redirect(url_for("season_detail", season_id=season.id))

        try:
            short_code = _derive_short_code(current_user.id, display_name)
            team = DBTeam(
                user_id=current_user.id,
                name=display_name,
                short_code=short_code,
                season_id=season.id,
                is_draft=True,  # empty team; will be finalized post-auction
            )
            db.session.add(team)
            db.session.flush()  # obtain team.id

            st = DBSeasonTeam(
                season_id=season.id,
                team_id=team.id,
                display_name=display_name,
                access_token=str(uuid.uuid4()),
            )
            db.session.add(st)
            db.session.commit()
            flash(f"Team '{display_name}' added.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="season_team_create")
            flash("Could not add team.", "error")
        return redirect(url_for("season_detail", season_id=season.id))

    @app.route("/season-teams/<int:season_team_id>/edit", methods=["POST"])
    @login_required
    def season_team_edit(season_team_id):
        st, season, league = _own_season_team_or_404(season_team_id)

        display_name = (request.form.get("display_name") or "").strip()
        if not display_name:
            flash("Team name is required.", "error")
            return redirect(url_for("season_detail", season_id=season.id))

        collision = (
            DBSeasonTeam.query
            .filter(DBSeasonTeam.season_id == season.id,
                    DBSeasonTeam.display_name == display_name,
                    DBSeasonTeam.id != st.id)
            .first()
        )
        if collision:
            flash(f"Another team named '{display_name}' already exists.", "error")
            return redirect(url_for("season_detail", season_id=season.id))

        try:
            st.display_name = display_name
            # Keep the backing Team.name in sync (short_code preserved to avoid collisions).
            team = DBTeam.query.get(st.team_id)
            if team is not None:
                team.name = display_name
            db.session.commit()
            flash("Team renamed.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="season_team_edit")
            flash("Could not rename team.", "error")
        return redirect(url_for("season_detail", season_id=season.id))

    @app.route("/season-teams/<int:season_team_id>/delete", methods=["POST"])
    @login_required
    def season_team_delete(season_team_id):
        st, season, league = _own_season_team_or_404(season_team_id)
        try:
            # Cascade: deleting the Team row removes its SeasonTeam + TeamProfiles + Players.
            team = DBTeam.query.get(st.team_id)
            if team is not None:
                db.session.delete(team)
            else:
                db.session.delete(st)
            db.session.commit()
            flash(f"Team '{st.display_name}' removed.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="season_team_delete")
            flash("Could not remove team.", "error")
        return redirect(url_for("season_detail", season_id=season.id))

    @app.route("/season-teams/<int:season_team_id>/rotate-token", methods=["POST"])
    @login_required
    def season_team_rotate_token(season_team_id):
        st, season, league = _own_season_team_or_404(season_team_id)
        try:
            st.access_token = str(uuid.uuid4())
            db.session.commit()
            flash(f"Portal link rotated for '{st.display_name}'. Reshare the new link.", "success")
        except Exception as exc:
            db.session.rollback()
            log_exception(exc, source="season_team_rotate_token")
            flash("Could not rotate token.", "error")
        return redirect(url_for("season_detail", season_id=season.id))
