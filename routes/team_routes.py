"""Team management route registration."""

import json
import re

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from utils.exception_tracker import log_exception

VALID_FORMATS = ("T20", "ListA")
SHORT_CODE_RE = re.compile(r'^[A-Z0-9]{2,5}$')


def register_team_routes(
    app,
    *,
    db,
    Player,
    DBTeam,
    DBPlayer,
    DBTeamProfile,
    DBMasterPlayer=None,
    DBUserPlayer=None,
    MatchScorecard=None,
    TournamentPlayerStatsCache=None,
    MatchPartnership=None,
    DBMatch=None,
    TournamentTeam=None,
    TournamentFixture=None,
    Tournament=None,
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
                log_exception(source="backend")
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

    def _delete_team_player_dependents(team_id):
        """Remove rows that reference team players before bulk player deletion."""
        player_ids = [
            pid for (pid,) in db.session.query(DBPlayer.id)
            .filter_by(team_id=team_id)
            .all()
        ]

        if player_ids and MatchPartnership is not None:
            db.session.query(MatchPartnership).filter(
                or_(
                    MatchPartnership.batsman1_id.in_(player_ids),
                    MatchPartnership.batsman2_id.in_(player_ids),
                )
            ).delete(synchronize_session=False)

        if TournamentPlayerStatsCache is not None:
            cache_filters = [TournamentPlayerStatsCache.team_id == team_id]
            if player_ids:
                cache_filters.append(TournamentPlayerStatsCache.player_id.in_(player_ids))
            db.session.query(TournamentPlayerStatsCache).filter(
                or_(*cache_filters)
            ).delete(synchronize_session=False)

        if MatchScorecard is not None:
            scorecard_filters = [MatchScorecard.team_id == team_id]
            if player_ids:
                scorecard_filters.append(MatchScorecard.player_id.in_(player_ids))
            db.session.query(MatchScorecard).filter(
                or_(*scorecard_filters)
            ).delete(synchronize_session=False)

    def _list_team_tournaments(team_id):
        """Return Tournament rows where this team is referenced — via standings,
        fixtures, or tournament-tagged matches. Used to refuse delete_team and
        show the user "delete these tournaments first" instead of silently
        cascade-destroying tournament structure.
        """
        if Tournament is None:
            return []
        ids = set()
        if TournamentTeam is not None:
            for (tid,) in db.session.query(TournamentTeam.tournament_id).filter_by(team_id=team_id).all():
                if tid is not None:
                    ids.add(tid)
        if TournamentFixture is not None:
            rows = db.session.query(TournamentFixture.tournament_id).filter(
                or_(TournamentFixture.home_team_id == team_id,
                    TournamentFixture.away_team_id == team_id,
                    TournamentFixture.winner_team_id == team_id),
            ).distinct().all()
            for (tid,) in rows:
                if tid is not None:
                    ids.add(tid)
        if DBMatch is not None:
            rows = db.session.query(DBMatch.tournament_id).filter(
                DBMatch.tournament_id.isnot(None),
                or_(DBMatch.home_team_id == team_id, DBMatch.away_team_id == team_id),
            ).distinct().all()
            for (tid,) in rows:
                if tid is not None:
                    ids.add(tid)
        if not ids:
            return []
        return Tournament.query.filter(Tournament.id.in_(ids)).all()

    def _diagnose_team_blockers(team_id):
        """Return a human-readable list of dependent rows still referencing
        this team. Used to give the user a concrete error message when delete
        fails despite the cleanup helpers having run — i.e. some new code
        path or migration introduced an FK we don't yet handle.

        Best-effort: each query is wrapped so a failure just omits that
        bullet rather than masking the original IntegrityError.
        """
        blockers = []

        def _count(label, query):
            try:
                n = query.count()
                if n:
                    blockers.append(f"{n} {label}")
            except Exception:
                pass

        if DBMatch is not None:
            _count("archived match(es)", db.session.query(DBMatch).filter(
                or_(DBMatch.home_team_id == team_id,
                    DBMatch.away_team_id == team_id,
                    DBMatch.winner_team_id == team_id,
                    DBMatch.toss_winner_team_id == team_id)
            ))
        if TournamentFixture is not None:
            _count("tournament fixture(s)", db.session.query(TournamentFixture).filter(
                or_(TournamentFixture.home_team_id == team_id,
                    TournamentFixture.away_team_id == team_id,
                    TournamentFixture.winner_team_id == team_id)
            ))
        if TournamentTeam is not None:
            _count("tournament standings row(s)",
                   db.session.query(TournamentTeam).filter_by(team_id=team_id))
        if MatchScorecard is not None:
            _count("match scorecard(s)",
                   db.session.query(MatchScorecard).filter_by(team_id=team_id))

        return blockers

    def _cleanup_team_external_refs(team_id):
        """Clear FK references to this team from matches, fixtures and standings.

        Without this, deleting a team that has played any match or has been
        registered in any tournament fails with an IntegrityError because the
        FKs from matches/tournament_fixtures/tournament_teams to teams.id have
        no ON DELETE cascade. This helper runs the cleanup at the application
        layer so deletion always succeeds for the owning user.
        """
        if DBMatch is not None:
            # Preserve match history (scores + result_description) by nulling
            # team FKs rather than deleting the match row.
            for col in (DBMatch.home_team_id, DBMatch.away_team_id,
                        DBMatch.winner_team_id, DBMatch.toss_winner_team_id):
                db.session.query(DBMatch).filter(col == team_id).update(
                    {col: None}, synchronize_session=False
                )

        if TournamentFixture is not None:
            # Fixtures involving this team can no longer be played → drop them.
            db.session.query(TournamentFixture).filter(
                or_(TournamentFixture.home_team_id == team_id,
                    TournamentFixture.away_team_id == team_id)
            ).delete(synchronize_session=False)
            # Fixtures already played and won by this team but not deleted above
            # (defensive — should be empty after the previous step).
            db.session.query(TournamentFixture).filter(
                TournamentFixture.winner_team_id == team_id
            ).update({TournamentFixture.winner_team_id: None}, synchronize_session=False)

        if TournamentTeam is not None:
            db.session.query(TournamentTeam).filter_by(team_id=team_id).delete(
                synchronize_session=False
            )

    def _validate_profile(fmt, profile_data, is_draft):
        """
        Validate a single profile dict {captain, wicketkeeper, players: [...]}.
        Also enforces: WK players have bowling fields cleared, duplicate names rejected.
        Returns error string or None.
        """
        players = profile_data.get("players", [])
        captain = (profile_data.get("captain") or "").strip()
        wk_name = (profile_data.get("wicketkeeper") or "").strip()

        # Strip bowling fields for Wicketkeeper-role players (server-side enforcement)
        for p in players:
            if p.get("role") == "Wicketkeeper":
                p["bowling_type"] = ""
                p["bowling_hand"] = ""

        # Duplicate player name check (case-insensitive)
        seen_names = {}
        for p in players:
            lower_name = p["name"].strip().lower()
            if not lower_name:
                continue
            if lower_name in seen_names:
                return f"{fmt} profile: duplicate player name '{p['name']}'."
            seen_names[lower_name] = True

        if is_draft:
            if len(players) < 1:
                return f"{fmt} profile: draft must have at least 1 player."
            if len(players) > 25:
                return f"{fmt} profile: maximum 25 players (have {len(players)})."
            return None

        if not (12 <= len(players) <= 25):
            return f"{fmt} profile: You must enter between 12 and 25 players."

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

    def _parse_legacy_profile_payload(form):
        """
        Backward-compatible parser for older form payloads that post flat
        player arrays (player_name/player_role/etc.) instead of profiles_payload.
        Returns (profiles_dict, error_str|None).
        """
        names = [str(v).strip() for v in form.getlist("player_name")]
        if not any(names):
            return None, "No profile data submitted."

        field_lists = {
            "player_role": [str(v).strip() for v in form.getlist("player_role")],
            "batting_rating": form.getlist("batting_rating"),
            "bowling_rating": form.getlist("bowling_rating"),
            "fielding_rating": form.getlist("fielding_rating"),
            "batting_hand": [str(v).strip() for v in form.getlist("batting_hand")],
            "bowling_type": [str(v).strip() for v in form.getlist("bowling_type")],
            "bowling_hand": [str(v).strip() for v in form.getlist("bowling_hand")],
        }

        count = len(names)
        for field_name, values in field_lists.items():
            if len(values) != count:
                return None, (
                    f"Invalid team payload: '{field_name}' has {len(values)} items, "
                    f"expected {count}."
                )

        raw_players = []
        for idx in range(count):
            raw_players.append({
                "name": names[idx],
                "role": field_lists["player_role"][idx],
                "batting_rating": field_lists["batting_rating"][idx],
                "bowling_rating": field_lists["bowling_rating"][idx],
                "fielding_rating": field_lists["fielding_rating"][idx],
                "batting_hand": field_lists["batting_hand"][idx],
                "bowling_type": field_lists["bowling_type"][idx],
                "bowling_hand": field_lists["bowling_hand"][idx],
            })

        players, err = _extract_player_list(raw_players)
        if err:
            return None, f"T20 profile — {err}"

        return {
            "T20": {
                "captain": (form.get("captain") or "").strip(),
                "wicketkeeper": (form.get("wicketkeeper") or "").strip(),
                "players": players,
            }
        }, None

    def _parse_profiles_payload(form):
        """
        Parse the 'profiles_payload' hidden field.
        Returns (profiles_dict, error_str|None).
        profiles_dict maps format_type → {captain, wicketkeeper, players: [...]}.
        """
        raw = (form.get("profiles_payload") or "").strip()
        if not raw:
            return _parse_legacy_profile_payload(form)
        try:
            payload = json.loads(raw)
        except Exception:
            log_exception(source="backend")
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

    def _detach_player_from_squad(player):
        """Remove a player from active squad selection without deleting history."""
        player.profile_id = None
        player.is_captain = False
        player.is_wicketkeeper = False

    def _sync_profile_squad(team_id, profile, pdata):
        """
        Idempotently sync one profile's active squad while preserving Player IDs.

        Strategy:
        - Reuse in-squad rows by name (profile_id + name).
        - Reattach legacy rows (same team_id + name + profile_id NULL) when present.
        - Create new rows only when no reusable identity exists.
        - Detach rows removed from squad instead of deleting.
        """
        captain = (pdata.get("captain") or "").strip()
        wk_name = (pdata.get("wicketkeeper") or "").strip()
        incoming = pdata.get("players", []) or []

        # Existing active rows in this profile.
        existing_active = {
            (p.name or "").strip().lower(): p
            for p in DBPlayer.query.filter_by(profile_id=profile.id).all()
        }

        incoming_names = set()
        for p in incoming:
            name = (p.get("name") or "").strip()
            key = name.lower()
            if not key:
                continue
            incoming_names.add(key)

            # Prefer current in-profile row; otherwise try legacy detached row.
            player = existing_active.get(key)
            if player is None:
                player = (
                    DBPlayer.query.filter_by(team_id=team_id, profile_id=None, name=name)
                    .order_by(DBPlayer.id.asc())
                    .first()
                )
                if player is not None:
                    player.profile_id = profile.id

            if player is None:
                player = DBPlayer(team_id=team_id, profile_id=profile.id, name=name)
                db.session.add(player)

            player.role = p.get("role")
            player.batting_rating = p.get("batting_rating")
            player.bowling_rating = p.get("bowling_rating")
            player.fielding_rating = p.get("fielding_rating")
            player.batting_hand = p.get("batting_hand")
            player.bowling_type = p.get("bowling_type")
            player.bowling_hand = p.get("bowling_hand")
            player.is_captain = (name == captain) if captain else False
            player.is_wicketkeeper = (name == wk_name) if wk_name else False

        # Players removed from squad are detached (never deleted).
        for key, player in existing_active.items():
            if key not in incoming_names:
                _detach_player_from_squad(player)

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

                if not (name and short_code and home_ground and pitch):
                    return render_template(
                        "team_create.html",
                        error="All team fields are required.",
                    )

                if not SHORT_CODE_RE.match(short_code):
                    return render_template(
                        "team_create.html",
                        error="Short code must be 2-5 uppercase alphanumeric characters.",
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

                color = request.form.get("team_color", "#4f46e5")

                # New single-page flow: if profiles_payload is present, atomically
                # create team + profiles + players. Legacy flat player arrays are
                # still accepted below for older forms/tests.
                raw_payload = (request.form.get("profiles_payload") or "").strip()
                has_legacy_players = any(
                    str(v).strip() for v in request.form.getlist("player_name")
                )
                if raw_payload or has_legacy_players:
                    profiles, parse_err = _parse_profiles_payload(request.form)
                    if parse_err:
                        return render_template("team_create.html", error=parse_err)

                    non_empty = {
                        fmt: pdata
                        for fmt, pdata in profiles.items()
                        if pdata.get("players")
                    }
                    if not non_empty:
                        return render_template(
                            "team_create.html",
                            error="Add at least one player to a profile before saving.",
                        )

                    is_draft = (request.form.get("action") == "save_draft") and not raw_payload

                    # Strict validation on every non-empty profile unless this is
                    # the legacy draft-save action.
                    for fmt, pdata in non_empty.items():
                        err = _validate_profile(fmt, pdata, is_draft=is_draft)
                        if err:
                            return render_template("team_create.html", error=err)

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

                    save_err = _save_profiles(new_team.id, non_empty, is_draft=is_draft)
                    if save_err:
                        db.session.rollback()
                        return render_template("team_create.html", error=save_err)

                    db.session.commit()

                    app.logger.info(
                        f"Team '{new_team.name}' (ID: {new_team.id}) created as "
                        f"{'Draft' if is_draft else 'published'} by {current_user.id}"
                    )
                    if is_draft:
                        flash("Team saved as draft.", "success")
                    else:
                        flash(f"Team '{new_team.name}' saved. Ready to play!", "success")
                    return redirect(url_for("manage_teams"))

                # Legacy flow: identity only → create draft team + default T20 profile.
                new_team = DBTeam(
                    user_id=current_user.id,
                    name=name,
                    short_code=short_code,
                    home_ground=home_ground,
                    pitch_preference=pitch,
                    team_color=color,
                    is_draft=True,
                )
                db.session.add(new_team)
                db.session.flush()

                profile = DBTeamProfile(team_id=new_team.id, format_type="T20")
                db.session.add(profile)
                db.session.commit()

                app.logger.info(
                    f"Team '{new_team.name}' (ID: {new_team.id}) created as "
                    f"Draft by {current_user.id}"
                )
                flash("Team created as draft. Now build your squad.", "success")
                return redirect(url_for("team_squad", team_id=new_team.id, fmt="T20"))

            except Exception as e:
                log_exception(e)
                db.session.rollback()
                app.logger.error(
                    f"Error creating team: {e}", exc_info=True
                )
                return render_template(
                    "team_create.html",
                    error="An unexpected error occurred. Please try again.",
                )

        return render_template("team_create.html")

    # ── Squad Builder ────────────────────────────────────────────────────────

    def _get_effective_pool(user_id):
        if not DBMasterPlayer or not DBUserPlayer:
            return []
        masters = DBMasterPlayer.query.order_by(DBMasterPlayer.name).all()
        overrides = {}
        customs = []
        for up in DBUserPlayer.query.filter_by(user_id=user_id).all():
            if up.master_player_id is not None:
                overrides[up.master_player_id] = up
            else:
                customs.append(up)
        pool = []
        for mp in masters:
            if mp.id in overrides:
                up = overrides[mp.id]
                pool.append(_pool_dict(up, "override", mp.id, up.id))
            else:
                pool.append(_pool_dict(mp, "master", mp.id, None))
        for cp in customs:
            pool.append(_pool_dict(cp, "custom", None, cp.id))
        return pool

    def _pool_dict(obj, source, master_id, user_player_id):
        return {
            "id": f"{source}_{obj.id}",
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
        }

    @app.route("/team/<int:team_id>/squad")
    @app.route("/team/<int:team_id>/squad/<fmt>")
    @login_required
    def team_squad(team_id, fmt="T20"):
        team = DBTeam.query.get_or_404(team_id)
        if team.user_id != current_user.id:
            flash("Access denied.", "danger")
            return redirect(url_for("manage_teams"))
        if fmt not in VALID_FORMATS:
            fmt = "T20"
        profile = DBTeamProfile.query.filter_by(team_id=team_id, format_type=fmt).first()
        if not profile:
            profile = DBTeamProfile(team_id=team_id, format_type=fmt)
            db.session.add(profile)
            db.session.commit()
        squad = DBPlayer.query.filter_by(profile_id=profile.id).order_by(DBPlayer.name).all()
        squad_json = json.dumps([{
            "pool_id": None, "source": None,
            "name": p.name, "role": p.role,
            "batting_rating": p.batting_rating, "bowling_rating": p.bowling_rating,
            "fielding_rating": p.fielding_rating,
            "batting_hand": p.batting_hand or "", "bowling_type": p.bowling_type or "",
            "bowling_hand": p.bowling_hand or "",
            "is_captain": p.is_captain, "is_wicketkeeper": p.is_wicketkeeper,
        } for p in squad])
        return render_template(
            "team_squad.html",
            team=team,
            fmt=fmt,
            profile=profile,
            squad=squad,
            squad_json=squad_json,
            formats=VALID_FORMATS,
        )

    @app.route("/api/team/<int:team_id>/squad/<fmt>/add", methods=["POST"])
    @login_required
    def team_squad_add(team_id, fmt):
        team = DBTeam.query.get_or_404(team_id)
        if team.user_id != current_user.id:
            return json.dumps({"error": "Forbidden"}), 403, {"Content-Type": "application/json"}
        profile = DBTeamProfile.query.filter_by(team_id=team_id, format_type=fmt).first()
        if not profile:
            return json.dumps({"error": "Profile not found."}), 404, {"Content-Type": "application/json"}
        data = request.get_json(silent=True) or {}
        player_id = data.get("player_id", "")
        pool = _get_effective_pool(current_user.id)
        entry = None
        for p in pool:
            if p["id"] == player_id:
                entry = p
                break
        if not entry:
            return json.dumps({"error": "Player not found in pool."}), 404, {"Content-Type": "application/json"}
        existing = DBPlayer.query.filter_by(profile_id=profile.id, name=entry["name"]).first()
        if existing:
            return json.dumps({"error": f"'{entry['name']}' is already in the squad."}), 400, {"Content-Type": "application/json"}
        count = DBPlayer.query.filter_by(profile_id=profile.id).count()
        if count >= 25:
            return json.dumps({"error": "Squad is full (max 25 players)."}), 400, {"Content-Type": "application/json"}
        player = (
            DBPlayer.query.filter_by(team_id=team_id, profile_id=None, name=entry["name"])
            .order_by(DBPlayer.id.asc())
            .first()
        )
        if player is None:
            player = DBPlayer(team_id=team_id, name=entry["name"])
            db.session.add(player)
        player.profile_id = profile.id
        player.role = entry["role"]
        player.batting_rating = entry["batting_rating"]
        player.bowling_rating = entry["bowling_rating"]
        player.fielding_rating = entry["fielding_rating"]
        player.batting_hand = entry["batting_hand"]
        player.bowling_type = entry["bowling_type"]
        player.bowling_hand = entry["bowling_hand"]
        db.session.commit()
        return json.dumps({
            "ok": True,
            "player": {
                "id": player.id, "name": player.name, "role": player.role,
                "batting_rating": player.batting_rating,
                "bowling_rating": player.bowling_rating,
                "fielding_rating": player.fielding_rating,
                "batting_hand": player.batting_hand or "",
                "bowling_type": player.bowling_type or "",
                "bowling_hand": player.bowling_hand or "",
            },
        }), 200, {"Content-Type": "application/json"}

    @app.route("/api/team/<int:team_id>/squad/<fmt>/remove", methods=["POST"])
    @login_required
    def team_squad_remove(team_id, fmt):
        team = DBTeam.query.get_or_404(team_id)
        if team.user_id != current_user.id:
            return json.dumps({"error": "Forbidden"}), 403, {"Content-Type": "application/json"}
        data = request.get_json(silent=True) or {}
        player_id = data.get("player_id")
        player = DBPlayer.query.get(player_id)
        if not player or player.team_id != team_id:
            return json.dumps({"error": "Player not found."}), 404, {"Content-Type": "application/json"}
        name = player.name
        _detach_player_from_squad(player)
        db.session.commit()
        return json.dumps({"ok": True, "name": name}), 200, {"Content-Type": "application/json"}

    @app.route("/api/team/<int:team_id>/squad/<fmt>/captain", methods=["POST"])
    @login_required
    def team_squad_captain(team_id, fmt):
        team = DBTeam.query.get_or_404(team_id)
        if team.user_id != current_user.id:
            return json.dumps({"error": "Forbidden"}), 403, {"Content-Type": "application/json"}
        profile = DBTeamProfile.query.filter_by(team_id=team_id, format_type=fmt).first()
        if not profile:
            return json.dumps({"error": "Profile not found."}), 404, {"Content-Type": "application/json"}
        data = request.get_json(silent=True) or {}
        player_id = data.get("player_id")
        for p in DBPlayer.query.filter_by(profile_id=profile.id).all():
            p.is_captain = (p.id == player_id)
        db.session.commit()
        return json.dumps({"ok": True}), 200, {"Content-Type": "application/json"}

    @app.route("/api/team/<int:team_id>/squad/<fmt>/wicketkeeper", methods=["POST"])
    @login_required
    def team_squad_wicketkeeper(team_id, fmt):
        team = DBTeam.query.get_or_404(team_id)
        if team.user_id != current_user.id:
            return json.dumps({"error": "Forbidden"}), 403, {"Content-Type": "application/json"}
        profile = DBTeamProfile.query.filter_by(team_id=team_id, format_type=fmt).first()
        if not profile:
            return json.dumps({"error": "Profile not found."}), 404, {"Content-Type": "application/json"}
        data = request.get_json(silent=True) or {}
        player_id = data.get("player_id")
        player = DBPlayer.query.get(player_id)
        if not player or player.profile_id != profile.id:
            return json.dumps({"error": "Player not in squad."}), 400, {"Content-Type": "application/json"}
        if player.role != "Wicketkeeper":
            return json.dumps({"error": "Only Wicketkeeper-role players can be designated."}), 400, {"Content-Type": "application/json"}
        for p in DBPlayer.query.filter_by(profile_id=profile.id).all():
            p.is_wicketkeeper = (p.id == player_id)
        db.session.commit()
        return json.dumps({"ok": True}), 200, {"Content-Type": "application/json"}

    @app.route("/api/team/<int:team_id>/squad/<fmt>/publish", methods=["POST"])
    @login_required
    def team_squad_publish(team_id, fmt):
        team = DBTeam.query.get_or_404(team_id)
        if team.user_id != current_user.id:
            return json.dumps({"error": "Forbidden"}), 403, {"Content-Type": "application/json"}
        profile = DBTeamProfile.query.filter_by(team_id=team_id, format_type=fmt).first()
        if not profile:
            return json.dumps({"error": "Profile not found."}), 404, {"Content-Type": "application/json"}
        players = DBPlayer.query.filter_by(profile_id=profile.id).all()
        count = len(players)
        if not (11 <= count <= 25):
            return json.dumps({"error": f"Need 11-25 players, have {count}."}), 400, {"Content-Type": "application/json"}
        wk_count = sum(1 for p in players if p.role == "Wicketkeeper")
        if wk_count < 1:
            return json.dumps({"error": "Need at least 1 Wicketkeeper."}), 400, {"Content-Type": "application/json"}
        bowl_count = sum(1 for p in players if p.role in ("Bowler", "All-rounder"))
        if bowl_count < 5:
            return json.dumps({"error": "Need at least 5 Bowlers/All-rounders."}), 400, {"Content-Type": "application/json"}
        captain = [p for p in players if p.is_captain]
        if not captain:
            return json.dumps({"error": "Select a captain."}), 400, {"Content-Type": "application/json"}
        wk_designated = [p for p in players if p.is_wicketkeeper]
        if not wk_designated:
            return json.dumps({"error": "Designate a wicketkeeper."}), 400, {"Content-Type": "application/json"}
        team.is_draft = False
        db.session.commit()
        return json.dumps({"ok": True}), 200, {"Content-Type": "application/json"}

    @app.route("/api/team/<int:team_id>/squad/<fmt>/bulk-publish", methods=["POST"])
    @login_required
    def team_squad_bulk_publish(team_id, fmt):
        """
        Bulk-create squad from localStorage draft and optionally publish.
        Accepts: {players: [{pool_id, source, name, role, ...}], captain_name, wk_name, is_draft}
        Players with a pool_id are looked up for authoritative data.
        Players without pool_id use the provided data directly (legacy squad editing).
        """
        team = DBTeam.query.get_or_404(team_id)
        if team.user_id != current_user.id:
            return json.dumps({"error": "Forbidden"}), 403, {"Content-Type": "application/json"}
        if fmt not in VALID_FORMATS:
            return json.dumps({"error": "Invalid format."}), 400, {"Content-Type": "application/json"}

        data = request.get_json(silent=True) or {}
        raw_players = data.get("players", [])
        captain_name = (data.get("captain_name") or "").strip()
        wk_name = (data.get("wk_name") or "").strip()
        is_draft = bool(data.get("is_draft", False))

        if not isinstance(raw_players, list) or len(raw_players) == 0:
            return json.dumps({"error": "No players provided."}), 400, {"Content-Type": "application/json"}

        # Resolve each player: pool lookup if pool_id present, else use raw data
        pool_cache = None
        resolved = []
        seen_names = set()
        for item in raw_players:
            pool_id = item.get("pool_id")
            source = item.get("source")
            if pool_id is not None:
                # Look up authoritative data from effective pool
                if pool_cache is None:
                    pool_cache, _ = _get_effective_pool(current_user.id)
                entry = next((p for p in pool_cache if p["id"] == pool_id and p["source"] == source), None)
                if not entry:
                    # Fallback: match by id only
                    entry = next((p for p in pool_cache if p["id"] == pool_id), None)
                if not entry:
                    return json.dumps({"error": f"Player with pool_id {pool_id} not found in your pool."}), 400, {"Content-Type": "application/json"}
                p = {k: entry[k] for k in ("name", "role", "batting_rating", "bowling_rating",
                                            "fielding_rating", "batting_hand", "bowling_type", "bowling_hand")}
            else:
                # Legacy path: use provided data directly
                p = {
                    "name": str(item.get("name", "")).strip(),
                    "role": str(item.get("role", "")).strip(),
                    "batting_rating": int(item.get("batting_rating") or 50),
                    "bowling_rating": int(item.get("bowling_rating") or 50),
                    "fielding_rating": int(item.get("fielding_rating") or 50),
                    "batting_hand": str(item.get("batting_hand") or ""),
                    "bowling_type": str(item.get("bowling_type") or ""),
                    "bowling_hand": str(item.get("bowling_hand") or ""),
                }
            if not p["name"]:
                return json.dumps({"error": "Player with empty name."}), 400, {"Content-Type": "application/json"}
            lower = p["name"].lower()
            if lower in seen_names:
                return json.dumps({"error": f"Duplicate player: '{p['name']}'."}), 400, {"Content-Type": "application/json"}
            seen_names.add(lower)
            # Clear bowling fields for Wicketkeeper-role players
            if p["role"] == "Wicketkeeper":
                p["bowling_type"] = ""
                p["bowling_hand"] = ""
            resolved.append(p)

        n = len(resolved)
        if is_draft:
            if n > 25:
                return json.dumps({"error": f"Maximum 25 players, have {n}."}), 400, {"Content-Type": "application/json"}
        if not is_draft:
            if not (11 <= n <= 25):
                return json.dumps({"error": f"Need 11-25 players, have {n}."}), 400, {"Content-Type": "application/json"}
            wk_count = sum(1 for p in resolved if p["role"] == "Wicketkeeper")
            if wk_count < 1:
                return json.dumps({"error": "Need at least 1 Wicketkeeper."}), 400, {"Content-Type": "application/json"}
            bowl_count = sum(1 for p in resolved if p["role"] in ("Bowler", "All-rounder"))
            if bowl_count < 5:
                return json.dumps({"error": "Need at least 5 Bowlers/All-rounders."}), 400, {"Content-Type": "application/json"}
            if not captain_name:
                return json.dumps({"error": "Select a captain."}), 400, {"Content-Type": "application/json"}
            if not wk_name:
                return json.dumps({"error": "Designate a wicketkeeper."}), 400, {"Content-Type": "application/json"}

        profile = DBTeamProfile.query.filter_by(team_id=team_id, format_type=fmt).first()
        if not profile:
            profile = DBTeamProfile(team_id=team_id, format_type=fmt)
            db.session.add(profile)
            db.session.flush()

        # Replace active squad identity-safely (preserve Player rows with history).
        _sync_profile_squad(
            team_id,
            profile,
            {
                "captain": captain_name,
                "wicketkeeper": wk_name,
                "players": resolved,
            },
        )

        if not is_draft:
            team.is_draft = False

        db.session.commit()
        return json.dumps({"ok": True}), 200, {"Content-Type": "application/json"}

    @app.route("/api/team/<int:team_id>/pool/search")
    @login_required
    def team_pool_search(team_id):
        team = DBTeam.query.get_or_404(team_id)
        if team.user_id != current_user.id:
            return json.dumps({"error": "Forbidden"}), 403, {"Content-Type": "application/json"}
        fmt = request.args.get("fmt", "T20")
        q = request.args.get("q", "").strip().lower()
        role = request.args.get("role", "").strip()
        batting_hand = request.args.get("batting_hand", "").strip()
        bowling_type = request.args.get("bowling_type", "").strip()
        bowling_hand = request.args.get("bowling_hand", "").strip()

        profile = DBTeamProfile.query.filter_by(team_id=team_id, format_type=fmt).first()
        squad_names = set()
        if profile:
            squad_names = {p.name.lower() for p in DBPlayer.query.filter_by(profile_id=profile.id).all()}

        pool = _get_effective_pool(current_user.id)
        results = []
        for p in pool:
            if p["name"].lower() in squad_names:
                continue
            if q and q not in p["name"].lower():
                continue
            if role and p["role"] != role:
                continue
            if batting_hand and p["batting_hand"] != batting_hand:
                continue
            if bowling_type and p["bowling_type"] != bowling_type:
                continue
            if bowling_hand and p["bowling_hand"] != bowling_hand:
                continue
            results.append(p)
        return json.dumps(results), 200, {"Content-Type": "application/json"}

    def _user_oversize_profiles(user_id):
        """
        Return a list of dicts {team_id, team_name, short_code, format_type, player_count}
        for every TeamProfile owned by `user_id` that has more than 25 players.
        """
        rows = (
            db.session.query(
                DBTeam.id, DBTeam.name, DBTeam.short_code,
                DBTeamProfile.format_type, func.count(DBPlayer.id),
            )
            .join(DBTeamProfile, DBTeamProfile.team_id == DBTeam.id)
            .join(DBPlayer, DBPlayer.profile_id == DBTeamProfile.id)
            .filter(DBTeam.user_id == user_id)
            .group_by(DBTeam.id, DBTeam.name, DBTeam.short_code, DBTeamProfile.format_type)
            .having(func.count(DBPlayer.id) > 25)
            .all()
        )
        return [
            {
                "team_id": tid, "team_name": tname, "short_code": scode,
                "format_type": fmt, "player_count": count,
            }
            for (tid, tname, scode, fmt, count) in rows
        ]

    @app.route("/account/squad-cleanup")
    @login_required
    def squad_cleanup():
        """Remediation page: lists every profile owned by the current user that
        has more than 25 players, with links to trim each squad."""
        offending = _user_oversize_profiles(current_user.id)
        return render_template(
            "squad_cleanup.html",
            offending=offending,
        )

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
                    "created_at": t.created_at,
                })
        except Exception as e:
            log_exception(e)
            app.logger.error(f"Error loading teams from DB: {e}", exc_info=True)

        total_players = sum(pi["player_count"] for t in teams for pi in t["profiles"])
        total_profiles = sum(len(t["profiles"]) for t in teams)
        avg_squad_size = total_players // total_profiles if total_profiles else 0

        return render_template("manage_teams.html", teams=teams, avg_squad_size=avg_squad_size, total_players=total_players)

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

            # Refuse delete if the team is part of any tournament. The cascade
            # rules added in 2.4.3 would silently delete fixtures and standings,
            # which would damage the tournament's structure for any opposing
            # team still in it. Force the user to delete the tournament first
            # (which cleans up its fixtures + standings + matches as a unit).
            blocking_tournaments = _list_team_tournaments(team.id)
            if blocking_tournaments:
                names = ", ".join(f"'{t.name}'" for t in blocking_tournaments[:3])
                more = f" and {len(blocking_tournaments) - 3} more" if len(blocking_tournaments) > 3 else ""
                flash(
                    f"Cannot delete '{team.name}' — it is part of tournament(s): "
                    f"{names}{more}. Please delete those tournaments first, "
                    "then delete the team.",
                    "danger",
                )
                return redirect(url_for("manage_teams"))

            team_name = team.name
            # Defensive cleanup: remove all players/profiles for this team explicitly.
            # This covers legacy rows that may not be profile-linked.
            _delete_team_player_dependents(team.id)
            # Clear external FK references (matches, fixtures, standings) — the FKs
            # to teams.id have no ON DELETE cascade so deletion would otherwise
            # fail with an IntegrityError for any team that has been used.
            _cleanup_team_external_refs(team.id)
            db.session.query(DBPlayer).filter_by(team_id=team.id).delete(synchronize_session=False)
            db.session.query(DBTeamProfile).filter_by(team_id=team.id).delete(synchronize_session=False)
            db.session.delete(team)
            db.session.commit()

            app.logger.info(
                f"Team '{short_code}' (ID: {team.id}) deleted by {current_user.id}"
            )
            flash(f"Team '{team_name}' has been deleted.", "success")
        except IntegrityError as e:
            log_exception(e)
            db.session.rollback()
            app.logger.error(
                f"Integrity error deleting team {short_code}: {e}", exc_info=True
            )
            blockers = _diagnose_team_blockers(team.id)
            if blockers:
                flash(
                    f"Could not delete '{team_name}'. It is still referenced by "
                    f"{', '.join(blockers)}. Delete or clean up these first, "
                    "then try again.",
                    "danger",
                )
            else:
                flash(
                    f"Could not delete '{team_name}' due to a database constraint "
                    f"({type(e.orig).__name__ if getattr(e, 'orig', None) else 'IntegrityError'}). "
                    "Please contact support.",
                    "danger",
                )
        except Exception as e:
            log_exception(e)
            db.session.rollback()
            app.logger.error(f"Error deleting team from DB: {e}", exc_info=True)
            flash(
                f"Could not delete the team: {type(e).__name__}. "
                "Please contact support if this keeps happening.",
                "danger",
            )

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
                    def _edit_error(msg):
                        """Return the edit template with an error, avoiding repetition."""
                        db.session.rollback()
                        return render_template(
                            "team_create.html",
                            team=_build_team_data_for_edit(team),
                            edit=True,
                            error=msg,
                        )

                    team.name = request.form["team_name"].strip()
                    new_short_code = request.form["short_code"].strip().upper()
                    team.home_ground = request.form["home_ground"].strip()
                    team.pitch_preference = request.form["pitch_preference"]
                    team.team_color = request.form["team_color"]

                    if not SHORT_CODE_RE.match(new_short_code):
                        return _edit_error(
                            "Short code must be 2-5 uppercase alphanumeric characters."
                        )

                    name_conflict = _find_name_conflict(
                        user_id, team.name, exclude_team_id=team.id,
                    )
                    if name_conflict:
                        return _edit_error(
                            f"You already have a team named '{name_conflict.name}'. "
                            "Please use a different team name."
                        )

                    if new_short_code != team.short_code:
                        conflict = DBTeam.query.filter_by(
                            user_id=user_id, short_code=new_short_code,
                        ).first()
                        if conflict:
                            return _edit_error(
                                f"You already have a team with short code "
                                f"'{new_short_code}'."
                            )

                    action = request.form.get("action", "publish")
                    is_draft = action == "save_draft"

                    profiles, parse_err = _parse_profiles_payload(request.form)
                    if parse_err:
                        return _edit_error(parse_err)

                    non_empty = {
                        fmt: pd for fmt, pd in profiles.items() if pd["players"]
                    }
                    if not non_empty:
                        return _edit_error(
                            "At least one format profile with players is required."
                        )

                    for fmt, pdata in non_empty.items():
                        err = _validate_profile(fmt, pdata, is_draft)
                        if err:
                            return _edit_error(err)

                    team.is_draft = is_draft
                    team.short_code = new_short_code

                    # Upsert profiles and sync active squads without deleting
                    # historical Player identities used by archived scorecards.
                    existing_profiles = {
                        prof.format_type: prof for prof in list(team.profiles)
                    }
                    for fmt, pdata in non_empty.items():
                        prof = existing_profiles.get(fmt)
                        if not prof:
                            prof = DBTeamProfile(team_id=team.id, format_type=fmt)
                            db.session.add(prof)
                            db.session.flush()
                        _sync_profile_squad(team.id, prof, pdata)

                    # Formats removed in edit: detach active players first, then
                    # remove now-empty profile rows.
                    for fmt, prof in existing_profiles.items():
                        if fmt in non_empty:
                            continue
                        for p in DBPlayer.query.filter_by(profile_id=prof.id).all():
                            _detach_player_from_squad(p)
                        db.session.flush()
                        if DBPlayer.query.filter_by(profile_id=prof.id).count() == 0:
                            db.session.delete(prof)

                    db.session.commit()
                    status_msg = "Draft" if is_draft else "Active"
                    app.logger.info(
                        f"Team '{team.short_code}' (ID: {team.id}) updated as "
                        f"{status_msg} by {user_id}"
                    )
                    flash(f"Team updated as {status_msg}.", "success")
                    return redirect(url_for("manage_teams"))

                except Exception as e:
                    log_exception(e)
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
            log_exception(e)
            app.logger.error(f"Error in edit_team: {e}", exc_info=True)
            return redirect(url_for("manage_teams"))

    @app.route("/team/<short_code>/clone", methods=["POST"])
    @login_required
    def clone_team(short_code):
        """Duplicate an existing team as a new draft with '-Copy' suffix."""
        user_id = current_user.id
        try:
            source = DBTeam.query.filter_by(
                short_code=short_code, user_id=user_id,
            ).first()
            if not source:
                flash("Team not found.", "danger")
                return redirect(url_for("manage_teams"))

            # Generate unique name and short code for clone
            base_name = source.name + " (Copy)"
            clone_name = base_name
            suffix = 1
            while _find_name_conflict(user_id, clone_name):
                suffix += 1
                clone_name = f"{source.name} (Copy {suffix})"

            base_code = (source.short_code[:3] + "C").upper()
            clone_code = base_code
            code_suffix = 1
            while DBTeam.query.filter_by(user_id=user_id, short_code=clone_code).first():
                code_suffix += 1
                clone_code = (source.short_code[:2] + f"C{code_suffix}").upper()
                if not SHORT_CODE_RE.match(clone_code):
                    clone_code = f"CP{code_suffix}".upper()

            new_team = DBTeam(
                user_id=user_id,
                name=clone_name,
                short_code=clone_code,
                home_ground=source.home_ground,
                pitch_preference=source.pitch_preference,
                team_color=source.team_color,
                is_draft=True,
            )
            db.session.add(new_team)
            db.session.flush()

            for prof in source.profiles:
                new_prof = DBTeamProfile(
                    team_id=new_team.id, format_type=prof.format_type,
                )
                db.session.add(new_prof)
                db.session.flush()
                for p in prof.players:
                    db.session.add(DBPlayer(
                        team_id=new_team.id,
                        profile_id=new_prof.id,
                        name=p.name,
                        role=p.role,
                        batting_rating=p.batting_rating,
                        bowling_rating=p.bowling_rating,
                        fielding_rating=p.fielding_rating,
                        batting_hand=p.batting_hand,
                        bowling_type=p.bowling_type,
                        bowling_hand=p.bowling_hand,
                        is_captain=p.is_captain,
                        is_wicketkeeper=p.is_wicketkeeper,
                    ))

            db.session.commit()
            app.logger.info(
                f"Team '{source.short_code}' cloned as '{clone_code}' by {user_id}"
            )
            flash(f"Team cloned as draft '{clone_name}'.", "success")
        except Exception as e:
            log_exception(e)
            db.session.rollback()
            app.logger.error(f"Error cloning team: {e}", exc_info=True)
            flash("An error occurred while cloning the team.", "danger")

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
