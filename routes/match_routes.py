"""Match and archive route registration."""

import json
import os
import random
import time
import uuid
import zipfile
from datetime import datetime, timedelta

from flask import flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename


def register_match_routes(
    app,
    *,
    limiter,
    db,
    Match,
    Tournament,
    TournamentFixture,
    DBTeam,
    DBMatch,
    MatchScorecard,
    MatchPartnership,
    load_user_teams,
    clean_old_archives,
    PROD_MAX_AGE,
    cleanup_temp_scorecard_images,
    PROJECT_ROOT,
    MATCH_INSTANCES,
    MATCH_INSTANCES_LOCK,
    _get_match_file_lock,
    _load_match_file_for_user,
    load_config,
    increment_matches_simulated,
    rate_limit,
    _handle_tournament_match_completion,
    _persist_non_tournament_match_completion,
    load_match_metadata,
    _is_valid_match_id,
    reverse_player_aggregates,
):
    MATCH_SETUP_FORMATS = {"T20", "ListA"}

    @app.route("/match/setup", methods=["GET", "POST"])
    @login_required
    def match_setup():
        # Check for tournament fixture execution
        fixture_id = request.args.get('fixture_id')
        preselect_home = None
        preselect_away = None
        tournament_id = None
        tournament_format = None  # None means free format selection (non-tournament match)

        if fixture_id:
            fixture = db.session.get(TournamentFixture, fixture_id)
            if fixture and fixture.tournament.user_id == current_user.id:
                # Prevent starting locked or completed matches
                if fixture.status == 'Locked':
                    flash("Cannot start a locked match. Wait for previous rounds to complete.", "error")
                    return redirect(url_for("tournament_dashboard", tournament_id=fixture.tournament.id))

                if fixture.status == 'Completed':
                    flash("This match is already completed.", "info")
                    return redirect(url_for("tournament_dashboard", tournament_id=fixture.tournament.id))

                preselect_home = fixture.home_team_id
                preselect_away = fixture.away_team_id
                tournament_id = fixture.tournament_id
                tournament_format = fixture.tournament.format_type  # Lock format to tournament's setting

        # Load teams using the appropriate format (locked for tournament, T20 default otherwise)
        teams = load_user_teams(current_user.id, match_format=tournament_format or "T20")

        if request.method == "POST":
            clean_old_archives(PROD_MAX_AGE)
            cleanup_temp_scorecard_images()

            data = request.get_json(silent=True) or {}
            if not data:
                return jsonify({"error": "Invalid or missing JSON body"}), 400
            simulation_mode = str(data.get("simulation_mode", "auto")).lower()
            if simulation_mode not in {"auto", "manual"}:
                simulation_mode = "auto"
            data["simulation_mode"] = simulation_mode

            # Validate and normalise match format (T20 default when omitted).
            _raw_fmt = data.get("match_format")
            if _raw_fmt is None or str(_raw_fmt).strip() == "":
                data["match_format"] = "T20"
            else:
                _fmt = str(_raw_fmt).strip()
                if _fmt not in MATCH_SETUP_FORMATS:
                    return jsonify({"error": "Invalid or unsupported match format"}), 400
                data["match_format"] = _fmt

            # Normalise is_day_night to bool
            _dn_raw = data.get("is_day_night", False)
            if isinstance(_dn_raw, str):
                data["is_day_night"] = _dn_raw.strip().lower() in {"true", "1", "yes", "on"}
            else:
                data["is_day_night"] = bool(_dn_raw)

            scenario_options = ("last_ball_six", "win_by_1_run", "super_over_thriller")
            interesting_raw = data.get("make_match_interesting", False)
            if isinstance(interesting_raw, str):
                make_match_interesting = interesting_raw.strip().lower() in {"true", "1", "yes", "on"}
            else:
                make_match_interesting = bool(interesting_raw)

            # If interesting mode is enabled, pick one dramatic scenario at random.
            if make_match_interesting:
                scenario_mode = random.choice(scenario_options)
            else:
                # Backward-compatible path for older clients that still post scenario_mode directly.
                scenario_mode = data.get("scenario_mode")
                if scenario_mode and scenario_mode not in scenario_options:
                    scenario_mode = None
            data["scenario_mode"] = scenario_mode

            # Step 1: Load teams from DB using IDs from frontend
            home_id = data.get("team_home")
            away_id = data.get("team_away")
            # Backward-compatible aliases used by tests/older clients.
            if home_id is None:
                home_id = data.get("team1_id")
            if away_id is None:
                away_id = data.get("team2_id")

            try:
                home_id = int(home_id)
                away_id = int(away_id)
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid team selection"}), 400
            
            # Tournament context
            req_tournament_id = data.get("tournament_id")
            req_fixture_id = data.get("fixture_id")

            # Fix: Use db.session.get() instead of legacy Model.query.get()
            home_db = db.session.get(DBTeam, home_id)
            away_db = db.session.get(DBTeam, away_id)
            
            if not home_db or not away_db:
                return jsonify({"error": "Invalid team selection"}), 400
            if home_db.user_id != current_user.id or away_db.user_id != current_user.id:
                return jsonify({"error": "Unauthorized team selection"}), 403
            if home_db.id == away_db.id:
                return jsonify({"error": "Please select two different teams"}), 400

            if req_fixture_id:
                fixture = db.session.get(TournamentFixture, req_fixture_id)
                if not fixture or fixture.tournament.user_id != current_user.id:
                    return jsonify({"error": "Invalid tournament fixture"}), 403
                if req_tournament_id and fixture.tournament_id != int(req_tournament_id):
                    return jsonify({"error": "Fixture does not match tournament"}), 400
                if fixture.home_team_id != home_id or fixture.away_team_id != away_id:
                    return jsonify({"error": "Fixture teams do not match selection"}), 400
                req_tournament_id = fixture.tournament_id
            elif req_tournament_id:
                tournament = db.session.get(Tournament, int(req_tournament_id))
                if not tournament or tournament.user_id != current_user.id:
                    return jsonify({"error": "Invalid tournament"}), 403

            # Enforce tournament format: override any client-supplied format with the
            # tournament's locked format to prevent inconsistency across fixtures.
            if req_tournament_id:
                _tournament = db.session.get(Tournament, int(req_tournament_id))
                if _tournament and _tournament.format_type:
                    data["match_format"] = _tournament.format_type

            # Fix: Define codes for filename generation later
            home_code = home_db.short_code
            away_code = away_db.short_code

            # Step 2: Construct legacy string identifiers for Match Engine compatibility
            # Engine expects "ShortCode_UserEmail" format to parse ShortCode via split('_')[0]
            data["team_home"] = f"{home_code}_{home_db.user_id}"
            data["team_away"] = f"{away_code}_{away_db.user_id}"

            _fmt = data.get("match_format", "T20")

            home_profile = next((p for p in home_db.profiles if p.format_type == _fmt), None)
            away_profile = next((p for p in away_db.profiles if p.format_type == _fmt), None)

            def _resolve_players_for_format(team_obj, profile_obj):
                # Backward compatibility for legacy teams with players that are not
                # attached to TeamProfile rows (profile_id is NULL in older data/tests).
                if profile_obj:
                    return list(profile_obj.players)
                legacy_players = [p for p in team_obj.players if p.profile_id is None]
                if legacy_players:
                    return legacy_players
                return []

            # Helper to convert DB team to Full Dict (mimicking JSON file structure).
            def team_to_full_dict(t, players):
                d = {
                    "team_name": t.name,
                    "short_code": t.short_code,
                    "players": [],
                }
                for p in players:
                    d["players"].append({
                        "name": p.name,
                        "role": p.role,
                        "batting_rating": p.batting_rating,
                        "bowling_rating": p.bowling_rating,
                        "fielding_rating": p.fielding_rating,
                        "batting_hand": p.batting_hand,
                        "bowling_type": p.bowling_type,
                        "bowling_hand": p.bowling_hand,
                        "is_captain": p.is_captain,
                        "will_bowl": False,
                    })
                return d

            home_players = _resolve_players_for_format(home_db, home_profile)
            away_players = _resolve_players_for_format(away_db, away_profile)
            if len(home_players) < 11 or len(away_players) < 11:
                return jsonify({
                    "error": (
                        f"Selected teams do not have enough players for {_fmt}. "
                        "Each side must have at least 11 players."
                    )
                }), 400

            full_home = team_to_full_dict(home_db, home_players)
            full_away = team_to_full_dict(away_db, away_players)

            # Backward-compatible payload support: if XI data is missing, derive a default XI.
            if not isinstance(data.get("playing_xi"), dict):
                def _default_xi(team_payload):
                    players = list(team_payload.get("players", []))
                    xi = []
                    for idx, player in enumerate(players[:11]):
                        row = {"name": player.get("name", "")}
                        # Mark the first up-to-5 bowlers/all-rounders as active bowlers by default.
                        role = str(player.get("role", "")).strip().lower()
                        row["will_bowl"] = role in {"bowler", "all-rounder"} and idx < 5
                        xi.append(row)
                    return xi

                data["playing_xi"] = {
                    "home": _default_xi(full_home),
                    "away": _default_xi(full_away),
                }

            if not isinstance(data.get("substitutes"), dict):
                def _default_subs(team_payload):
                    players = list(team_payload.get("players", []))
                    return [{"name": p.get("name", "")} for p in players[11:]]

                data["substitutes"] = {
                    "home": _default_subs(full_home),
                    "away": _default_subs(full_away),
                }

            # Step 3: Generic function to enrich player lists (XI and substitutes)
            def enrich_player_list(players_to_enrich, full_team_data):
                enriched = []
                for player_info in players_to_enrich:
                    # Find the full player data from the team dict
                    full_player_data = next((p for p in full_team_data["players"] if p["name"] == player_info["name"]), None)
                    if full_player_data:
                        enriched_player = full_player_data.copy()
                        # If 'will_bowl' was sent from frontend (for playing_xi), add it.
                        print("Logger in app.py for player_info: {}".format(player_info))
                        if 'will_bowl' in player_info:
                            enriched_player["will_bowl"] = player_info.get("will_bowl", False)
                        enriched.append(enriched_player)
                return enriched

            # Enrich both playing XI and substitutes
            data["playing_xi"]["home"] = enrich_player_list(data["playing_xi"]["home"], full_home)
            data["playing_xi"]["away"] = enrich_player_list(data["playing_xi"]["away"], full_away)

            # ── Source-of-truth validation ────────────────────────────────────
            # After enrichment, every player name must be found in the DB profile
            # for the selected format.  If any player was sent from a wrong format
            # squad they will have been silently dropped by enrich_player_list().
            # Reject the request here so a stale/tampered payload never starts a match.
            home_xi_count = len(data["playing_xi"]["home"])
            away_xi_count = len(data["playing_xi"]["away"])
            if home_xi_count != 11:
                return jsonify({
                    "error": (
                        f"Home XI has {home_xi_count}/11 valid players for the {_fmt} format. "
                        "Ensure the correct format squad is selected and all 11 players belong "
                        "to that profile."
                    )
                }), 400
            if away_xi_count != 11:
                return jsonify({
                    "error": (
                        f"Away XI has {away_xi_count}/11 valid players for the {_fmt} format. "
                        "Ensure the correct format squad is selected and all 11 players belong "
                        "to that profile."
                    )
                }), 400

            if "substitutes" in data:
                data["substitutes"]["home"] = enrich_player_list(data["substitutes"]["home"], full_home)
                data["substitutes"]["away"] = enrich_player_list(data["substitutes"]["away"], full_away)
            else:
                data["substitutes"] = {"home": [], "away": []}


            # Step 4: Generate metadata and save file
            match_id = str(uuid.uuid4())
            ts = datetime.now().strftime("%Y%m%d%H%M%S")
            user = current_user.id
            # D1: Use match_id in filename for O(1) lookup
            fname = f"match_{match_id}.json"

            match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
            os.makedirs(match_dir, exist_ok=True)
            path = os.path.join(match_dir, fname)

            from engine.ground_config import get_effective_config as _get_gc
            data.update({
                "match_id": match_id,
                "created_by": user,
                "tournament_id": req_tournament_id,
                "fixture_id": req_fixture_id,
                "created_at": time.time(),
                "timestamp": ts,
                "ground_config": _get_gc(current_user.id),
            })
            # Transient setup flag: do not persist beyond match creation.
            data.pop("make_match_interesting", None)

            with open(path, "w") as f:
                json.dump(data, f, indent=2)

            app.logger.info(f"[MatchSetup] Saved {fname} for {user}")
            return jsonify(match_id=match_id), 200

        from engine.ground_config import get_effective_config as _get_gc_for_setup
        _setup_cfg = _get_gc_for_setup(current_user.id)
        _mode_name = _setup_cfg.get("active_game_mode", "natural_game")
        _modes = _setup_cfg.get("game_modes", {})
        active_mode_label = _modes.get(_mode_name, {}).get("label", "Natural Game")

        return render_template("match_setup.html",
                               teams=teams,
                               preselect_home=preselect_home,
                               preselect_away=preselect_away,
                               tournament_id=tournament_id,
                               fixture_id=fixture_id,
                               tournament_format=tournament_format,
                               active_game_mode_label=active_mode_label)

    @app.route("/api/match/verify-lineups", methods=["POST"])
    @login_required
    def verify_match_lineups():
        """Pre-simulate DB verification: confirm all named XI players exist in
        the team's format-specific squad in the database.  Called by the
        frontend just before POSTing to /match/setup so the user sees a clear
        error before the match starts rather than a cryptic 400 from enrichment.
        """
        data = request.get_json(silent=True) or {}
        _raw_fmt = data.get("match_format")
        if _raw_fmt is None or str(_raw_fmt).strip() == "":
            fmt = "T20"
        else:
            fmt = str(_raw_fmt).strip()
            if fmt not in MATCH_SETUP_FORMATS:
                return jsonify({"error": "Invalid or unsupported match format"}), 400

        home_id = data.get("home_team_id")
        away_id = data.get("away_team_id")
        home_xi_names = data.get("home_xi", [])
        away_xi_names = data.get("away_xi", [])

        home_db = db.session.get(DBTeam, home_id)
        away_db = db.session.get(DBTeam, away_id)

        if not home_db or not away_db:
            return jsonify({"error": "Invalid team IDs"}), 400
        if home_db.user_id != current_user.id or away_db.user_id != current_user.id:
            return jsonify({"error": "Unauthorized"}), 403

        def _check(team_db, xi_names):
            profile = next(
                (p for p in team_db.profiles if p.format_type == fmt), None
            )
            if not profile:
                # Team has no profile for this format — all names are invalid.
                return [], list(xi_names)
            squad_names = {p.name for p in profile.players}
            valid   = [n for n in xi_names if n in squad_names]
            invalid = [n for n in xi_names if n not in squad_names]
            return valid, invalid

        home_valid, home_invalid = _check(home_db, home_xi_names)
        away_valid, away_invalid = _check(away_db, away_xi_names)
        all_valid = not home_invalid and not away_invalid

        return jsonify({
            "valid": all_valid,
            "home": {"valid": home_valid, "invalid": home_invalid},
            "away": {"valid": away_valid, "invalid": away_invalid},
        }), 200

    @app.route("/match/<match_id>")
    @login_required
    def match_detail(match_id):
        match_data, _path, _err = _load_match_file_for_user(match_id)

        if not match_data:
            # JSON cleaned up after archiving — check DB for completed match
            db_match = DBMatch.query.filter_by(id=match_id, user_id=current_user.id).first()
            if db_match:
                return redirect(url_for("view_scoreboard", match_id=match_id))
            return redirect(url_for("home"))

        # increment_matches_simulated()  <-- REMOVED: Caused premature counting on page load/reload
        
        # Check if match is completed
        if match_data.get("current_state") == "completed":
             # Redirect to the dedicated scoreboard view
             return redirect(url_for("view_scoreboard", match_id=match_id))
            
        # Render the detail page, passing the loaded JSON
        return render_template("match_detail.html", match=match_data)
    
    @app.route("/match/<match_id>/scoreboard")
    @login_required
    def view_scoreboard(match_id):
        db_match = DBMatch.query.filter_by(id=match_id, user_id=current_user.id).first()
        if not db_match:
            flash("Match not found", "error")
            return redirect(url_for("home"))

        scorecards = (
            MatchScorecard.query.options(joinedload(MatchScorecard.player_ref))
            .filter_by(match_id=match_id)
            .all()
        )
        if not scorecards:
            flash("Detailed scorecard stats unavailable - showing summary only", "warning")
            # Don't redirect, just continue with empty scorecards


        teams = {
            team.id: team
            for team in DBTeam.query.filter(DBTeam.id.in_([db_match.home_team_id, db_match.away_team_id])).all()
        }

        def format_overs(card):
            if card.balls_bowled:
                return f"{card.balls_bowled // 6}.{card.balls_bowled % 6}"
            if card.overs:
                return f"{card.overs:.1f}"
            return "0.0"

        innings_data = {}
        for card in scorecards:
            entry = innings_data.setdefault(
                card.innings_number,
                {
                    "number": card.innings_number,
                    "batting": [],
                    "bowling": [],
                    "batting_team_id": None,
                    "bowling_team_id": None,
                },
            )
            player_name = card.player_ref.name if card.player_ref else "Unknown"

            if card.record_type == "batting":
                entry["batting_team_id"] = card.team_id
                entry["batting"].append(
                    {
                        "name": player_name,
                        "runs": card.runs,
                        "balls": card.balls,
                        "fours": card.fours,
                        "sixes": card.sixes,
                        "is_out": card.is_out,
                        "wicket_type": card.wicket_type,
                        "wicket_taker_name": card.wicket_taker_name,
                        "fielder_name": card.fielder_name,
                        "strike_rate": card.strike_rate if card.strike_rate else (card.runs * 100.0 / card.balls if card.balls > 0 else 0),
                        "position": card.position or 9999,
                    }
                )
            elif card.record_type == "bowling":
                entry["bowling_team_id"] = card.team_id
                entry["bowling"].append(
                    {
                        "name": player_name,
                        "overs": format_overs(card),
                        "runs_conceded": card.runs_conceded,
                        "wickets": card.wickets,
                        "maidens": card.maidens,
                        "wides": card.wides,
                        "noballs": card.noballs,
                        "economy": (card.runs_conceded / card.overs) if card.overs and card.overs > 0 else 0,
                        "position": card.position or 9999,
                    }
                )

        innings_list = []
        for innings_number in sorted(innings_data.keys()):
            entry = innings_data[innings_number]
            entry["batting"].sort(key=lambda item: item["position"])
            entry["bowling"].sort(key=lambda item: item["position"])
            # Compute extras breakdown from bowling stats
            total_wides = sum(item.get("wides", 0) for item in entry["bowling"])
            total_noballs = sum(item.get("noballs", 0) for item in entry["bowling"])
            batting_runs = sum(item["runs"] for item in entry["batting"])
            # Use authoritative DBMatch score (includes byes/legbyes) instead of
            # re-computing from MatchScorecard rows which have no byes/legbyes columns.
            batting_team_id = entry["batting_team_id"]
            if batting_team_id == db_match.home_team_id:
                entry["score"] = db_match.home_team_score or 0
            else:
                entry["score"] = db_match.away_team_score or 0
            # Derive byes+legbyes as the remainder not accounted for by batting or known extras
            byes_legbyes = max(0, entry["score"] - batting_runs - total_wides - total_noballs)
            total_extras = total_wides + total_noballs + byes_legbyes
            entry["extras"] = {
                "wides": total_wides,
                "noballs": total_noballs,
                "byes_legbyes": byes_legbyes,
                "total": total_extras,
            }
            entry["wickets"] = sum(1 for item in entry["batting"] if item["is_out"])
            entry["batting_team_name"] = teams.get(entry["batting_team_id"]).name if entry["batting_team_id"] in teams else "Unknown"
            entry["bowling_team_name"] = teams.get(entry["bowling_team_id"]).name if entry["bowling_team_id"] in teams else "Unknown"
            innings_list.append(entry)

        match_summary = {
            "result_description": db_match.result_description or "Match Completed",
            "team_home": teams.get(db_match.home_team_id).name if db_match.home_team_id in teams else "Home",
            "team_away": teams.get(db_match.away_team_id).name if db_match.away_team_id in teams else "Away",
            "venue": db_match.venue or "Stadium",
            "tournament_id": db_match.tournament_id,
        }

        return render_template(
            "scorecard_view.html",
            match=match_summary,
            innings_list=innings_list,
        )
    


    @app.route("/match/<match_id>/set-toss", methods=["POST"])
    @login_required
    def set_toss(match_id):
        with _get_match_file_lock(match_id):  # D3: serialize file access per match
            match_data, match_path, err = _load_match_file_for_user(match_id)
            if err:
                return err

            data = request.get_json() or {}
            toss_winner = data.get("winner")
            decision = data.get("decision")
            if not toss_winner or not decision:
                return jsonify({"error": "winner and decision are required"}), 400

            match_data["toss_winner"] = toss_winner
            match_data["toss_decision"] = decision

            with open(match_path, "w") as f:
                json.dump(match_data, f, indent=2)
            app.logger.info(f"[MatchToss] {toss_winner} chose to {decision} (Match: {match_id})")
            return jsonify({"status": "success"}), 200
    
    @app.route("/match/<match_id>/spin-toss", methods=["POST"])
    @login_required
    def spin_toss(match_id):
        with _get_match_file_lock(match_id):  # D3: serialize file access per match
            match_data, match_path, err = _load_match_file_for_user(match_id)
            if err:
                return err

            if not match_data:
                return jsonify({"error": "Match not found"}), 404

            team_home = match_data["team_home"].split('_')[0]
            team_away = match_data["team_away"].split('_')[0]
            toss_choice = match_data["toss"]
            toss_result = random.choice(["Heads", "Tails"])
            
            # Get captain names with fallback logic
            def get_captain_name(team_players, team_short_code):
                """Find captain in playing XI, fallback to first player or team code"""
                # Try to find player with is_captain flag
                captain = next((p for p in team_players if p.get("is_captain")), None)
                if captain:
                    return captain["name"]
                
                # Fallback 1: First player in XI (for old data without is_captain)
                if team_players:
                    app.logger.warning(f"[Toss] No captain found for {team_short_code}, using first player")
                    return team_players[0]["name"]
                
                # Fallback 2: Team short code (shouldn't happen)
                app.logger.error(f"[Toss] No players in XI for {team_short_code}!")
                return team_short_code
            
            home_captain = get_captain_name(match_data["playing_xi"]["home"], team_home)
            away_captain = get_captain_name(match_data["playing_xi"]["away"], team_away)

            toss_winner = team_away if toss_choice == toss_result else team_home
            toss_decision = random.choice(["Bat", "Bowl"])

            match_data["toss_winner"] = toss_winner
            match_data["toss_decision"] = toss_decision

            with open(match_path, "w") as f:
                json.dump(match_data, f, indent=2)

            # Update the in-memory Match instance, if created
            with MATCH_INSTANCES_LOCK:
                if match_id in MATCH_INSTANCES:
                    app.logger.info(f"[ImpactSwap] Found active match instance for {match_id}. Updating state.")
                    match_instance = MATCH_INSTANCES[match_id]
                    match_instance.toss_winner   = toss_winner
                    match_instance.toss_decision = toss_decision
                    match_instance.batting_team  = match_instance.home_xi if toss_decision=="Bat" else match_instance.away_xi
                    match_instance.bowling_team  = match_instance.away_xi if match_instance.batting_team==match_instance.home_xi else match_instance.home_xi

        # Build toss commentary (outside lock — no file/instance access needed)
        full_commentary = f"{home_captain} spins the coin and {away_captain} calls for {toss_choice}.<br>" \
                        f"{toss_winner} won the toss and choose to {toss_decision} first.<br>"

        return jsonify({
            "toss_commentary": full_commentary,
            "toss_winner":     toss_winner,
            "toss_decision":   toss_decision
        })

    @app.route("/match/<match_id>/impact-player-swap", methods=["POST"])
    @login_required
    def impact_player_swap(match_id):
        """Handle impact player substitution with optional swaps for each team."""
        app.logger.info(f"[ImpactSwap] Starting impact player swap for match {match_id}")

        try:
            swap_data = request.get_json()
            if not swap_data:
                return jsonify({"error": "Request body is required"}), 400

            home_swap = swap_data.get("home_swap")
            away_swap = swap_data.get("away_swap")

            with _get_match_file_lock(match_id):  # D3: serialize file access per match
                match_data, match_path, err = _load_match_file_for_user(match_id)
                if err:
                    return err

                match_format = str(match_data.get("match_format", "T20")).strip().upper()
                if match_format != "T20":
                    return jsonify({"error": "Impact player is supported only for T20 format"}), 400

                impact_swaps = {}

                # Perform home team swap
                if home_swap:
                    home_out_idx = home_swap["out_player_index"]
                    home_in_idx = home_swap["in_player_index"]
                    home_out_player = match_data["playing_xi"]["home"][home_out_idx]
                    home_in_player = match_data["substitutes"]["home"][home_in_idx]
                    match_data["playing_xi"]["home"][home_out_idx] = home_in_player
                    match_data["substitutes"]["home"][home_in_idx] = home_out_player
                    impact_swaps["home"] = {"out": home_out_player["name"], "in": home_in_player["name"]}

                # Perform away team swap
                if away_swap:
                    away_out_idx = away_swap["out_player_index"]
                    away_in_idx = away_swap["in_player_index"]
                    away_out_player = match_data["playing_xi"]["away"][away_out_idx]
                    away_in_player = match_data["substitutes"]["away"][away_in_idx]
                    match_data["playing_xi"]["away"][away_out_idx] = away_in_player
                    match_data["substitutes"]["away"][away_in_idx] = away_out_player
                    impact_swaps["away"] = {"out": away_out_player["name"], "in": away_in_player["name"]}

                # Mark that swaps have occurred
                match_data["impact_players_swapped"] = True

                # Update in-memory instance under MATCH_INSTANCES_LOCK
                with MATCH_INSTANCES_LOCK:
                    if match_id in MATCH_INSTANCES:
                        app.logger.info(f"[ImpactSwap] Found active match instance for {match_id}. Updating state.")
                        match_instance = MATCH_INSTANCES[match_id]
                        match_instance.home_xi = match_data["playing_xi"]["home"]
                        match_instance.away_xi = match_data["playing_xi"]["away"]
                        match_instance.data = match_data
                        app.logger.info(f"[ImpactSwap] Instance updated. Home XI now has {len(match_instance.home_xi)} players.")
                    else:
                        app.logger.warning(f"[ImpactSwap] No active match instance found for {match_id}.")

                # Save the updated data back to the JSON file
                with open(match_path, "w", encoding="utf-8") as f:
                    json.dump(match_data, f, indent=2)

            app.logger.info(f"[ImpactSwap] Successfully completed swaps for match {match_id}: {impact_swaps}")

            return jsonify({
                "success": True,
                "match_id": match_id,
                "updated_match_data": match_data,
                "swaps_made": impact_swaps
            }), 200

        except Exception as e:
            app.logger.error(f"[ImpactSwap] Unexpected error for match {match_id}: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500


    @app.route("/match/<match_id>/update-final-lineups", methods=["POST"])
    @login_required
    def update_final_lineups(match_id):
        """
        Update match instance with final reordered lineups and resync stats dictionaries.
        """
        try:
            if match_id not in MATCH_INSTANCES:
                _match_data, _match_path, err = _load_match_file_for_user(match_id)
                if err:
                    return err if err else (jsonify({"error": "Match not found"}), 404)
                app.logger.info(f"[FinalLineups] Match instance {match_id} not yet in memory. No action needed.")
                return jsonify({"success": True, "message": "Lineups will be loaded from updated file."}), 200

            match_instance = MATCH_INSTANCES[match_id]
            if match_instance.data.get("created_by") != current_user.id:
                return jsonify({"error": "Unauthorized"}), 403
            lineup_data = request.get_json() or {}
            home_final_xi = lineup_data.get("home_final_xi")
            away_final_xi = lineup_data.get("away_final_xi")

            # Update the master XI lists in the instance
            if home_final_xi:
                match_instance.home_xi = home_final_xi
                match_instance.data["playing_xi"]["home"] = home_final_xi
                app.logger.info(f"[FinalLineups] Updated HOME XI for match {match_id}")

            if away_final_xi:
                match_instance.away_xi = away_final_xi
                match_instance.data["playing_xi"]["away"] = away_final_xi
                app.logger.info(f"[FinalLineups] Updated AWAY XI for match {match_id}")

            # --- START FIX ---
            # Determine the current batting and bowling teams based on the updated XIs
            team_home_code = match_instance.match_data["team_home"].split("_")[0]
            first_batting_team_was_home = (match_instance.toss_winner == team_home_code and match_instance.toss_decision == "Bat") or \
                                        (match_instance.toss_winner != team_home_code and match_instance.toss_decision == "Bowl")

            current_batting_team_list = None
            if match_instance.innings == 1:
                if first_batting_team_was_home:
                    match_instance.batting_team = match_instance.home_xi
                    match_instance.bowling_team = match_instance.away_xi
                else:
                    match_instance.batting_team = match_instance.away_xi
                    match_instance.bowling_team = match_instance.home_xi
            else:  # Innings 2
                if first_batting_team_was_home:
                    match_instance.batting_team = match_instance.away_xi
                    match_instance.bowling_team = match_instance.home_xi
                else:
                    match_instance.batting_team = match_instance.home_xi
                    match_instance.bowling_team = match_instance.away_xi
            
            # Preserve old stats before rebuilding the dictionary
            old_batsman_stats = getattr(match_instance, 'batsman_stats', {}).copy()
            new_batsman_stats = {}

            # Rebuild the batsman_stats dictionary using the new batting_team
            for player in match_instance.batting_team:
                player_name = player["name"]
                if player_name in old_batsman_stats:
                    # If player already has stats, keep them
                    new_batsman_stats[player_name] = old_batsman_stats[player_name]
                else:
                    # If it's a new player (e.g., impact sub), initialize their stats
                    new_batsman_stats[player_name] = {
                        "runs": 0, "balls": 0, "fours": 0, "sixes": 0,
                        "ones": 0, "twos": 0, "threes": 0, "dots": 0,
                        "wicket_type": "", "bowler_out": "", "fielder_out": ""
                    }
            
            # Overwrite the instance's stats with the newly synced dictionary
            match_instance.batsman_stats = new_batsman_stats
            app.logger.info(f"[FinalLineups] Batsman stats dictionary resynced. Contains {len(new_batsman_stats)} players.")
            
            # Update the current striker and non-striker objects
            if match_instance.wickets < 10 and len(match_instance.batting_team) > 1:
                # Ensure batter_idx is valid for the current team size
                if match_instance.batter_idx[0] < len(match_instance.batting_team) and \
                match_instance.batter_idx[1] < len(match_instance.batting_team):
                    match_instance.current_striker = match_instance.batting_team[match_instance.batter_idx[0]]
                    match_instance.current_non_striker = match_instance.batting_team[match_instance.batter_idx[1]]
            # --- END FIX ---

            app.logger.info(f"[FinalLineups] Confirmed batting order for Innings {match_instance.innings}:")
            for i, player in enumerate(match_instance.batting_team):
                app.logger.info(f"   {i+1}. {player['name']}")

            return jsonify({"success": True}), 200

        except Exception as e:
            app.logger.error(f"Error updating final lineups: {e}", exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500
        
    @app.route("/match/<match_id>/next-ball", methods=["POST"])
    @login_required
    @rate_limit(max_requests=60, window_seconds=10)  # C3: Rate limit to prevent DoS
    def next_ball(match_id):
        try:
            with MATCH_INSTANCES_LOCK:  # Bug Fix B2: Thread-safe match creation
                if match_id not in MATCH_INSTANCES:
                    # Try loading match data from JSON file first (for active/new matches)
                    match_data, _path, err = _load_match_file_for_user(match_id)
                    if match_data:
                        if 'rain_probability' not in match_data:
                            match_data['rain_probability'] = load_config().get('rain_probability', 0.0)
                        MATCH_INSTANCES[match_id] = Match(match_data)
                    else:
                        return err if err else (jsonify({"error": "Match not found"}), 404)
                
                match = MATCH_INSTANCES[match_id]
            if match.data.get("created_by") != current_user.id:
                return jsonify({"error": "Unauthorized"}), 403
            outcome = match.next_ball()

            # Explicitly send final score and wickets clearly
            if outcome.get("match_over"):
                # Only increment if this is the first time we're seeing the match end
                # (Checking if it wasn't already marked completed prevents double counting on repeated API calls)
                first_completion = match.data.get("current_state") != "completed"
                if first_completion:
                    increment_matches_simulated()

                    # Persist completed match data in DB.
                    if match.data.get("tournament_id"):
                        _handle_tournament_match_completion(match, match_id, outcome, app.logger)
                    else:
                        _persist_non_tournament_match_completion(match, match_id, outcome, app.logger)

                return jsonify({
                    "innings_end":     match.innings == 2, # Flag generic innings end
                    "innings_number":  match.innings,
                    "match_over":      True,
                    "commentary":      outcome.get("commentary", "<b>Match Over!</b>"),
                    "scorecard_data":  outcome.get("scorecard_data"),
                    "score":           outcome.get("final_score", match.score),
                    "wickets":         outcome.get("wickets",  match.wickets),
                    "result":          outcome.get("result",  "Match ended")
                })

            return jsonify(outcome)
        except Exception as e:
            # Log the complete error with stack trace to execution.log
            app.logger.error(f"[NextBall] Error processing ball for match {match_id}: {e}", exc_info=True)
            
            # Also log to console for immediate visibility
            import traceback
            traceback.print_exc()
            
            # Return JSON error response instead of HTML 500 page
            return jsonify({
                "error": "An error occurred while processing the ball",
                "details": str(e),
                "match_id": match_id
            }), 500

    @app.route("/match/<match_id>/set-simulation-mode", methods=["POST"])
    @login_required
    def set_simulation_mode(match_id):
        data = request.get_json() or {}
        mode = str(data.get("mode", "auto")).lower()
        if mode not in {"auto", "manual"}:
            return jsonify({"error": "mode must be auto or manual"}), 400

        with _get_match_file_lock(match_id):
            match_data, match_path, err = _load_match_file_for_user(match_id)
            if err:
                return err

            match_data["simulation_mode"] = mode
            with open(match_path, "w", encoding="utf-8") as f:
                json.dump(match_data, f, indent=2)

        with MATCH_INSTANCES_LOCK:
            if match_id in MATCH_INSTANCES:
                match = MATCH_INSTANCES[match_id]
                if match.data.get("created_by") != current_user.id:
                    return jsonify({"error": "Unauthorized"}), 403
                match.simulation_mode = mode
                match.data["simulation_mode"] = mode

        return jsonify({"success": True, "mode": mode}), 200

    @app.route("/match/<match_id>/submit-decision", methods=["POST"])
    @login_required
    def submit_decision(match_id):
        payload = request.get_json() or {}
        selected_index = payload.get("selected_index")
        decision_type = payload.get("type")

        if selected_index is None:
            return jsonify({"error": "selected_index is required"}), 400

        with MATCH_INSTANCES_LOCK:
            if match_id not in MATCH_INSTANCES:
                match_data, _path, err = _load_match_file_for_user(match_id)
                if err:
                    return err
                if 'rain_probability' not in match_data:
                    match_data['rain_probability'] = load_config().get('rain_probability', 0.0)
                MATCH_INSTANCES[match_id] = Match(match_data)
            match = MATCH_INSTANCES[match_id]

        if match.data.get("created_by") != current_user.id:
            return jsonify({"error": "Unauthorized"}), 403

        if not match.pending_decision:
            return jsonify({"error": "No pending decision"}), 400
        if decision_type and decision_type != match.pending_decision.get("type"):
            return jsonify({"error": "Decision type mismatch"}), 400

        result, status_code = match.submit_pending_decision(selected_index)
        return jsonify(result), status_code
    

    @app.route("/match/<match_id>/start-super-over", methods=["POST"])
    @login_required
    def start_super_over(match_id):
        with MATCH_INSTANCES_LOCK:
            if match_id not in MATCH_INSTANCES:
                return jsonify({"error": "Match not found"}), 404
            match = MATCH_INSTANCES[match_id]
            if match.data.get("created_by") != current_user.id:
                return jsonify({"error": "Unauthorized"}), 403

        data = request.get_json(silent=True) or {}
        first_batting_team = data.get("first_batting_team")
        batsmen_names = data.get("batsmen")  # list of 2 names
        bowler_name = data.get("bowler")      # single name

        result = match.start_super_over(first_batting_team, batsmen_names, bowler_name)
        return jsonify(result)

    @app.route("/match/<match_id>/start-super-over-innings2", methods=["POST"])
    @login_required
    def start_super_over_innings2(match_id):
        with MATCH_INSTANCES_LOCK:
            if match_id not in MATCH_INSTANCES:
                return jsonify({"error": "Match not found"}), 404
            match = MATCH_INSTANCES[match_id]
            if match.data.get("created_by") != current_user.id:
                return jsonify({"error": "Unauthorized"}), 403

        data = request.get_json(silent=True) or {}
        batsmen_names = data.get("batsmen")
        bowler_name = data.get("bowler")

        result = match.start_super_over_innings2(batsmen_names, bowler_name)
        return jsonify(result)

    @app.route("/match/<match_id>/next-super-over-ball", methods=["POST"])
    @login_required
    def next_super_over_ball(match_id):
        with MATCH_INSTANCES_LOCK:
            if match_id not in MATCH_INSTANCES:
                return jsonify({"error": "Match not found"}), 404
            match = MATCH_INSTANCES[match_id]
            if match.data.get("created_by") != current_user.id:
                return jsonify({"error": "Unauthorized"}), 403
        try:
            result = match.next_super_over_ball()
            return jsonify(result)
        except Exception as e:
            app.logger.error(f"Error in super over: {e}", exc_info=True)
            return jsonify({"error": "An internal error occurred"}), 500
    
    # Add this endpoint to your app.py

    @app.route("/match/<match_id>/save-commentary", methods=["POST"])
    @login_required
    def save_commentary(match_id):
        """Receive and store the complete frontend commentary for archiving"""
        try:
            print(f"DEBUG: Received commentary request for match {match_id}")
            
            data = request.get_json(silent=True) or {}
            commentary_html = data.get('commentary_html', '')
            
            print(f"DEBUG: Commentary HTML length: {len(commentary_html)}")
            print(f"DEBUG: Contains 'End of over': {'End of over' in commentary_html}")
            print(f"DEBUG: First 300 chars: {commentary_html[:300]}")
            
            if not commentary_html:
                return jsonify({"error": "No commentary provided"}), 400
            
            # Store commentary for the match instance
            if match_id in MATCH_INSTANCES:
                match_instance = MATCH_INSTANCES[match_id]
                if match_instance.data.get("created_by") != current_user.id:
                    return jsonify({"error": "Unauthorized"}), 403
                
                # Store the raw innerHTML so the HTML archive can clone it exactly
                match_instance.frontend_commentary_html = commentary_html

                # Convert HTML to clean text list (used for TXT file generation)
                frontend_commentary = html_to_commentary_list(commentary_html)
                print(f"DEBUG: Converted to {len(frontend_commentary)} commentary items")

                # Replace the backend commentary with frontend commentary
                match_instance.frontend_commentary_captured = frontend_commentary

                app.logger.info(f"[Commentary] Captured {len(frontend_commentary)} items for match {match_id}")
                return jsonify({"message": "Commentary captured successfully"}), 200
            else:
                print(f"DEBUG: Match instance {match_id} not found in MATCH_INSTANCES")
                return jsonify({"error": "Match instance not found"}), 404
                
        except Exception as e:
            print(f"DEBUG: Error in save_commentary: {e}")
            app.logger.error(f"Error saving commentary: {e}", exc_info=True)
            return jsonify({"error": "Failed to save commentary"}), 500

    def html_to_commentary_list(html_content):
        """Convert commentary-log HTML to a list of div strings, preserving token classes."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html_content, 'html.parser')
        commentary_items = []

        for div in soup.find_all('div', class_='code-line'):
            span = div.find('span')
            if not span:
                continue
            text = span.get_text().strip()
            # Skip empty entries and the initial placeholder
            if not text or text in ('// Match simulation ready. Waiting for toss...',
                                    '// Match simulation ready...'):
                continue
            # Store the full div HTML so the archiver can read the token class
            commentary_items.append(str(div))

        return commentary_items


    @app.route("/match/<match_id>/download-archive", methods=["POST"])
    @login_required
    @limiter.limit("10 per minute")
    def download_archive(match_id):
        """
        PRODUCTION VERSION with HTML Integration
        1) Receive HTML content from frontend
        2) Load match metadata and instance
        3) Use MatchArchiver to create complete archive with CSV, JSON, TXT, AND HTML
        4) Return ZIP file to user (also stored under <PROJECT_ROOT>/data/)
        """
        try:
            app.logger.info(f"[DownloadArchive] Starting archive creation for match '{match_id}'")

            # ??? A) Extract HTML content from request ???????????????????????????
            # html_content is accepted for backwards-compatibility but is no longer
            # used to build the archive HTML file. The HTML report is now generated
            # entirely from commentary_log + match metadata on the backend.
            payload = request.get_json() or {}
            html_content = payload.get("html_content")
            if html_content:
                app.logger.debug(f"[DownloadArchive] html_content received ({len(html_content):,} chars) — not used for HTML generation")

            # ??? B) Load match metadata ?????????????????????????????????????????
            match_meta = load_match_metadata(match_id)
            if not match_meta:
                # JSON cleaned up after archiving — try in-memory instance
                with MATCH_INSTANCES_LOCK:
                    inst = MATCH_INSTANCES.get(match_id)
                if inst and inst.data.get("created_by") == current_user.id:
                    match_meta = inst.data
                    app.logger.info(f"[DownloadArchive] Using in-memory match data for '{match_id}'")
                else:
                    app.logger.error(f"[DownloadArchive] Match metadata not found for match_id='{match_id}'")
                    return jsonify({"error": "Match not found"}), 404

            # Verify ownership
            created_by = match_meta.get("created_by")
            if created_by != current_user.id:
                app.logger.warning(f"[DownloadArchive] Unauthorized access: user='{current_user.id}' attempted to archive match='{match_id}'")
                return jsonify({"error": "Unauthorized"}), 403

            # ??? C) Retrieve or rehydrate match instance ?????????????????????????
            match_instance = MATCH_INSTANCES.get(match_id)
            if not match_instance:
                app.logger.info(f"[DownloadArchive] Match instance not in memory; recreating minimal Match for '{match_id}'")
                from engine.match import Match
                match_instance = Match(match_meta)

            # ??? D) Locate original JSON file on disk ???????????????????????????
            from match_archiver import find_original_json_file
            original_json_path = find_original_json_file(match_id)
            _temp_json_created = False
            if not original_json_path:
                # JSON cleaned up after archiving — write match_meta to temp file
                app.logger.info(f"[DownloadArchive] Original JSON cleaned up; writing temp file from match_meta")
                temp_dir = os.path.join(PROJECT_ROOT, "data", "matches")
                os.makedirs(temp_dir, exist_ok=True)
                original_json_path = os.path.join(temp_dir, f"_temp_{match_id}.json")
                with open(original_json_path, "w", encoding="utf-8") as f:
                    json.dump(match_meta, f, indent=2)
                _temp_json_created = True

            app.logger.debug(f"[DownloadArchive] Using JSON at '{original_json_path}'")

            # ??? E) Extract commentary log + raw HTML ????????????????????????????????????
            # Raw HTML: used to clone the commentary box exactly in the HTML archive file
            commentary_raw_html = getattr(match_instance, "frontend_commentary_html", None)
            if commentary_raw_html:
                app.logger.info(f"[DownloadArchive] Raw commentary HTML captured ({len(commentary_raw_html):,} chars)")

            # Text list: used for TXT file generation
            if getattr(match_instance, "frontend_commentary_captured", None):
                commentary_log = match_instance.frontend_commentary_captured
                app.logger.info(f"[DownloadArchive] Using frontend commentary (items={len(commentary_log)})")
            elif getattr(match_instance, "commentary", None):
                commentary_log = match_instance.commentary
                app.logger.info(f"[DownloadArchive] Using backend commentary (items={len(commentary_log)})")
            else:
                commentary_log = ["Match completed - commentary preserved in HTML"]
                app.logger.warning("[DownloadArchive] No commentary found; using fallback single-line log")

            # ??? F) Instantiate MatchArchiver and create ZIP ????????????????????
            from match_archiver import MatchArchiver
            archiver = MatchArchiver(match_meta, match_instance)
            zip_name = f"{archiver.folder_name}.zip"
            app.logger.info(f"[DownloadArchive] Creating archive '{zip_name}' via MatchArchiver")

            try:
                # create_archive() will write ZIP to <PROJECT_ROOT>/data/<zip_name>
                success = archiver.create_archive(
                    original_json_path=original_json_path,
                    commentary_log=commentary_log,
                    commentary_raw_html=commentary_raw_html
                )
                if not success:
                    app.logger.error(f"[DownloadArchive] MatchArchiver reported failure for '{match_id}'")
                    return jsonify({"error": "Failed to create archive"}), 500
            except ValueError as ve:
                app.logger.error(f"[DownloadArchive] Validation error during archiving: {ve}", exc_info=True)
                return jsonify({"error": "Invalid archive data provided"}), 400
            except Exception as arch_err:
                app.logger.error(f"[DownloadArchive] Failed to create archive for match '{match_id}': {arch_err}", exc_info=True)
                return jsonify({"error": "Failed to create archive"}), 500
            finally:
                # Clean up temp JSON if we created one
                if _temp_json_created and os.path.isfile(original_json_path):
                    try:
                        os.remove(original_json_path)
                    except Exception:
                        pass

            # ??? G) Compute and confirm ZIP path on disk ?????????????????????????
            zip_path = os.path.join(PROJECT_ROOT, "data", zip_name)
            if not os.path.isfile(zip_path):
                app.logger.error(f"[DownloadArchive] ZIP file missing after creation: '{zip_path}'")
                return jsonify({"error": "Archive ZIP file not found"}), 500

            zip_size = os.path.getsize(zip_path)
            app.logger.info(f"[DownloadArchive] ZIP successfully created: '{zip_name}' ({zip_size:,} bytes)")

            # ??? H) Stream the ZIP file back to the browser ?????????????????????
            try:
                app.logger.debug(f"[DownloadArchive] Sending ZIP to client: '{zip_path}'")
                return send_file(
                    zip_path,
                    mimetype="application/zip",
                    as_attachment=True,
                    download_name=zip_name
                )
            except Exception as send_err:
                app.logger.error(f"[DownloadArchive] Error sending ZIP file for match '{match_id}': {send_err}", exc_info=True)
                return jsonify({"error": "Failed to send archive file"}), 500

        except Exception as e:
            app.logger.error(f"[DownloadArchive] Unexpected error: {e}", exc_info=True)
            return jsonify({"error": "An unexpected error occurred while creating the archive"}), 500



    @app.route("/my-matches")
    @login_required
    def my_matches():
        """
        Display all ZIP archives in data/files/ matching current_user.id,
        regardless of subfolders. Only show files up to 7 days old.
        """
        def _normalize_match_format(raw_format):
            normalized = (
                str(raw_format or "")
                .strip()
                .lower()
                .replace(" ", "")
                .replace("_", "")
                .replace("-", "")
            )
            format_map = {
                "t20": ("T20", "T20"),
                "lista": ("ListA", "List A"),
                "odi": ("ListA", "List A"),

            }
            return format_map.get(normalized, ("T20", "T20"))

        def _get_archive_format(zip_path):
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    json_members = [name for name in zf.namelist() if name.lower().endswith(".json")]
                    for member in json_members:
                        try:
                            with zf.open(member, "r") as raw_json:
                                payload = json.load(raw_json)
                        except Exception:
                            continue
                        if isinstance(payload, dict):
                            raw_format = payload.get("match_format")
                            if raw_format is not None and str(raw_format).strip():
                                return _normalize_match_format(raw_format)
            except Exception as exc:
                app.logger.debug(f"Could not infer format for archive '{zip_path}': {exc}")
            return ("T20", "T20")

        username    = current_user.id
        files_dir   = os.path.join(PROJECT_ROOT, "data")
        valid_files = []
        match_history = []

        app.logger.info(f"User '{username}' requested /my-matches")

        try:
            if not os.path.isdir(files_dir):
                app.logger.warning(f"'{files_dir}' does not exist for user '{username}'")
            else:
                now = time.time()
                max_age = 7 * 24 * 3600  # 7 days in seconds

                for fn in os.listdir(files_dir):
                    # Only consider ".zip" and filenames containing "_<username>_"
                    if not fn.lower().endswith(".zip"):
                        continue
                    if f"_{username}_" not in fn:
                        continue

                    full_path = os.path.join(files_dir, fn)
                    if not os.path.isfile(full_path):
                        app.logger.debug(f"Skipping '{fn}' (not a regular file)")
                        continue

                    age = now - os.path.getmtime(full_path)
                    if age > max_age:
                        app.logger.info(f"Skipping old archive '{fn}' (age {age//3600}h > 7d)")
                        continue
                    created_at = datetime.fromtimestamp(os.path.getmtime(full_path))
                    expires_at = created_at + timedelta(days=7)

                    # Build URLs for download & delete
                    download_url = f"/archives/{username}/{fn}"
                    delete_url   = f"/archives/{username}/{fn}"
                    format_code, format_label = _get_archive_format(full_path)
                    valid_files.append({
                        "filename":     fn,
                        "download_url": download_url,
                        "delete_url":   delete_url,
                        "created_at":   created_at,
                        "expires_at":   expires_at,
                        "format_code":  format_code,
                        "format_label": format_label,
                    })

                app.logger.info(f"User '{username}' has {len(valid_files)} valid archives")
                valid_files.sort(key=lambda x: x.get("created_at") or datetime.min, reverse=True)

        except Exception as e:
            app.logger.error(f"Error listing archives in '{files_dir}' for '{username}': {e}", exc_info=True)

        try:
            # Fetch ALL matches for the user, joined with Tournament to get names
            all_matches = (
                db.session.query(DBMatch, Tournament.name)
                .outerjoin(Tournament, DBMatch.tournament_id == Tournament.id)
                .filter(DBMatch.user_id == current_user.id)
                .order_by(DBMatch.date.desc())
                .all()
            )

            # Collect team IDs for bulk fetching names
            team_ids = set()
            for m, _ in all_matches:
                if m.home_team_id:
                    team_ids.add(m.home_team_id)
                if m.away_team_id:
                    team_ids.add(m.away_team_id)

            teams_by_id = {}
            if team_ids:
                teams_by_id = {
                    t.id: t for t in DBTeam.query.filter(DBTeam.id.in_(team_ids)).all()
                }

            for m, tour_name in all_matches:
                home_name = teams_by_id.get(m.home_team_id).name if m.home_team_id in teams_by_id else "Home"
                away_name = teams_by_id.get(m.away_team_id).name if m.away_team_id in teams_by_id else "Away"
                format_code, format_label = _normalize_match_format(getattr(m, "match_format", None))
                
                match_history.append({
                    "match_id": m.id,
                    "home_team": home_name,
                    "away_team": away_name,
                    "result_description": m.result_description or "Match Completed",
                    "played_at": m.date,
                    "scoreline": (
                        f"{home_name} {m.home_team_score or 0}/{m.home_team_wickets or 0} "
                        f"({m.home_team_overs or '0.0'}) vs "
                        f"{away_name} {m.away_team_score or 0}/{m.away_team_wickets or 0} "
                        f"({m.away_team_overs or '0.0'})"
                    ),
                    "scoreboard_url": url_for("view_scoreboard", match_id=m.id),
                    "is_tournament": m.tournament_id is not None,
                    "tournament_name": tour_name if tour_name else None,
                    "format_code": format_code,
                    "format_label": format_label,
                })
        except Exception as e:
            app.logger.error(f"Error loading match history for '{username}': {e}", exc_info=True)

        return render_template("my_matches.html", files=valid_files, match_history=match_history)


    @app.route("/archives/<username>/<filename>", methods=["GET"])
    @login_required
    def serve_archive(username, filename):
        """
        Serve a ZIP file stored under PROJECT_ROOT/data/<filename>
        Only the user whose email == username can download it.
        """
        # 1) Authorization check: current_user.id holds the email
        if current_user.id != username:
            app.logger.warning(f"Unauthorized download attempt by '{current_user.id}' for '{username}/{filename}'")
            return jsonify({"error": "Unauthorized"}), 403
        if f"_{current_user.id}_" not in filename:
            app.logger.warning(f"Unauthorized download attempt by '{current_user.id}' for filename '{filename}'")
            return jsonify({"error": "Unauthorized"}), 403

        # 2) Prevent directory-traversal (reject anything with a slash)
        if "/" in filename or "\\" in filename:
            app.logger.warning(f"Invalid filename in download: {filename}")
            return jsonify({"error": "Invalid filename"}), 400

        # 3) Build the absolute path to the ZIP under data/
        zip_path = os.path.join(PROJECT_ROOT, "data", filename)
        if not os.path.isfile(zip_path):
            app.logger.warning(f"Attempt to download non-existent file: {zip_path}")
            return jsonify({"error": "File not found"}), 404

        # 4) Stream the file back
        try:
            app.logger.info(f"Sending archive '{filename}' to user '{username}'")
            return send_file(
                zip_path,
                mimetype="application/zip",
                as_attachment=True,
                download_name=filename
            )
        except Exception as e:
            app.logger.error(f"Error sending archive {zip_path}: {e}", exc_info=True)
            return jsonify({"error": "Failed to send file"}), 500



    @app.route("/matches/delete-multiple", methods=["POST"])
    @login_required
    def delete_multiple_matches():
        """
        Delete multiple non-tournament matches and reverse their stats.
        """
        try:
            data = request.get_json(silent=True) or {}
            match_ids = data.get('match_ids', [])
            app.logger.info(f"Bulk delete requested for matches: {match_ids} by user {current_user.id}")
            
            if not match_ids:
                return jsonify({'error': 'No matches selected'}), 400
                
            deleted_count = 0
            
            for match_id in match_ids:
                try:
                    # Find the match and verify ownership
                    match = DBMatch.query.get(match_id)
                    if not match:
                        app.logger.warning(f"Match {match_id} not found during bulk delete")
                        continue
                        
                    if match.user_id != current_user.id:
                        app.logger.warning(f"Unauthorized delete attempt by {current_user.id} for match {match_id}")
                        continue
                        
                    # Only allow deleting non-tournament matches here to be safe
                    if match.tournament_id is not None:
                        app.logger.warning(f"Attempt to delete tournament match {match_id} via loose match deletion")
                        continue

                    # 1. Get scorecards to reverse stats
                    scorecards = MatchScorecard.query.filter_by(match_id=match_id).all()
                    
                    # 2. Reverse aggregates
                    if scorecards:
                        try:
                            # Import here or rely on global scope? assuming global scope based on view_file earlier
                            reverse_player_aggregates(scorecards, logger=app.logger)
                        except Exception as rev_err:
                            app.logger.error(f"Error reversing stats for match {match_id}: {rev_err}", exc_info=True)
                    
                    # 3. Delete dependent records explicitly
                    MatchPartnership.query.filter_by(match_id=match_id).delete()
                    MatchScorecard.query.filter_by(match_id=match_id).delete()
                    
                    # 4. Remove from in-memory cache if present
                    with MATCH_INSTANCES_LOCK:
                        MATCH_INSTANCES.pop(match_id, None)

                    # 5. Delete the match record
                    db.session.delete(match)
                    deleted_count += 1
                    
                    # 6. Try to delete the JSON file if it exists
                    match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
                    json_path = None
                    if match.match_json_path:
                        # Handle both absolute and relative paths
                        json_path = match.match_json_path
                        if not os.path.isabs(json_path):
                             json_path = os.path.join(match_dir, json_path)
                    
                    # Try explicit path first
                    if json_path and os.path.isfile(json_path):
                        try:
                            os.remove(json_path)
                        except Exception as e:
                            app.logger.warning(f"Failed to delete JSON file {json_path}: {e}")
                    else:
                        # Fallback search if path wasn't stored or file wasn't found
                        if os.path.isdir(match_dir):
                            # Look for files containing the match ID
                            for fn in os.listdir(match_dir):
                                if fn.endswith(".json") and match_id in fn:
                                    try:
                                        full_path = os.path.join(match_dir, fn)
                                        if os.path.isfile(full_path):
                                            os.remove(full_path)
                                    except Exception as e:
                                        app.logger.warning(f"Failed to delete fallback JSON {fn}: {e}")

                except Exception as inner_e:
                    app.logger.error(f"Error deleting individual match {match_id}: {inner_e}", exc_info=True)
                    continue
            
            db.session.commit()
            app.logger.info(f"Bulk delete completed. Deleted {deleted_count} matches.")
            return jsonify({'success': True, 'deleted_count': deleted_count}), 200
            
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error deleting matches: {e}", exc_info=True)
            return jsonify({'error': str(e)}), 500


    @app.route('/archives/<path:archive_name>', methods=['DELETE'])
    @login_required
    def delete_archive(archive_name):
        """
        DELETE endpoint to remove an archive file.
        Expects archive_name to be either 'filename' or 'username/filename'.
        Verified against current_user.id for security.
        """
        # 1. Extract filename and verify ownership
        if '/' in archive_name:
            username_part, filename = archive_name.split('/', 1)
            # Ensure the user is deleting their own file
            if username_part != current_user.id:
                app.logger.warning(f"Unauthorized delete attempt by {current_user.id} for {archive_name}")
                return jsonify({'error': 'Unauthorized'}), 403
        else:
            filename = archive_name
            # If only filename is provided, we must verify it contains the username
            if f"_{current_user.id}_" not in filename:
                app.logger.warning(f"Unauthorized delete attempt by {current_user.id} for {filename}")
                return jsonify({'error': 'Unauthorized'}), 403

        # 2. Normalize filename
        filename = os.path.basename(filename)
        
        # 3. Build the absolute path under ARCHIVES_FOLDER
        archive_folder = app.config.get('ARCHIVES_FOLDER')
        if not archive_folder:
            app.logger.error("ARCHIVES_FOLDER is not configured")
            return jsonify({'error': 'Server misconfiguration'}), 500

        file_path = os.path.join(archive_folder, filename)

        # 4. Check existence
        if not os.path.isfile(file_path):
            app.logger.info(f"Delete requested for non-existent file: {file_path}")
            return jsonify({'error': 'File not found'}), 404

        # 5. Attempt removal
        try:
            os.remove(file_path)
            app.logger.info(f"Deleted archive: {file_path}")
            
            # Also cleanup any related CSV files if they exist in statistics?
            # (Optional, but good for storage)
            
            return jsonify({'message': 'Archive deleted successfully'}), 200

        except PermissionError:
            app.logger.exception(f"Permission denied deleting {file_path}")
            return jsonify({'error': 'Permission denied'}), 403

        except Exception:
            app.logger.exception(f"Unexpected error deleting {file_path}")
            return jsonify({'error': 'Internal server error'}), 500
        


    # Note: Database backup endpoint has been moved to admin routes section
    # See /admin/backup-database route above with admin_required decorator



    # ============================================================================
    @app.route('/match/<match_id>/save-scorecard-images', methods=['POST'])
    @login_required
    @limiter.limit("10 per minute")
    def save_scorecard_images(match_id):
        MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB
        ALLOWED_CONTENT_TYPES = {'image/png', 'image/jpeg', 'image/webp'}

        try:
            from pathlib import Path
            if not _is_valid_match_id(match_id):
                return jsonify({"error": "Invalid match id"}), 400

            _match_data, _match_path, err = _load_match_file_for_user(match_id)
            if err:
                # JSON may be cleaned up after archiving — check in-memory or DB
                authorized = False
                with MATCH_INSTANCES_LOCK:
                    if match_id in MATCH_INSTANCES:
                        authorized = MATCH_INSTANCES[match_id].data.get("created_by") == current_user.id
                if not authorized:
                    db_match = DBMatch.query.filter_by(id=match_id, user_id=current_user.id).first()
                    authorized = db_match is not None
                if not authorized:
                    return err

            # Use absolute path with user isolation
            temp_dir = Path(PROJECT_ROOT) / "data" / "temp_scorecard_images" / secure_filename(current_user.id)
            temp_dir.mkdir(parents=True, exist_ok=True)

            saved_files = []

            for field_name, label in [('first_innings_image', 'first'), ('second_innings_image', 'second')]:
                if field_name in request.files:
                    img = request.files[field_name]
                    if img.filename:
                        # Validate content type
                        if img.content_type not in ALLOWED_CONTENT_TYPES:
                            return jsonify({"error": f"Invalid file type for {label} innings image. Allowed: PNG, JPEG, WebP"}), 400

                        # Validate file size
                        img.seek(0, 2)  # Seek to end
                        size = img.tell()
                        img.seek(0)     # Reset to start
                        if size > MAX_IMAGE_SIZE:
                            return jsonify({"error": f"The {label} innings image exceeds the 5 MB size limit"}), 400

                        safe_match_id = secure_filename(match_id)
                        if safe_match_id != match_id:
                            return jsonify({"error": "Invalid match id"}), 400

                        ext = 'png' if img.content_type == 'image/png' else ('jpg' if img.content_type == 'image/jpeg' else 'webp')
                        img_path = temp_dir / f"{safe_match_id}_{label}_innings_scorecard.{ext}"
                        img.save(img_path)
                        saved_files.append(str(img_path))

            return jsonify({
                "success": True,
                "saved_files": saved_files
            })

        except Exception as e:
            app.logger.error(f"Error saving scorecard images: {e}", exc_info=True)
            return jsonify({"error": "An error occurred while saving images"}), 500
