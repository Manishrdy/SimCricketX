"""
AUCTION-REDESIGN Phase 6 — Roster sync + export.

Once an auction completes, copy each SeasonTeam's roster of sold/picked
AuctionPlayers into the underlying Team's `TeamProfile` (format =
`season.format`) so the empty teams become playable squads.

Idempotent: rerunning the sync wipes and repopulates the target profile,
so it's safe to call from `complete_auction()` and also expose as a
manual "re-sync" HTTP action.

Pure functions here — Flask routes live in `auction_routes.py`.
"""

from datetime import datetime


# ── Captain / WK heuristic ────────────────────────────────────────────────────

def _pick_wicketkeeper(players):
    """Return (wk_player, forced_role_change). If any player is flagged
    is_wicketkeeper or has role='Wicketkeeper', use them. Otherwise promote
    the most fielding-capable player to Wicketkeeper."""
    wk = next((p for p in players if getattr(p, "is_wicketkeeper", False)), None)
    if wk:
        return wk, False
    wk = next((p for p in players if (p.role or "").lower() == "wicketkeeper"), None)
    if wk:
        return wk, False
    if not players:
        return None, False
    # Fall back: promote the highest-fielding player.
    promoted = max(players, key=lambda p: (int(p.fielding_rating or 0), int(p.batting_rating or 0)))
    return promoted, True


def _pick_captain(players, wk_player):
    """Return captain player. Prefer is_captain flag; else the most-valuable
    player by combined rating who isn't the WK; else the WK; else the first."""
    cap = next((p for p in players if getattr(p, "is_captain", False)), None)
    if cap:
        return cap
    non_wk = [p for p in players if p is not wk_player] or players
    if not non_wk:
        return None
    return max(non_wk, key=lambda p: (
        int(p.batting_rating or 0) + int(p.bowling_rating or 0) + int(p.fielding_rating or 0),
    ))


# ── Sync ──────────────────────────────────────────────────────────────────────

def sync_season_rosters(db, season, auction,
                        *, DBSeasonTeam, DBTeam, DBTeamProfile, DBPlayer, DBAuctionPlayer,
                        DBAuctionCategory):
    """Populate each season-team's TeamProfile with the roster it acquired
    in this auction. Returns a list of per-team reports:
        {team_id, display_name, count, captain_name, wk_name, publish_ready, issues[]}.
    Caller commits.
    """
    season_teams = (DBSeasonTeam.query
                    .filter_by(season_id=season.id)
                    .order_by(DBSeasonTeam.id.asc())
                    .all())
    category_by_id = {c.id: c.name for c in DBAuctionCategory.query.filter_by(auction_id=auction.id).all()}
    results = []

    for st in season_teams:
        team = DBTeam.query.get(st.team_id)
        if team is None:
            continue

        fmt = season.format or "T20"

        # Find or create the TeamProfile for this format.
        profile = (DBTeamProfile.query
                   .filter_by(team_id=team.id, format_type=fmt)
                   .first())
        if profile is None:
            profile = DBTeamProfile(team_id=team.id, format_type=fmt)
            db.session.add(profile)
            db.session.flush()

        # Wipe any pre-existing Player rows under this profile so the sync is
        # strictly idempotent. A partial re-sync with stale rows is confusing.
        DBPlayer.query.filter_by(profile_id=profile.id).delete(synchronize_session=False)

        roster_rows = (DBAuctionPlayer.query
                       .filter_by(auction_id=auction.id, sold_to_season_team_id=st.id)
                       .all())

        # Build draft Player rows (no captain/WK flags yet — set after).
        new_players = []
        for ap in roster_rows:
            p = DBPlayer(
                team_id=team.id,
                profile_id=profile.id,
                name=ap.name,
                role=ap.role or "Batsman",
                batting_rating=ap.batting_rating or 50,
                bowling_rating=ap.bowling_rating or 50,
                fielding_rating=ap.fielding_rating or 50,
                batting_hand=ap.batting_hand,
                bowling_type=ap.bowling_type,
                bowling_hand=ap.bowling_hand,
                is_captain=False,
                is_wicketkeeper=False,
                master_player_id=ap.master_player_id,
                user_player_id=ap.user_player_id,
            )
            db.session.add(p)
            new_players.append(p)

        db.session.flush()

        wk_player, forced_role = _pick_wicketkeeper(new_players)
        if wk_player is not None:
            if forced_role:
                wk_player.role = "Wicketkeeper"
            wk_player.is_wicketkeeper = True
        captain = _pick_captain(new_players, wk_player)
        if captain is not None:
            captain.is_captain = True

        # Publish readiness: mirror the rules in /api/team/<id>/squad/<fmt>/publish.
        issues = []
        count = len(new_players)
        if not (11 <= count <= 25):
            issues.append(f"Need 11–25 players (have {count}).")
        wk_count = sum(1 for p in new_players if p.role == "Wicketkeeper")
        if wk_count < 1:
            issues.append("Need at least 1 Wicketkeeper.")
        bowl_count = sum(1 for p in new_players if p.role in ("Bowler", "All-rounder"))
        if bowl_count < 5:
            issues.append("Need at least 5 Bowlers/All-rounders.")
        if not any(p.is_captain for p in new_players):
            issues.append("Captain not set.")
        if not any(p.is_wicketkeeper for p in new_players):
            issues.append("Wicketkeeper not set.")

        publish_ready = not issues
        # Flip the Team out of draft state only when the roster can actually be published.
        if publish_ready:
            team.is_draft = False
        else:
            team.is_draft = True

        results.append({
            "team_id": team.id,
            "season_team_id": st.id,
            "display_name": st.display_name,
            "count": count,
            "captain_name": captain.name if captain else None,
            "wk_name": wk_player.name if wk_player else None,
            "publish_ready": publish_ready,
            "issues": issues,
            "format": fmt,
            # Leave the category breakdown for the export, not the summary.
        })

    return results


