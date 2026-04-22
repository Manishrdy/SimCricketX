"""Tournament route registration."""

import json
import os

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from utils.exception_tracker import log_exception


def register_tournament_routes(
    app,
    *,
    db,
    limiter,
    tournament_engine,
    Tournament,
    TournamentPlayerStatsCache,
    DBTeam,
    DBMatch,
    DBPlayer,
    MatchScorecard,
    MatchPartnership,
    TournamentFixture,
    reverse_player_aggregates,
    MATCH_INSTANCES,
    MATCH_INSTANCES_LOCK,
    PROJECT_ROOT,
):
    # ── Shared helper ─────────────────────────────────────────────────────

    def _cleanup_match_artifacts(match, *, reverse_stats=True):
        """
        Delete all artifacts for a single DBMatch: reverse career aggregates,
        remove scorecards/partnerships, delete JSON file, purge memory cache.
        Caller is responsible for deleting the DBMatch itself and committing.
        """
        match_id = match.id

        # 1. Reverse player career stats
        if reverse_stats:
            scorecards = MatchScorecard.query.filter_by(match_id=match_id).all()
            if scorecards:
                reverse_player_aggregates(scorecards, logger=app.logger)

        # 2. Delete dependent records
        db.session.query(MatchPartnership).filter_by(match_id=match_id).delete(
            synchronize_session=False
        )
        db.session.query(MatchScorecard).filter_by(match_id=match_id).delete(
            synchronize_session=False
        )

        # 3. Delete JSON file — O(1) via stored path, O(N) fallback
        _delete_match_json(match)

        # 4. Purge from in-memory cache
        with MATCH_INSTANCES_LOCK:
            MATCH_INSTANCES.pop(match_id, None)

    def _delete_match_json(match):
        """Remove match JSON file from disk using stored path or fallback scan."""
        match_dir = os.path.join(PROJECT_ROOT, "data", "matches")

        # O(1) — use stored path
        if match.match_json_path:
            json_path = match.match_json_path
            if not os.path.isabs(json_path):
                json_path = os.path.join(match_dir, json_path)
            if os.path.isfile(json_path):
                try:
                    os.remove(json_path)
                    return
                except Exception as e:
                    log_exception(e)
                    app.logger.warning(f"Failed to delete JSON via stored path {json_path}: {e}")

        # O(1) — try canonical name
        canonical = os.path.join(match_dir, f"match_{match.id}.json")
        if os.path.isfile(canonical):
            try:
                os.remove(canonical)
                return
            except Exception:
                log_exception(source="backend")
                pass

        # O(N) fallback — only if directory exists
        if not os.path.isdir(match_dir):
            return
        for fn in os.listdir(match_dir):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(match_dir, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("match_id") == match.id:
                    os.remove(path)
                    break
            except Exception:
                log_exception(source="backend")
                continue

    # ── Routes ────────────────────────────────────────────────────────────

    @app.route("/tournaments")
    @login_required
    def tournaments():
        user_tournaments = (
            Tournament.query.filter_by(user_id=current_user.id)
            .order_by(Tournament.created_at.desc())
            .all()
        )
        return render_template("tournaments/dashboard_list.html", tournaments=user_tournaments)

    @app.route("/tournaments/create", methods=["GET", "POST"])
    @login_required
    @limiter.limit("10 per minute")
    def create_tournament_route():
        VALID_TOURNAMENT_FORMATS = {"T20", "ListA"}

        if request.method == "POST":
            name = request.form.get("name")
            team_ids = request.form.getlist("team_ids")
            mode = request.form.get("mode", "round_robin")
            match_format = request.form.get("match_format", "T20").strip()

            if match_format not in VALID_TOURNAMENT_FORMATS:
                flash("Invalid match format selected.", "error")
                return redirect(url_for("create_tournament_route"))

            if not name or len(team_ids) < 2:
                flash("Please provide a tournament name and select at least 2 teams.", "error")
                return redirect(url_for("create_tournament_route"))

            try:
                team_ids = [int(tid) for tid in team_ids]

                owned_team_ids = {
                    team.id
                    for team in DBTeam.query.filter_by(user_id=current_user.id)
                    .filter(DBTeam.id.in_(team_ids), DBTeam.is_placeholder != True)
                    .all()
                }
                if len(owned_team_ids) != len(team_ids):
                    flash("One or more selected teams are not owned by you.", "error")
                    return redirect(url_for("create_tournament_route"))

                series_config = None
                if mode == "custom_series":
                    if len(team_ids) != 2:
                        flash("Custom series requires exactly 2 teams.", "error")
                        return redirect(url_for("create_tournament_route"))

                    num_matches = int(request.form.get("series_matches", 3))
                    series_config = {
                        "series_name": name,
                        "matches": [],
                    }
                    for i in range(num_matches):
                        series_config["matches"].append(
                            {
                                "match_num": i + 1,
                                "home": i % 2,
                                "venue_name": f"Match {i + 1}",
                            }
                        )

                min_teams = tournament_engine.MIN_TEAMS.get(mode, 2)
                if len(team_ids) < min_teams:
                    flash(
                        f"{mode.replace('_', ' ').title()} requires at least {min_teams} teams.",
                        "error",
                    )
                    return redirect(url_for("create_tournament_route"))

                t = tournament_engine.create_tournament(
                    name=name,
                    user_id=current_user.id,
                    team_ids=team_ids,
                    mode=mode,
                    series_config=series_config,
                    format_type=match_format,
                )
                flash(f"Tournament '{name}' created successfully!", "success")
                return redirect(url_for("tournament_dashboard", tournament_id=t.id))
            except ValueError as e:
                log_exception(e)
                flash(str(e), "error")
                return redirect(url_for("create_tournament_route"))
            except Exception as e:
                log_exception(e)
                app.logger.error(f"Error creating tournament: {e}", exc_info=True)
                flash("An error occurred while creating the tournament.", "error")
                return redirect(url_for("create_tournament_route"))

        teams = DBTeam.query.filter_by(user_id=current_user.id).filter(DBTeam.is_placeholder != True).all()
        num_teams = len(teams)
        available_modes = tournament_engine.get_available_modes(num_teams) if num_teams >= 2 else []

        # Build a map of {team_id: [format_types]} so the template JS can filter
        # teams based on the selected match format.
        team_formats = {
            t.id: [p.format_type for p in t.profiles]
            for t in teams
        }

        return render_template(
            "tournaments/create.html",
            teams=teams,
            available_modes=available_modes,
            team_formats_json=json.dumps(team_formats),
        )

    @app.route("/tournaments/<int:tournament_id>")
    @login_required
    def tournament_dashboard(tournament_id):
        t = db.session.get(Tournament, tournament_id)
        if not t or t.user_id != current_user.id:
            return "Tournament not found", 404

        standings = tournament_engine.get_standings(tournament_id)

        # Eagerly load fixtures with relationships to avoid N+1
        fixtures = (
            TournamentFixture.query
            .filter_by(tournament_id=tournament_id)
            .order_by(TournamentFixture.round_number, TournamentFixture.id)
            .all()
        )

        # Identify the next scheduled fixture for highlighting
        next_fixture_id = None
        for f in fixtures:
            if f.status == "Scheduled":
                next_fixture_id = f.id
                break

        return render_template(
            "tournaments/dashboard.html",
            tournament=t,
            standings=standings,
            fixtures=fixtures,
            next_fixture_id=next_fixture_id,
        )

    @app.route("/tournaments/<int:tournament_id>/rename", methods=["POST"])
    @login_required
    @limiter.limit("10 per minute")
    def rename_tournament(tournament_id):
        t = db.session.get(Tournament, tournament_id)
        if not t or t.user_id != current_user.id:
            flash("Tournament not found.", "danger")
            return redirect(url_for("tournaments"))

        new_name = (request.form.get("name") or "").strip()
        if not new_name:
            flash("Tournament name cannot be empty.", "error")
            return redirect(url_for("tournament_dashboard", tournament_id=tournament_id))

        if len(new_name) > 100:
            flash("Tournament name must be 100 characters or less.", "error")
            return redirect(url_for("tournament_dashboard", tournament_id=tournament_id))

        t.name = new_name
        db.session.commit()
        flash(f"Tournament renamed to '{new_name}'.", "success")
        return redirect(url_for("tournament_dashboard", tournament_id=tournament_id))

    @app.route("/tournaments/<int:tournament_id>/delete", methods=["POST"])
    @login_required
    @limiter.limit("5 per minute")
    def delete_tournament(tournament_id):
        t = db.session.get(Tournament, tournament_id)
        if t and t.user_id == current_user.id:
            tournament_matches = DBMatch.query.filter_by(tournament_id=tournament_id).all()
            for m in tournament_matches:
                _cleanup_match_artifacts(m)
                db.session.delete(m)

            db.session.delete(t)
            db.session.commit()
            flash("Tournament deleted successfully.", "success")
        return redirect(url_for("tournaments"))

    @app.route("/fixture/<fixture_id>/resimulate", methods=["POST"])
    @login_required
    @limiter.limit("10 per minute")
    def resimulate_fixture(fixture_id):
        """Reset a fixture to Scheduled and clear old simulation artifacts."""
        try:
            fixture = db.session.get(TournamentFixture, fixture_id)
            if not fixture:
                flash("Fixture not found.", "danger")
                return redirect(url_for("tournaments"))

            if fixture.tournament.user_id != current_user.id:
                flash("Unauthorized to modify this fixture.", "danger")
                return redirect(url_for("tournament_dashboard", tournament_id=fixture.tournament_id))

            match_id = fixture.match_id
            if not match_id:
                flash("No match data found to reset.", "warning")
                return redirect(url_for("tournament_dashboard", tournament_id=fixture.tournament_id))

            db_match = db.session.get(DBMatch, match_id)
            if db_match:
                app.logger.info(f"Reversing stats for match {match_id}")
                reversed_ok = tournament_engine.reverse_standings(db_match, commit=False)
                if not reversed_ok:
                    fixture.status = "Scheduled"
                    fixture.winner_team_id = None
                    fixture.match_id = None
                    fixture.standings_applied = False

                _cleanup_match_artifacts(db_match)
                db.session.delete(db_match)
            else:
                fixture.status = "Scheduled"
                fixture.winner_team_id = None
                fixture.match_id = None
                fixture.standings_applied = False

            db.session.commit()
            flash("Match reset successfully. You can now re-simulate.", "success")
            return redirect(
                url_for("match_setup", fixture_id=fixture.id, tournament_id=fixture.tournament_id)
            )
        except Exception as e:
            log_exception(e)
            db.session.rollback()
            app.logger.error(f"Resimulation error: {e}", exc_info=True)
            flash("Failed to reset match.", "danger")
            return redirect(
                url_for(
                    "tournament_dashboard",
                    tournament_id=fixture.tournament_id if fixture else 0,
                )
            )
