import time
import uuid
from datetime import datetime

from app import db
from database.models import (
    ActiveSession,
    Auction,
    AuctionCategory,
    AuctionPlayer,
    League,
    MasterPlayer,
    Season,
    SeasonTeam,
    Team,
)


def _create_season(owner_id, *, status="setup"):
    marker = uuid.uuid4().hex[:8]
    league = League(
        user_id=owner_id,
        name=f"League-{marker}",
        short_code=f"L{marker[:3].upper()}",
    )
    db.session.add(league)
    db.session.flush()

    season = Season(
        league_id=league.id,
        name=f"Season-{marker}",
        format="T20",
        auction_mode="traditional",
        status=status,
    )
    db.session.add(season)
    db.session.commit()
    return season.id


def _force_login(client, user_id):
    token = uuid.uuid4().hex
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["session_token"] = token
        sess["cf_ts_verified"] = time.time()
    db.session.add(
        ActiveSession(
            session_token=token,
            user_id=user_id,
            login_at=datetime.utcnow(),
            last_active=datetime.utcnow(),
        )
    )
    db.session.commit()


def _seed_teams(owner_id, season_id, count):
    for idx in range(count):
        team = Team(
            user_id=owner_id,
            name=f"AT-{idx + 1}-{uuid.uuid4().hex[:6]}",
            short_code=f"AS{uuid.uuid4().hex[:4].upper()}",
            season_id=season_id,
        )
        db.session.add(team)
        db.session.flush()
        db.session.add(
            SeasonTeam(
                season_id=season_id,
                team_id=team.id,
                display_name=team.name,
                purse_remaining=1000,
            )
        )
    db.session.commit()


def _seed_master_players(n):
    rows = []
    for i in range(n):
        rows.append(MasterPlayer(name=f"AP-{i}-{uuid.uuid4().hex[:6]}", role="Batsman"))
    db.session.add_all(rows)
    db.session.commit()


def test_auto_prepare_bootstraps_and_reaches_finalize_ready(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    with app.app_context():
        _seed_teams(regular_user.id, season_id, count=2)
        db.session.add(Auction(season_id=season_id, min_players_per_team=2, max_players_per_team=6))
        db.session.commit()
        _seed_master_players(10)

    resp = client.post(f"/seasons/{season_id}/auction/auto-prepare", follow_redirects=False)
    assert resp.status_code == 302

    with app.app_context():
        auction = Auction.query.filter_by(season_id=season_id).first()
        assert auction is not None
        assert int(auction.uniform_budget or 0) == 1000
        assert AuctionCategory.query.filter_by(auction_id=auction.id).count() == 4
        assert AuctionPlayer.query.filter_by(auction_id=auction.id).count() == 4

    page = client.get(f"/seasons/{season_id}/auction")
    assert page.status_code == 200
    assert b"Ready to finalize." in page.data


def test_auto_prepare_is_idempotent(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    with app.app_context():
        _seed_teams(regular_user.id, season_id, count=2)
        db.session.add(Auction(season_id=season_id, min_players_per_team=1, max_players_per_team=5))
        db.session.commit()
        _seed_master_players(10)

    first = client.post(f"/seasons/{season_id}/auction/auto-prepare", follow_redirects=False)
    second = client.post(f"/seasons/{season_id}/auction/auto-prepare", follow_redirects=False)
    assert first.status_code == 302
    assert second.status_code == 302

    with app.app_context():
        auction = Auction.query.filter_by(season_id=season_id).first()
        assert auction is not None
        assert AuctionCategory.query.filter_by(auction_id=auction.id).count() == 4
        assert AuctionPlayer.query.filter_by(auction_id=auction.id).count() == 2


def test_auto_prepare_disabled_when_flag_off(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = False
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    resp = client.post(f"/seasons/{season_id}/auction/auto-prepare", follow_redirects=False)
    assert resp.status_code == 302

    with app.app_context():
        assert Auction.query.filter_by(season_id=season_id).first() is None


def test_auto_prepare_blocked_when_finalized_lock_active(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="auction_ready")

    resp = client.post(f"/seasons/{season_id}/auction/auto-prepare", follow_redirects=False)
    assert resp.status_code == 409
    assert b"does not allow setup edits" in resp.data
    assert b"Reopen setup first" in resp.data


def test_auto_prepare_partial_when_supply_is_insufficient(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    with app.app_context():
        _seed_teams(regular_user.id, season_id, count=2)
        db.session.add(Auction(season_id=season_id, min_players_per_team=3, max_players_per_team=8))
        db.session.commit()
        _seed_master_players(3)

    resp = client.post(f"/seasons/{season_id}/auction/auto-prepare", follow_redirects=False)
    assert resp.status_code == 302

    with app.app_context():
        auction = Auction.query.filter_by(season_id=season_id).first()
        assert auction is not None
        assert AuctionCategory.query.filter_by(auction_id=auction.id).count() == 4
        # Need 6, but only 3 available.
        assert AuctionPlayer.query.filter_by(auction_id=auction.id).count() == 3
