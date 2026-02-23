"""Statistics and player-comparison route registration."""

from flask import Response, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required


def register_stats_routes(
    app,
    *,
    limiter,
    db,
    Team,
    Tournament,
    DBPlayer,
    DBTeam,
    DBMatch,
    MatchScorecard,
    MatchPartnership,
    StatsService,
    aliased,
    func,
):
    @app.route("/statistics")
    @login_required
    def statistics():
        """Display statistics dashboard with overall or tournament-specific stats."""
        try:
            stats_service = StatsService(logger=app.logger)

            view_type = request.args.get("view", "overall")
            tournament_id = request.args.get("tournament_id", type=int)
            tournaments = Tournament.query.filter_by(user_id=current_user.id).all()

            stats_data = None
            has_stats = False

            if view_type == "overall":
                app.logger.info(f"Fetching overall stats for user {current_user.id}")
                stats_data = stats_service.get_overall_stats(current_user.id)
            elif view_type == "tournament" and tournament_id:
                app.logger.info(
                    f"Fetching tournament stats for user {current_user.id}, tournament {tournament_id}"
                )
                stats_data = stats_service.get_tournament_stats(current_user.id, tournament_id)

            if stats_data and (
                stats_data["batting"] or stats_data["bowling"] or stats_data["fielding"]
            ):
                has_stats = True

            batting_headers = [
                "Player",
                "Team",
                "Matches",
                "Innings",
                "Runs",
                "Balls",
                "Not Outs",
                "Strike Rate",
                "Average",
                "0s",
                "1s",
                "2s",
                "3s",
                "4s",
                "6s",
                "30s",
                "50s",
                "100s",
            ]
            bowling_headers = [
                "Team",
                "Player",
                "Matches",
                "Innings",
                "Overs",
                "Runs",
                "Wickets",
                "Best",
                "Average",
                "Economy",
                "Dots",
                "Bowled",
                "LBW",
                "Byes",
                "Leg Byes",
                "Wides",
                "No Balls",
            ]
            fielding_headers = ["Player", "Team", "Matches", "Catches", "Run Outs"]

            if stats_data is not None:
                figures_tournament = tournament_id if view_type == "tournament" else None
                best_figures = stats_service.get_bowling_figures_leaderboard(
                    current_user.id,
                    figures_tournament,
                    limit=5,
                )
                stats_data.setdefault("leaderboards", {})
                stats_data["leaderboards"]["best_bowling_figures"] = best_figures

            insights = (
                stats_service.get_insights(
                    current_user.id,
                    tournament_id if view_type == "tournament" else None,
                )
                if stats_data
                else {}
            )

            return render_template(
                "statistics.html",
                view_type=view_type,
                tournament_id=tournament_id,
                tournaments=tournaments,
                has_stats=has_stats,
                batting_stats=stats_data["batting"] if stats_data else [],
                bowling_stats=stats_data["bowling"] if stats_data else [],
                fielding_stats=stats_data["fielding"] if stats_data else [],
                leaderboards=stats_data["leaderboards"] if stats_data else {},
                insights=insights,
                batting_headers=batting_headers,
                bowling_headers=bowling_headers,
                fielding_headers=fielding_headers,
                user=current_user,
            )
        except Exception as e:
            app.logger.error(f"Error in statistics route: {e}", exc_info=True)
            flash("Error loading statistics", "danger")
            return render_template("statistics.html", has_stats=False, user=current_user)

    @app.route("/statistics/export/<stat_type>/<format_type>")
    @login_required
    def export_statistics(stat_type, format_type):
        """Export statistics to CSV or TXT format."""
        try:
            stats_service = StatsService(logger=app.logger)

            view_type = request.args.get("view", "overall")
            tournament_id = request.args.get("tournament_id", type=int)

            if view_type == "overall":
                stats_data = stats_service.get_overall_stats(current_user.id)
            elif tournament_id:
                stats_data = stats_service.get_tournament_stats(current_user.id, tournament_id)
            else:
                return jsonify({"error": "Please select a tournament"}), 400

            if stat_type == "batting":
                data = stats_data["batting"]
            elif stat_type == "bowling":
                data = stats_data["bowling"]
            elif stat_type == "fielding":
                data = stats_data["fielding"]
            else:
                return jsonify({"error": "Invalid stat type"}), 400

            if not data:
                return jsonify({"error": f"No {stat_type} data available"}), 404

            view_label = f"tournament_{tournament_id}" if view_type == "tournament" else "overall"
            filename = f"{view_label}_{stat_type}_stats.{format_type}"

            if format_type == "csv":
                content = stats_service.export_to_csv(data, stat_type)
                mimetype = "text/csv"
            elif format_type == "txt":
                content = stats_service.export_to_txt(data, stat_type)
                mimetype = "text/plain"
            else:
                return jsonify({"error": "Invalid format type"}), 400

            return Response(
                content,
                mimetype=mimetype,
                headers={"Content-Disposition": f"attachment;filename={filename}"},
            )
        except Exception as e:
            app.logger.error(f"Error exporting statistics: {e}", exc_info=True)
            return jsonify({"error": "Error exporting statistics"}), 500

    @app.route("/compare-players")
    @login_required
    def compare_players_page():
        """Render player comparison page."""
        try:
            teams = DBTeam.query.filter_by(user_id=current_user.id).filter(DBTeam.is_placeholder != True).all()
            tournaments = Tournament.query.filter_by(user_id=current_user.id).all()

            return render_template(
                "compare_players.html",
                teams=teams,
                tournaments=tournaments,
            )
        except Exception as e:
            app.logger.error(f"Error loading comparison page: {e}", exc_info=True)
            flash("Error loading comparison page", "danger")
            return redirect(url_for("home"))

    @app.route("/api/bowling-figures")
    @login_required
    @limiter.limit("30 per minute")
    def api_bowling_figures():
        """API endpoint for best bowling figures leaderboard."""
        try:
            tournament_id = request.args.get("tournament_id", type=int)
            limit = request.args.get("limit", 10, type=int)

            if limit < 1 or limit > 100:
                return jsonify({"error": "Limit must be between 1 and 100"}), 400

            stats_service = StatsService(app.logger)
            figures = stats_service.get_bowling_figures_leaderboard(
                current_user.id,
                tournament_id,
                limit,
            )

            return jsonify(
                {
                    "success": True,
                    "data": figures,
                    "count": len(figures),
                }
            )
        except Exception as e:
            app.logger.error(f"Error fetching bowling figures: {e}", exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500

    @app.route("/api/compare-players")
    @login_required
    @limiter.limit("30 per minute")
    def api_compare_players():
        """API endpoint for player comparison."""
        try:
            player_ids_str = request.args.get("player_ids", "")
            player_ids = [int(x.strip()) for x in player_ids_str.split(",") if x.strip().isdigit()]
            tournament_id = request.args.get("tournament_id", type=int)

            if not player_ids:
                stats_service = StatsService(app.logger)
                players_with_stats = (
                    db.session.query(
                        DBPlayer.id,
                        DBPlayer.name,
                        DBTeam.name.label("team_name"),
                        func.sum(db.case((MatchScorecard.record_type == "batting", 1), else_=0)).label("batting_count"),
                        func.sum(db.case((MatchScorecard.record_type == "bowling", 1), else_=0)).label("bowling_count"),
                    )
                    .join(MatchScorecard, DBPlayer.id == MatchScorecard.player_id)
                    .join(DBMatch, MatchScorecard.match_id == DBMatch.id)
                    .join(DBTeam, DBPlayer.team_id == DBTeam.id)
                    .filter(DBMatch.user_id == current_user.id)
                    .group_by(DBPlayer.id, DBPlayer.name, DBTeam.name)
                    .all()
                )

                available_players = []
                for player_id, player_name, team_name, batting_count, bowling_count in players_with_stats:
                    has_batting = (batting_count or 0) > 0
                    has_bowling = (bowling_count or 0) > 0

                    if has_batting and has_bowling:
                        role = "All-Rounder"
                    elif has_bowling:
                        role = "Bowler"
                    elif has_batting:
                        role = "Batsman"
                    else:
                        role = "Unknown"

                    available_players.append(
                        {
                            "id": player_id,
                            "name": player_name,
                            "team": team_name,
                            "role": role,
                        }
                    )

                return jsonify(
                    {
                        "success": True,
                        "available_players": available_players,
                        "count": len(available_players),
                    }
                )

            if len(player_ids) < 2:
                return jsonify({"error": "Select at least 2 players to compare"}), 400
            if len(player_ids) > 6:
                return jsonify({"error": "Maximum 6 players can be compared at once"}), 400

            stats_service = StatsService(app.logger)
            comparison = stats_service.compare_players(current_user.id, player_ids, tournament_id)

            if "error" in comparison:
                return jsonify(comparison), 400

            players = comparison.get("players", [])
            normalized = []
            for p in players:
                matches = p.get("matches", 0)
                batting = dict(p.get("batting", {}) or {})
                bowling = dict(p.get("bowling", {}) or {})
                batting.setdefault("matches", matches)
                bowling.setdefault("matches", matches)
                normalized.append(
                    {
                        "id": p.get("player_id"),
                        "name": p.get("player_name") or p.get("name"),
                        "team": p.get("team_name") or p.get("team"),
                        "matches": matches,
                        "batting": batting,
                        "bowling": bowling,
                        "fielding": p.get("fielding", {}),
                    }
                )

            return jsonify({"success": True, "data": normalized})
        except Exception as e:
            app.logger.error(f"Error comparing players: {e}", exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500

    @app.route("/api/player/<int:player_id>/partnerships")
    @login_required
    @limiter.limit("30 per minute")
    def api_player_partnerships(player_id):
        """API endpoint for player partnership statistics."""
        try:
            tournament_id = request.args.get("tournament_id", type=int)
            stats_service = StatsService(app.logger)
            partnership_stats = stats_service.get_player_partnership_stats(
                player_id,
                current_user.id,
                tournament_id,
            )

            if "error" in partnership_stats:
                err_text = str(partnership_stats.get("error", "")).lower()
                status = 404 if "not found" in err_text else 400
                return jsonify(partnership_stats), status

            return jsonify({"success": True, "data": partnership_stats})
        except Exception as e:
            app.logger.error(f"Error fetching partnership stats: {e}", exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500

    @app.route("/api/tournament/<int:tournament_id>/partnerships")
    @login_required
    @limiter.limit("30 per minute")
    def api_tournament_partnerships(tournament_id):
        """API endpoint for tournament partnership leaderboard."""
        try:
            limit = request.args.get("limit", 10, type=int)

            if limit < 1 or limit > 50:
                return jsonify({"error": "Limit must be between 1 and 50"}), 400

            stats_service = StatsService(app.logger)
            partnerships = stats_service.get_tournament_partnership_leaderboard(
                current_user.id,
                tournament_id,
                limit,
            )

            return jsonify(
                {
                    "success": True,
                    "data": partnerships,
                    "count": len(partnerships),
                }
            )
        except Exception as e:
            app.logger.error(f"Error fetching tournament partnerships: {e}", exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500

    @app.route("/api/partnerships")
    @login_required
    @limiter.limit("30 per minute")
    def api_overall_partnerships():
        """API endpoint for overall partnership leaderboard."""
        try:
            limit = request.args.get("limit", 10, type=int)
            if limit < 1 or limit > 50:
                return jsonify({"error": "Limit must be between 1 and 50"}), 400

            batsman_1 = aliased(DBPlayer, name="batsman1")
            batsman_2 = aliased(DBPlayer, name="batsman2")

            partnerships = (
                db.session.query(
                    MatchPartnership,
                    batsman_1.name.label("batsman1_name"),
                    batsman_2.name.label("batsman2_name"),
                    Tournament.name.label("tournament_name"),
                )
                .join(DBMatch, MatchPartnership.match_id == DBMatch.id)
                .join(batsman_1, MatchPartnership.batsman1_id == batsman_1.id)
                .join(batsman_2, MatchPartnership.batsman2_id == batsman_2.id)
                .outerjoin(Tournament, DBMatch.tournament_id == Tournament.id)
                .filter(DBMatch.user_id == current_user.id)
                .order_by(MatchPartnership.runs.desc())
                .limit(limit)
                .all()
            )

            app.logger.info(
                f"[Partnerships] Found {len(partnerships)} overall partnership rows (limit={limit}) for user {current_user.id}"
            )

            result = []
            for p, b1_name, b2_name, tourn_name in partnerships:
                result.append(
                    {
                        "batsman1": b1_name,
                        "batsman2": b2_name,
                        "runs": p.runs,
                        "balls": p.balls,
                        "wicket": p.wicket_number,
                        "batsman1_contribution": p.batsman1_contribution,
                        "batsman2_contribution": p.batsman2_contribution,
                        "tournament": tourn_name,
                        "match_id": p.match_id,
                    }
                )

            return jsonify({"success": True, "data": result, "count": len(result)})
        except Exception as e:
            app.logger.error(f"Error fetching overall partnerships: {e}", exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500
