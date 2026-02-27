"""Team management route registration."""

import json

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

VALID_FORMATS = ("T20", "ListA", "FirstClass")


def register_team_routes(
    app,
    *,
    db,
    Player,
    DBTeam,
    DBPlayer,
    DBTeamProfile,
):
    # ── Internal helpers ──────────────────────────────────────────────────────

    def _extract_player_list(raw_list):
        """
        Parse a list of player dicts from the profiles_payload JSON.
        Returns (list[dict], error_str|None).
        """
        players = []
        for idx, item in enumerate(raw_list, start=1):
            if not isinstance(item, dict):
                return None, f"Invalid player item at position {idx}."
            name = str(item.get("name", "")).strip()
            role = str(item.get("role", "")).strip()
            batting_hand = str(item.get("batting_hand", "")).strip()
            bowling_type = str(item.get("bowling_type", "")).strip()
            bowling_hand = str(item.get("bowling_hand", "")).strip()
            try:
                bat = int(item.get("batting_rating", 0))
                bowl = int(item.get("bowling_rating", 0))
                field = int(item.get("fielding_rating", 0))
            except (TypeError, ValueError):
                return None, f"Player {idx}: ratings must be valid integers."
            if not (0 <= bat <= 100 and 0 <= bowl <= 100 and 0 <= field <= 100):
                return None, f"Player {idx}: ratings must be between 0 and 100."
            players.append({
                "name": name,
                "role": role,
                "batting_rating": bat,
                "bowling_rating": bowl,
                "fielding_rating": field,
                "batting_hand": batting_hand,
                "bowling_type": bowling_type,
                "bowling_hand": bowling_hand,
            })
        return players, None

    def _validate_profile(fmt, profile_data, is_draft):
        """
        Validate a single profile dict {captain, wicketkeeper, players: [...]}.
        Returns error string or None.
        """
        players = profile_data.get("players", [])
        captain = (profile_data.get("captain") or "").strip()
        wk_name = (profile_data.get("wicketkeeper") or "").strip()

        if is_draft:
            if len(players) < 1:
                return f"{fmt} profile: draft must have at least 1 player."
            return None

        if not (12 <= len(players) <= 25):
            return f"{fmt} profile: must have between 12 and 25 players."

        wk_count = sum(1 for p in players if p["role"] == "Wicketkeeper")
        if wk_count < 1:
            return f"{fmt} profile: needs at least one Wicketkeeper."

        bowl_count = sum(1 for p in players if p["role"] in ("Bowler", "All-rounder"))
        if bowl_count < 6:
            return f"{fmt} profile: needs at least six Bowlers/All-rounders."

        if not captain:
            return f"{fmt} profile: captain must be selected."
        if not wk_name:
            return f"{fmt} profile: wicketkeeper must be selected."

        return None

    def _parse_profiles_payload(form):
        """
        Parse the 'profiles_payload' hidden field.
        Returns (profiles_dict, error_str|None).
        profiles_dict maps format_type → {captain, wicketkeeper, players: [...]}.
        """
        raw = (form.get("profiles_payload") or "").strip()
        if not raw:
            return None, "No profile data submitted."
        try:
            payload = json.loads(raw)
        except Exception:
            return None, "Invalid profiles payload format."

        if not isinstance(payload, dict):
            return None, "Profiles payload must be a JSON object."

        result = {}
        for fmt, pdata in payload.items():
            if fmt not in VALID_FORMATS:
                return None, f"Unknown format type: '{fmt}'."
            if not isinstance(pdata, dict):
                return None, f"{fmt} profile data must be an object."
            raw_players = pdata.get("players", [])
            players, err = _extract_player_list(raw_players)
            if err:
                return None, f"{fmt} profile — {err}"
            result[fmt] = {
                "captain": (pdata.get("captain") or "").strip(),
                "wicketkeeper": (pdata.get("wicketkeeper") or "").strip(),
                "players": players,
            }
        return result, None

    def _save_profiles(team_id, profiles_dict, is_draft):
        """
        Create TeamProfile + DBPlayer rows for each format in profiles_dict.
        Caller must commit the session after calling this.
        Returns error string or None.
        """
        for fmt, pdata in profiles_dict.items():
            if not pdata["players"]:
                continue  # Skip empty profiles
            profile = DBTeamProfile(team_id=team_id, format_type=fmt)
            db.session.add(profile)
            db.session.flush()  # get profile.id

            captain = pdata["captain"]
            wk_name = pdata["wicketkeeper"]
            for p in pdata["players"]:
                db_player = DBPlayer(
                    team_id=team_id,
                    profile_id=profile.id,
                    name=p["name"],
                    role=p["role"],
                    batting_rating=p["batting_rating"],
                    bowling_rating=p["bowling_rating"],
                    fielding_rating=p["fielding_rating"],
                    batting_hand=p["batting_hand"],
                    bowling_type=p["bowling_type"],
                    bowling_hand=p["bowling_hand"],
                    is_captain=(p["name"] == captain) if captain else False,
                    is_wicketkeeper=(p["name"] == wk_name) if wk_name else False,
                )
                db.session.add(db_player)
        return None

    def _find_name_conflict(user_id, team_name, *, exclude_team_id=None):
        """
        Return an existing team for the user whose name matches case-insensitively.
        Optionally exclude one team id (used during edit).
        """
        normalized = (team_name or "").strip().lower()
        if not normalized:
            return None
        query = DBTeam.query.filter(
            DBTeam.user_id == user_id,
            func.lower(func.trim(DBTeam.name)) == normalized,
        )
        if exclude_team_id is not None:
            query = query.filter(DBTeam.id != exclude_team_id)
        return query.first()

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.route("/team/create", methods=["GET", "POST"])
    @login_required
    def create_team():
        if request.method == "POST":
            try:
                name = request.form["team_name"].strip()
                short_code = request.form["short_code"].strip().upper()
                home_ground = request.form["home_ground"].strip()
                pitch = request.form["pitch_preference"]
                action = request.form.get("action", "publish")
                is_draft = action == "save_draft"

                if not (name and short_code and home_ground and pitch):
                    return render_template(
                        "team_create.html",
                        error="All team fields are required.",
                    )

                existing = DBTeam.query.filter_by(
                    user_id=current_user.id,
                    short_code=short_code,
                ).first()
                if existing:
                    return render_template(
                        "team_create.html",
                        error=(
                            f"You already have a team with short code '{short_code}'. "
                            "Please use a different code."
                        ),
                    )

                name_conflict = _find_name_conflict(current_user.id, name)
                if name_conflict:
                    return render_template(
                        "team_create.html",
                        error=(
                            f"You already have a team named '{name_conflict.name}'. "
                            "Please use a different team name."
                        ),
                    )

                profiles, parse_err = _parse_profiles_payload(request.form)
                if parse_err:
                    return render_template("team_create.html", error=parse_err)

                # At least one profile must have players
                non_empty = {
                    fmt: pd for fmt, pd in profiles.items() if pd["players"]
                }
                if not non_empty:
                    return render_template(
                        "team_create.html",
                        error="At least one format profile with players is required.",
                    )

                # Validate each non-empty profile
                for fmt, pdata in non_empty.items():
                    err = _validate_profile(fmt, pdata, is_draft)
                    if err:
                        return render_template("team_create.html", error=err)

                color = request.form.get("team_color", "#4f46e5")

                try:
                    new_team = DBTeam(
                        user_id=current_user.id,
                        name=name,
                        short_code=short_code,
                        home_ground=home_ground,
                        pitch_preference=pitch,
                        team_color=color,
                        is_draft=is_draft,
                    )
                    db.session.add(new_team)
                    db.session.flush()

                    save_err = _save_profiles(new_team.id, non_empty, is_draft)
                    if save_err:
                        db.session.rollback()
                        return render_template("team_create.html", error=save_err)

                    db.session.commit()
                    status_msg = "Draft" if is_draft else "Active"
                    app.logger.info(
                        f"Team '{new_team.name}' (ID: {new_team.id}) created as "
                        f"{status_msg} by {current_user.id}"
                    )
                    if is_draft:
                        flash("Team saved as draft.", "success")
                        return redirect(
                            url_for("manage_teams", clear_team_draft=1)
                        )
                    return redirect(url_for("home", clear_team_draft=1))

                except Exception as db_err:
                    db.session.rollback()
                    app.logger.error(
                        f"Database error saving team: {db_err}", exc_info=True
                    )
                    return render_template(
                        "team_create.html", error="Database error saving team."
                    )

            except Exception as e:
                app.logger.error(
                    f"Unexpected error saving team: {e}", exc_info=True
                )
                return render_template(
                    "team_create.html",
                    error="An unexpected error occurred. Please try again.",
                )

        return render_template("team_create.html")

    @app.route("/teams/manage")
    @login_required
    def manage_teams():
        teams = []
        try:
            db_teams = DBTeam.query.filter_by(user_id=current_user.id).filter(
                DBTeam.is_placeholder != True
            ).all()
            for t in db_teams:
                profiles_info = []
                for prof in t.profiles:
                    captain = next(
                        (p.name for p in prof.players if p.is_captain), ""
                    )
                    profiles_info.append({
                        "format_type": prof.format_type,
                        "player_count": len(prof.players),
                        "captain": captain,
                    })

                # Primary display: prefer T20, else first profile
                primary = next(
                    (pi for pi in profiles_info if pi["format_type"] == "T20"),
                    profiles_info[0] if profiles_info else None,
                )

                teams.append({
                    "id": t.id,
                    "team_name": t.name,
                    "short_code": t.short_code,
                    "home_ground": t.home_ground,
                    "pitch_preference": t.pitch_preference,
                    "team_color": t.team_color,
                    "captain": primary["captain"] if primary else "—",
                    "profiles": profiles_info,
                    "is_draft": getattr(t, "is_draft", False),
                })
        except Exception as e:
            app.logger.error(f"Error loading teams from DB: {e}", exc_info=True)

        return render_template("manage_teams.html", teams=teams)

    @app.route("/team/delete", methods=["POST"])
    @login_required
    def delete_team():
        short_code = request.form.get("short_code")
        if not short_code:
            flash("No team specified for deletion.", "danger")
            return redirect(url_for("manage_teams"))

        try:
            team = DBTeam.query.filter_by(
                short_code=short_code,
                user_id=current_user.id,
            ).first()

            if not team:
                app.logger.warning(
                    f"Delete failed: Team '{short_code}' not found or "
                    f"unauthorized for {current_user.id}"
                )
                flash("Team not found or you don't have permission to delete it.", "danger")
                return redirect(url_for("manage_teams"))

            team_name = team.name
            # Defensive cleanup: remove all players/profiles for this team explicitly.
            # This covers legacy rows that may not be profile-linked.
            db.session.query(DBPlayer).filter_by(team_id=team.id).delete(synchronize_session=False)
            db.session.query(DBTeamProfile).filter_by(team_id=team.id).delete(synchronize_session=False)
            db.session.delete(team)
            db.session.commit()

            app.logger.info(
                f"Team '{short_code}' (ID: {team.id}) deleted by {current_user.id}"
            )
            flash(f"Team '{team_name}' has been deleted.", "success")
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error deleting team from DB: {e}", exc_info=True)
            flash("An error occurred while deleting the team. Please try again.", "danger")

        return redirect(url_for("manage_teams"))

    @app.route("/team/<short_code>/edit", methods=["GET", "POST"])
    @login_required
    def edit_team(short_code):
        user_id = current_user.id

        try:
            team = DBTeam.query.filter_by(
                short_code=short_code, user_id=user_id
            ).first()
            if not team:
                app.logger.warning(
                    f"Edit failed: Team '{short_code}' not found or "
                    f"unauthorized for {user_id}"
                )
                return redirect(url_for("manage_teams"))

            if request.method == "POST":
                try:
                    team.name = request.form["team_name"].strip()
                    new_short_code = request.form["short_code"].strip().upper()
                    team.home_ground = request.form["home_ground"].strip()
                    team.pitch_preference = request.form["pitch_preference"]
                    team.team_color = request.form["team_color"]

                    name_conflict = _find_name_conflict(
                        user_id,
                        team.name,
                        exclude_team_id=team.id,
                    )
                    if name_conflict:
                        team_data = _build_team_data_for_edit(team)
                        return render_template(
                            "team_create.html",
                            team=team_data,
                            edit=True,
                            error=(
                                f"You already have a team named '{name_conflict.name}'. "
                                "Please use a different team name."
                            ),
                        )

                    if new_short_code != team.short_code:
                        conflict = DBTeam.query.filter_by(
                            user_id=user_id,
                            short_code=new_short_code,
                        ).first()
                        if conflict:
                            team_data = _build_team_data_for_edit(team)
                            return render_template(
                                "team_create.html",
                                team=team_data,
                                edit=True,
                                error=(
                                    f"You already have a team with short code "
                                    f"'{new_short_code}'."
                                ),
                            )

                    action = request.form.get("action", "publish")
                    is_draft = action == "save_draft"

                    profiles, parse_err = _parse_profiles_payload(request.form)
                    if parse_err:
                        team_data = _build_team_data_for_edit(team)
                        return render_template(
                            "team_create.html",
                            team=team_data,
                            edit=True,
                            error=parse_err,
                        )

                    non_empty = {
                        fmt: pd for fmt, pd in profiles.items() if pd["players"]
                    }
                    if not non_empty:
                        team_data = _build_team_data_for_edit(team)
                        return render_template(
                            "team_create.html",
                            team=team_data,
                            edit=True,
                            error="At least one format profile with players is required.",
                        )

                    for fmt, pdata in non_empty.items():
                        err = _validate_profile(fmt, pdata, is_draft)
                        if err:
                            team_data = _build_team_data_for_edit(team)
                            return render_template(
                                "team_create.html",
                                team=team_data,
                                edit=True,
                                error=err,
                            )

                    # Replace all profiles: delete existing, recreate
                    for prof in list(team.profiles):
                        db.session.delete(prof)
                    db.session.flush()

                    team.is_draft = is_draft
                    team.short_code = new_short_code

                    save_err = _save_profiles(team.id, non_empty, is_draft)
                    if save_err:
                        db.session.rollback()
                        team_data = _build_team_data_for_edit(team)
                        return render_template(
                            "team_create.html",
                            team=team_data,
                            edit=True,
                            error=save_err,
                        )

                    db.session.commit()
                    status_msg = "Draft" if is_draft else "Active"
                    app.logger.info(
                        f"Team '{team.short_code}' (ID: {team.id}) updated as "
                        f"{status_msg} by {user_id}"
                    )
                    flash(f"Team updated as {status_msg}.", "success")
                    return redirect(url_for("manage_teams"))

                except Exception as e:
                    db.session.rollback()
                    app.logger.error(f"Error updating team: {e}", exc_info=True)
                    flash(
                        "An error occurred while updating the team. Please try again.",
                        "danger",
                    )
                    return redirect(url_for("edit_team", short_code=short_code))

            # GET — build data dict for template
            team_data = _build_team_data_for_edit(team)
            return render_template("team_create.html", team=team_data, edit=True)

        except Exception as e:
            app.logger.error(f"Error in edit_team: {e}", exc_info=True)
            return redirect(url_for("manage_teams"))

    # ── Edit helper ───────────────────────────────────────────────────────────

    def _build_team_data_for_edit(team):
        """Return a dict consumed by team_create.html in edit mode."""
        profiles_dict = {}
        for prof in team.profiles:
            captain = next(
                (p.name for p in prof.players if p.is_captain), ""
            )
            wk = next(
                (p.name for p in prof.players if p.is_wicketkeeper), ""
            )
            profiles_dict[prof.format_type] = {
                "captain": captain,
                "wicketkeeper": wk,
                "players": [
                    {
                        "name": p.name,
                        "role": p.role,
                        "batting_rating": p.batting_rating,
                        "bowling_rating": p.bowling_rating,
                        "fielding_rating": p.fielding_rating,
                        "batting_hand": p.batting_hand,
                        "bowling_type": p.bowling_type or "",
                        "bowling_hand": p.bowling_hand or "",
                    }
                    for p in prof.players
                ],
            }

        return {
            "team_name": team.name,
            "short_code": team.short_code,
            "home_ground": team.home_ground,
            "pitch_preference": team.pitch_preference,
            "team_color": team.team_color,
            "created_by_email": team.user_id,
            "profiles": profiles_dict,
        }
