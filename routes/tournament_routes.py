"""Tournament route registration."""

import json
import os

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func as sa_func
from utils.exception_tracker import log_exception


_KNOCKOUT_STAGE_LABELS = {
    "final": "Final",
    "knockout_sf": "Semi Final",
    "knockout_qf": "Quarter Final",
    "knockout_r2": "Round of 16",
    "knockout_r1": "Round 1",
    "completed": "Completed",
}


def _format_knockout_stage(stage):
    """Human-readable label for a Tournament.current_stage value in
    Knockout mode. Named stages get a proper cricket-tournament label;
    the generic 'round_N' fallback (used for brackets larger than 16
    teams) just gets title-cased.
    """
    if not stage:
        return "—"
    return _KNOCKOUT_STAGE_LABELS.get(stage, stage.replace("_", " ").title())


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

    def _cleanup_match_artifacts(match, *, reverse_stats=True, rebuild_player_cache=True):
        """
        Delete all artifacts for a single DBMatch: reverse career aggregates,
        remove scorecards/partnerships, delete JSON file, purge memory cache,
        and rebuild the affected players' tournament stats cache so it stops
        showing this (now-deleted) match's runs/wickets.
        Caller is responsible for deleting the DBMatch itself and committing.
        """
        match_id = match.id

        scorecards = MatchScorecard.query.filter_by(match_id=match_id).all()
        player_ids = {c.player_id for c in scorecards}

        # 1. Reverse player career stats
        if reverse_stats and scorecards:
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

        # 5. Rebuild the affected players' tournament stats cache. Skipped
        # when the whole tournament (and its cache rows) is being deleted
        # right after — rebuilding first would just be discarded work.
        if rebuild_player_cache and player_ids and match.tournament_id:
            tournament_engine.rebuild_player_stats_cache(match.tournament_id, player_ids)

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

            if mode not in tournament_engine.MIN_TEAMS:
                flash("Invalid tournament format selected.", "error")
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

        # Tournament Leaders — live aggregates within this tournament's
        # matches, top 5 each. Computed directly over DBMatch/MatchScorecard
        # rather than via TournamentPlayerStatsCache: that cache is only
        # rebuilt once per match, but update_standings() (which triggers
        # the rebuild) runs before that same match's MatchScorecard rows
        # are persisted (Step 10 vs Step 13 in the match-completion flow in
        # app.py), so the cache always lags one match behind for whoever
        # just played. Querying live avoids that lag.
        motm_rows = (
            db.session.query(DBMatch.motm_player_id, sa_func.count(DBMatch.id))
            .filter(
                DBMatch.tournament_id == tournament_id,
                DBMatch.motm_player_id.isnot(None),
            )
            .group_by(DBMatch.motm_player_id)
            .order_by(sa_func.count(DBMatch.id).desc())
            .limit(5)
            .all()
        )

        top_scorer_rows = (
            db.session.query(MatchScorecard.player_id, sa_func.sum(MatchScorecard.runs))
            .join(DBMatch, MatchScorecard.match_id == DBMatch.id)
            .filter(
                DBMatch.tournament_id == tournament_id,
                MatchScorecard.record_type == "batting",
                MatchScorecard.is_super_over.isnot(True),
            )
            .group_by(MatchScorecard.player_id)
            .order_by(sa_func.sum(MatchScorecard.runs).desc())
            .limit(5)
            .all()
        )

        top_wicket_rows = (
            db.session.query(
                MatchScorecard.player_id,
                sa_func.sum(MatchScorecard.wickets),
                sa_func.sum(MatchScorecard.runs_conceded),
            )
            .join(DBMatch, MatchScorecard.match_id == DBMatch.id)
            .filter(
                DBMatch.tournament_id == tournament_id,
                MatchScorecard.record_type == "bowling",
                MatchScorecard.is_super_over.isnot(True),
            )
            .group_by(MatchScorecard.player_id)
            .order_by(
                sa_func.sum(MatchScorecard.wickets).desc(),
                sa_func.sum(MatchScorecard.runs_conceded).asc(),
            )
            .limit(5)
            .all()
        )

        # Resolve players/teams for all three leaderboards in one pair of
        # queries instead of three.
        all_player_ids = (
            {pid for pid, _ in motm_rows}
            | {pid for pid, _ in top_scorer_rows}
            | {pid for pid, _, _ in top_wicket_rows}
        )
        players = {}
        teams = {}
        if all_player_ids:
            players = {
                p.id: p for p in DBPlayer.query.filter(DBPlayer.id.in_(all_player_ids)).all()
            }
            teams = {
                t2.id: t2 for t2 in DBTeam.query.filter(
                    DBTeam.id.in_([p.team_id for p in players.values() if p.team_id])
                ).all()
            }

        motm_leaderboard = []
        for player_id, count in motm_rows:
            player = players.get(player_id)
            if not player:
                continue
            team = teams.get(player.team_id)
            motm_leaderboard.append({
                "player_name": player.name,
                "team_name": team.name if team else "",
                "awards": count,
            })

        top_run_scorers = []
        for player_id, runs in top_scorer_rows:
            player = players.get(player_id)
            if not player:
                continue
            team = teams.get(player.team_id)
            top_run_scorers.append({
                "player_id": player_id,
                "player_name": player.name,
                "team_name": team.name if team else "",
                "runs": runs,
            })

        top_wicket_takers = []
        for player_id, wickets, _conceded in top_wicket_rows:
            player = players.get(player_id)
            if not player:
                continue
            team = teams.get(player.team_id)
            top_wicket_takers.append({
                "player_id": player_id,
                "player_name": player.name,
                "team_name": team.name if team else "",
                "wickets": wickets,
            })

        # Pure Knockout is the only mode whose fixtures are never staged
        # 'league' (see TournamentEngine.update_standings), so its
        # TournamentTeam rows never accumulate real W/L/points/NRR — a
        # Points Table for it would always read all-zero with arbitrary
        # ordering. Every other mode (round robin, the league+playoffs
        # modes, and custom series) has a genuine league stage.
        has_league_standings = t.mode != tournament_engine.MODE_KNOCKOUT
        current_round_label = (
            _format_knockout_stage(t.current_stage) if not has_league_standings else None
        )

        return render_template(
            "tournaments/dashboard.html",
            tournament=t,
            standings=standings,
            fixtures=fixtures,
            next_fixture_id=next_fixture_id,
            motm_leaderboard=motm_leaderboard,
            top_run_scorers=top_run_scorers,
            top_wicket_takers=top_wicket_takers,
            has_league_standings=has_league_standings,
            current_round_label=current_round_label,
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
        if not t or t.user_id != current_user.id:
            flash("Tournament not found or you don't have permission.", "danger")
            return redirect(url_for("tournaments"))

        try:
            tournament_matches = DBMatch.query.filter_by(tournament_id=tournament_id).all()
            match_ids = [m.id for m in tournament_matches]

            # Null tournament_fixtures.match_id before deleting the matches —
            # the FK has no ON DELETE cascade, so the match delete would
            # otherwise fail with an IntegrityError under Postgres.
            if match_ids:
                db.session.query(TournamentFixture).filter(
                    TournamentFixture.match_id.in_(match_ids)
                ).update({TournamentFixture.match_id: None}, synchronize_session=False)

            for m in tournament_matches:
                # The whole tournament (and its TournamentPlayerStatsCache
                # rows) is deleted right after this loop, so rebuilding the
                # per-match player cache here would just be discarded work.
                _cleanup_match_artifacts(m, rebuild_player_cache=False)
                db.session.delete(m)

            # Tournament cascades to TournamentTeam, TournamentFixture, and
            # TournamentPlayerStatsCache via relationship cascade on the model.
            db.session.delete(t)
            db.session.commit()
            flash("Tournament deleted successfully.", "success")
        except Exception as e:
            log_exception(e)
            db.session.rollback()
            app.logger.error(f"Error deleting tournament {tournament_id}: {e}", exc_info=True)
            flash(
                f"Could not delete the tournament: {type(e).__name__}. "
                "Please contact support if this keeps happening.",
                "danger",
            )
        return redirect(url_for("tournaments"))

    @app.route("/fixture/<fixture_id>/resimulate", methods=["POST"])
    @login_required
    @limiter.limit("10 per minute")
    def resimulate_fixture(fixture_id):
        """Reset a fixture to Scheduled and clear old simulation artifacts."""
        # Bound up front so the except handler's `fixture if fixture else 0`
        # can never raise UnboundLocalError if the very first lookup below
        # is what fails.
        fixture = None
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