# ── Export helpers ────────────────────────────────────────────────────────────

def export_rosters_json(db, season, auction,
                        *, DBSeasonTeam, DBAuctionPlayer, DBAuctionCategory):
    """Return a JSON-serializable dict describing the final auction state."""
    cats = {c.id: c.name for c in DBAuctionCategory.query.filter_by(auction_id=auction.id).all()}
    season_teams = (DBSeasonTeam.query
                    .filter_by(season_id=season.id)
                    .order_by(DBSeasonTeam.id.asc())
                    .all())
    teams_payload = []
    for st in season_teams:
        rows = (DBAuctionPlayer.query
                .filter_by(auction_id=auction.id, sold_to_season_team_id=st.id)
                .all())
        rows.sort(key=lambda ap: (int(ap.sold_price or 0) * -1, ap.name or ""))
        teams_payload.append({
            "team_id": st.team_id,
            "season_team_id": st.id,
            "display_name": st.display_name,
            "purse_remaining": int(st.purse_remaining or 0),
            "players_bought": int(st.players_bought or 0),
            "roster": [{
                "name": ap.name,
                "role": ap.role,
                "category": cats.get(ap.category_id, "?"),
                "batting_rating": ap.batting_rating,
                "bowling_rating": ap.bowling_rating,
                "fielding_rating": ap.fielding_rating,
                "batting_hand": ap.batting_hand,
                "bowling_type": ap.bowling_type,
                "bowling_hand": ap.bowling_hand,
                "sold_price": int(ap.sold_price or 0),
                "sold_in_round": ap.sold_in_round,
            } for ap in rows],
        })

    # Unsold list — useful for reporting unmatched players.
    unsold = (DBAuctionPlayer.query
              .filter(DBAuctionPlayer.auction_id == auction.id,
                      DBAuctionPlayer.status == "unsold")
              .all())
    return {
        "season_id": season.id,
        "season_name": season.name,
        "format": season.format,
        "auction_mode": season.auction_mode,
        "status": season.status,
        "completed_at": auction.ended_at.isoformat() if auction.ended_at else None,
        "categories": [{"id": c.id, "name": c.name} for c in DBAuctionCategory.query
                       .filter_by(auction_id=auction.id)
                       .order_by(DBAuctionCategory.display_order.asc()).all()],
        "teams": teams_payload,
        "unsold": [{
            "name": ap.name,
            "role": ap.role,
            "category": cats.get(ap.category_id, "?"),
        } for ap in unsold],
    }


def export_rosters_csv(db, season, auction,
                       *, DBSeasonTeam, DBAuctionPlayer, DBAuctionCategory):
    """Return a CSV string (one row per player). Columns chosen for quick
    spreadsheet auditability — teams grouped, sold_price last."""
    import io
    import csv
    cats = {c.id: c.name for c in DBAuctionCategory.query.filter_by(auction_id=auction.id).all()}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "team", "player", "role", "category",
        "batting", "bowling", "fielding",
        "batting_hand", "bowling_type", "bowling_hand",
        "sold_price", "round", "status",
    ])
    season_teams = (DBSeasonTeam.query
                    .filter_by(season_id=season.id)
                    .order_by(DBSeasonTeam.id.asc())
                    .all())
    team_name_by_st_id = {st.id: st.display_name for st in season_teams}
    rows = (DBAuctionPlayer.query
            .filter_by(auction_id=auction.id)
            .order_by(DBAuctionPlayer.sold_to_season_team_id.asc().nullslast(),
                      DBAuctionPlayer.name.asc())
            .all())
    for ap in rows:
        team_label = team_name_by_st_id.get(ap.sold_to_season_team_id, "—")
        writer.writerow([
            team_label,
            ap.name,
            ap.role or "",
            cats.get(ap.category_id, ""),
            ap.batting_rating or 0,
            ap.bowling_rating or 0,
            ap.fielding_rating or 0,
            ap.batting_hand or "",
            ap.bowling_type or "",
            ap.bowling_hand or "",
            int(ap.sold_price) if ap.sold_price is not None else "",
            ap.sold_in_round if ap.sold_in_round is not None else "",
            ap.status,
        ])
    return buf.getvalue()
