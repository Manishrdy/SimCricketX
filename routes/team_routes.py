"""Team management route registration."""

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required


def register_team_routes(
    app,
    *,
    db,
    Player,
    DBTeam,
    DBPlayer,
):
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
                    return render_template("team_create.html", error="All team fields are required.")

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

                player_names = request.form.getlist("player_name")
                roles = request.form.getlist("player_role")
                bat_ratings = request.form.getlist("batting_rating")
                bowl_ratings = request.form.getlist("bowling_rating")
                field_ratings = request.form.getlist("fielding_rating")
                bat_hands = request.form.getlist("batting_hand")
                bowl_types = request.form.getlist("bowling_type")
                bowl_hands = request.form.getlist("bowling_hand")

                players = []
                for i in range(len(player_names)):
                    try:
                        b_type = bowl_types[i] if i < len(bowl_types) else ""
                        b_hand = bowl_hands[i] if i < len(bowl_hands) else ""

                        bat_r = int(bat_ratings[i])
                        bowl_r = int(bowl_ratings[i])
                        field_r = int(field_ratings[i])

                        if not (0 <= bat_r <= 100 and 0 <= bowl_r <= 100 and 0 <= field_r <= 100):
                            return render_template(
                                "team_create.html",
                                error=f"Player {i+1}: All ratings must be between 0 and 100.",
                            )

                        player = Player(
                            name=player_names[i],
                            role=roles[i],
                            batting_rating=bat_r,
                            bowling_rating=bowl_r,
                            fielding_rating=field_r,
                            batting_hand=bat_hands[i],
                            bowling_type=b_type if b_type else "",
                            bowling_hand=b_hand if b_hand else "",
                        )
                        players.append(player)
                    except Exception as e:
                        app.logger.error(f"Error in player creation: {e}", exc_info=True)
                        return render_template("team_create.html", error=f"Error in player {i+1}: {e}")

                if not is_draft:
                    if len(players) < 12 or len(players) > 25:
                        return render_template(
                            "team_create.html",
                            error="You must enter between 12 and 25 players.",
                        )

                    wk_count = sum(1 for p in players if p.role == "Wicketkeeper")
                    if wk_count < 1:
                        return render_template(
                            "team_create.html",
                            error="You need at least one Wicketkeeper.",
                        )

                    bowl_count = sum(1 for p in players if p.role in ["Bowler", "All-rounder"])
                    if bowl_count < 6:
                        return render_template(
                            "team_create.html",
                            error="You need at least six Bowler/All-rounder roles.",
                        )

                    captain_name = request.form.get("captain")
                    wk_name = request.form.get("wicketkeeper")
                    if not captain_name or not wk_name:
                        return render_template(
                            "team_create.html",
                            error="Captain and Wicketkeeper selection required.",
                        )
                else:
                    if len(players) < 1:
                        return render_template(
                            "team_create.html",
                            error="Draft must have at least 1 player.",
                        )
                    captain_name = request.form.get("captain")
                    wk_name = request.form.get("wicketkeeper")

                color = request.form["team_color"]

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

                    for p in players:
                        is_captain = (p.name == captain_name) if captain_name else False
                        is_wk = (p.name == wk_name) if wk_name else False

                        db_player = DBPlayer(
                            team_id=new_team.id,
                            name=p.name,
                            role=p.role,
                            batting_rating=p.batting_rating,
                            bowling_rating=p.bowling_rating,
                            fielding_rating=p.fielding_rating,
                            batting_hand=p.batting_hand,
                            bowling_type=p.bowling_type,
                            bowling_hand=p.bowling_hand,
                            is_captain=is_captain,
                            is_wicketkeeper=is_wk,
                        )
                        db.session.add(db_player)

                    db.session.commit()
                    status_msg = "Draft" if is_draft else "Active"
                    app.logger.info(
                        f"Team '{new_team.name}' (ID: {new_team.id}) created as {status_msg} by {current_user.id}"
                    )
                    if is_draft:
                        flash("Team saved as draft.", "success")
                        return redirect(url_for("manage_teams"))
                    return redirect(url_for("home"))

                except Exception as db_err:
                    db.session.rollback()
                    app.logger.error(f"Database error saving team: {db_err}", exc_info=True)
                    return render_template("team_create.html", error="Database error saving team.")

            except Exception as e:
                app.logger.error(f"Unexpected error saving team: {e}", exc_info=True)
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
                players_list = []
                for p in t.players:
                    players_list.append(
                        {
                            "role": p.role,
                            "name": p.name,
                            "is_captain": p.is_captain,
                            "is_wicketkeeper": p.is_wicketkeeper,
                        }
                    )

                captain_name = next((p.name for p in t.players if p.is_captain), "Unknown")
                teams.append(
                    {
                        "id": t.id,
                        "team_name": t.name,
                        "short_code": t.short_code,
                        "home_ground": t.home_ground,
                        "pitch_preference": t.pitch_preference,
                        "team_color": t.team_color,
                        "captain": captain_name,
                        "players": players_list,
                        "is_draft": getattr(t, "is_draft", False),
                    }
                )
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
                    f"Delete failed: Team '{short_code}' not found or unauthorized for {current_user.id}"
                )
                flash("Team not found or you don't have permission to delete it.", "danger")
                return redirect(url_for("manage_teams"))

            team_name = team.name
            db.session.delete(team)
            db.session.commit()

            app.logger.info(f"Team '{short_code}' (ID: {team.id}) deleted by {current_user.id}")
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
            team = DBTeam.query.filter_by(short_code=short_code, user_id=user_id).first()
            if not team:
                app.logger.warning(
                    f"Edit failed: Team '{short_code}' not found or unauthorized for {user_id}"
                )
                return redirect(url_for("manage_teams"))

            if request.method == "POST":
                try:
                    team.name = request.form["team_name"].strip()
                    new_short_code = request.form["short_code"].strip().upper()
                    team.home_ground = request.form["home_ground"].strip()
                    team.pitch_preference = request.form["pitch_preference"]
                    team.team_color = request.form["team_color"]

                    if new_short_code != team.short_code:
                        conflict = DBTeam.query.filter_by(
                            user_id=user_id,
                            short_code=new_short_code,
                        ).first()
                        if conflict:
                            return render_template(
                                "team_create.html",
                                team={
                                    "team_name": team.name,
                                    "short_code": new_short_code,
                                    "home_ground": team.home_ground,
                                    "pitch_preference": team.pitch_preference,
                                    "team_color": team.team_color,
                                },
                                edit=True,
                                error=(
                                    f"You already have a team with short code '{new_short_code}'."
                                ),
                            )

                    action = request.form.get("action", "publish")
                    is_draft = action == "save_draft"

                    names = request.form.getlist("player_name")
                    roles = request.form.getlist("player_role")
                    bats = request.form.getlist("batting_rating")
                    bowls = request.form.getlist("bowling_rating")
                    fields = request.form.getlist("fielding_rating")
                    bhands = request.form.getlist("batting_hand")
                    btypes = request.form.getlist("bowling_type")
                    bhand2s = request.form.getlist("bowling_hand")

                    captain_name = request.form.get("captain")
                    wk_name = request.form.get("wicketkeeper")

                    team_form_data = {
                        "team_name": team.name,
                        "short_code": new_short_code,
                        "home_ground": team.home_ground,
                        "pitch_preference": team.pitch_preference,
                        "team_color": team.team_color,
                        "created_by_email": team.user_id,
                        "captain": captain_name or "",
                        "wicketkeeper": wk_name or "",
                        "players": [],
                    }
                    for i in range(len(names)):
                        safe_btype = btypes[i] if i < len(btypes) else ""
                        safe_bhand2 = bhand2s[i] if i < len(bhand2s) else ""
                        team_form_data["players"].append(
                            {
                                "name": names[i],
                                "role": roles[i],
                                "batting_rating": bats[i],
                                "bowling_rating": bowls[i],
                                "fielding_rating": fields[i],
                                "batting_hand": bhands[i],
                                "bowling_type": safe_btype or "",
                                "bowling_hand": safe_bhand2 or "",
                            }
                        )

                    if not is_draft:
                        if len(names) < 12 or len(names) > 25:
                            return render_template(
                                "team_create.html",
                                team=team_form_data,
                                edit=True,
                                error="Active teams must have 12-25 players.",
                            )

                        wk_count = roles.count("Wicketkeeper")
                        bowl_count = sum(1 for r in roles if r in ["Bowler", "All-rounder"])

                        if wk_count < 1:
                            return render_template(
                                "team_create.html",
                                team=team_form_data,
                                edit=True,
                                error="Active teams need at least one Wicketkeeper.",
                            )
                        if bowl_count < 6:
                            return render_template(
                                "team_create.html",
                                team=team_form_data,
                                edit=True,
                                error="Active teams need at least six Bowler/All-rounder roles.",
                            )
                        if not captain_name or not wk_name:
                            return render_template(
                                "team_create.html",
                                team=team_form_data,
                                edit=True,
                                error="Active teams require a Captain and Wicketkeeper.",
                            )
                    else:
                        if len(names) < 1:
                            return render_template(
                                "team_create.html",
                                team=team_form_data,
                                edit=True,
                                error="Drafts must have at least 1 player.",
                            )

                    for i in range(len(names)):
                        bat_r = int(bats[i])
                        bowl_r = int(bowls[i])
                        field_r = int(fields[i])
                        if not (
                            0 <= bat_r <= 100
                            and 0 <= bowl_r <= 100
                            and 0 <= field_r <= 100
                        ):
                            return render_template(
                                "team_create.html",
                                team=team_form_data,
                                edit=True,
                                error=f"Player {i+1}: All ratings must be between 0 and 100.",
                            )

                    team.is_draft = is_draft
                    team.short_code = new_short_code
                    DBPlayer.query.filter_by(team_id=team.id).delete()

                    for i in range(len(names)):
                        p_name = names[i]
                        safe_btype = btypes[i] if i < len(btypes) else ""
                        safe_bhand2 = bhand2s[i] if i < len(bhand2s) else ""
                        db_player = DBPlayer(
                            team_id=team.id,
                            name=p_name,
                            role=roles[i],
                            batting_rating=int(bats[i]),
                            bowling_rating=int(bowls[i]),
                            fielding_rating=int(fields[i]),
                            batting_hand=bhands[i],
                            bowling_type=safe_btype or "",
                            bowling_hand=safe_bhand2 or "",
                            is_captain=(p_name == captain_name) if captain_name else False,
                            is_wicketkeeper=(p_name == wk_name) if wk_name else False,
                        )
                        db.session.add(db_player)

                    db.session.commit()
                    status_msg = "Draft" if is_draft else "Active"
                    app.logger.info(
                        f"Team '{team.short_code}' (ID: {team.id}) updated as {status_msg} by {user_id}"
                    )
                    flash(f"Team updated as {status_msg}.", "success")
                    return redirect(url_for("manage_teams"))
                except Exception as e:
                    db.session.rollback()
                    app.logger.error(f"Error updating team: {e}", exc_info=True)
                    flash("An error occurred while updating the team. Please try again.", "danger")
                    return redirect(url_for("edit_team", short_code=short_code))

            team_data = {
                "team_name": team.name,
                "short_code": team.short_code,
                "home_ground": team.home_ground,
                "pitch_preference": team.pitch_preference,
                "team_color": team.team_color,
                "created_by_email": team.user_id,
                "captain": next((p.name for p in team.players if p.is_captain), ""),
                "wicketkeeper": next((p.name for p in team.players if p.is_wicketkeeper), ""),
                "players": [],
            }
            for p in team.players:
                team_data["players"].append(
                    {
                        "name": p.name,
                        "role": p.role,
                        "batting_rating": p.batting_rating,
                        "bowling_rating": p.bowling_rating,
                        "fielding_rating": p.fielding_rating,
                        "batting_hand": p.batting_hand,
                        "bowling_type": p.bowling_type,
                        "bowling_hand": p.bowling_hand,
                    }
                )
            team_data["captain"] = next((p.name for p in team.players if p.is_captain), "")
            team_data["wicketkeeper"] = next(
                (p.name for p in team.players if p.is_wicketkeeper),
                "",
            )

            return render_template("team_create.html", team=team_data, edit=True)
        except Exception as e:
            app.logger.error(f"Error in edit_team: {e}", exc_info=True)
            return redirect(url_for("manage_teams"))
