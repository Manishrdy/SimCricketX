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
            name=f"T{idx + 1}-{uuid.uuid4().hex[:6]}",
            short_code=f"S{uuid.uuid4().hex[:5].upper()}",
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
        rows.append(MasterPlayer(name=f"M-{i}-{uuid.uuid4().hex[:6]}", role="Batsman"))
    db.session.add_all(rows)
    db.session.commit()


def test_autofill_reaches_minimum_pool_when_available(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    with app.app_context():
        _seed_teams(regular_user.id, season_id, count=2)
        auction = Auction(season_id=season_id, min_players_per_team=2, max_players_per_team=6)
        db.session.add(auction)
        db.session.flush()
        db.session.add(AuctionCategory(auction_id=auction.id, name="Platinum", display_order=10, default_base_price=100))
        _seed_master_players(8)

    resp = client.post(f"/seasons/{season_id}/auction/autofill-min-pool", follow_redirects=False)
    assert resp.status_code == 302

    with app.app_context():
        auction = Auction.query.filter_by(season_id=season_id).first()
        assert auction is not None
        # 2 teams * min 2 = 4
        assert AuctionPlayer.query.filter_by(auction_id=auction.id).count() == 4


def test_autofill_is_idempotent_when_pool_already_sufficient(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    with app.app_context():
        _seed_teams(regular_user.id, season_id, count=2)
        auction = Auction(season_id=season_id, min_players_per_team=1, max_players_per_team=4)
        db.session.add(auction)
        db.session.flush()
        db.session.add(AuctionCategory(auction_id=auction.id, name="Gold", display_order=10, default_base_price=120))
        _seed_master_players(5)

    first = client.post(f"/seasons/{season_id}/auction/autofill-min-pool", follow_redirects=False)
    second = client.post(f"/seasons/{season_id}/auction/autofill-min-pool", follow_redirects=False)
    assert first.status_code == 302
    assert second.status_code == 302

    with app.app_context():
        auction = Auction.query.filter_by(season_id=season_id).first()
        assert auction is not None
        # 2 teams * min 1 = 2, no duplicate fill on second call.
        assert AuctionPlayer.query.filter_by(auction_id=auction.id).count() == 2


def test_autofill_disabled_when_flag_off(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = False
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    resp = client.post(f"/seasons/{season_id}/auction/autofill-min-pool", follow_redirects=False)
    assert resp.status_code == 302

    with app.app_context():
        assert Auction.query.filter_by(season_id=season_id).first() is None


def test_autofill_blocked_when_finalized_lock_active(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="auction_ready")

    resp = client.post(f"/seasons/{season_id}/auction/autofill-min-pool", follow_redirects=False)
    assert resp.status_code == 409
    assert b"does not allow setup edits" in resp.data
    assert b"Reopen setup first" in resp.data


def test_autofill_respects_category_capacity(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    with app.app_context():
        _seed_teams(regular_user.id, season_id, count=2)
        auction = Auction(season_id=season_id, min_players_per_team=3, max_players_per_team=8)
        db.session.add(auction)
        db.session.flush()
        db.session.add(
            AuctionCategory(
                auction_id=auction.id,
                name="Capped",
                display_order=10,
                default_base_price=100,
                max_players=2,
            )
        )
        _seed_master_players(10)

    resp = client.post(f"/seasons/{season_id}/auction/autofill-min-pool", follow_redirects=False)
    assert resp.status_code == 302

    with app.app_context():
        auction = Auction.query.filter_by(season_id=season_id).first()
        assert auction is not None
        # Required would be 6, but cap limits to 2.
        assert AuctionPlayer.query.filter_by(auction_id=auction.id).count() == 2
