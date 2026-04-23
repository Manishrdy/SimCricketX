import time
import uuid
from datetime import datetime

from app import db
from database.models import ActiveSession, Auction, AuctionCategory, League, Season


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


def test_quickstart_creates_starter_categories_and_budget(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    resp = client.post(f"/seasons/{season_id}/auction/quickstart", follow_redirects=False)
    assert resp.status_code == 302

    with app.app_context():
        auction = Auction.query.filter_by(season_id=season_id).first()
        assert auction is not None
        assert int(auction.uniform_budget or 0) == 1000
        categories = (
            AuctionCategory.query
            .filter_by(auction_id=auction.id)
            .order_by(AuctionCategory.display_order.asc())
            .all()
        )
        assert [c.name for c in categories] == ["Platinum", "Gold", "Silver", "Emerging"]
        assert [int(c.default_base_price or 0) for c in categories] == [300, 200, 120, 80]


def test_quickstart_is_idempotent(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    first = client.post(f"/seasons/{season_id}/auction/quickstart", follow_redirects=False)
    second = client.post(f"/seasons/{season_id}/auction/quickstart", follow_redirects=False)

    assert first.status_code == 302
    assert second.status_code == 302

    with app.app_context():
        auction = Auction.query.filter_by(season_id=season_id).first()
        assert auction is not None
        assert AuctionCategory.query.filter_by(auction_id=auction.id).count() == 4


def test_quickstart_disabled_when_flag_off(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = False
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    resp = client.post(f"/seasons/{season_id}/auction/quickstart", follow_redirects=False)
    assert resp.status_code == 302

    with app.app_context():
        assert Auction.query.filter_by(season_id=season_id).first() is None


def test_quickstart_blocked_when_finalized_lock_active(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="auction_ready")

    resp = client.post(f"/seasons/{season_id}/auction/quickstart", follow_redirects=False)

    assert resp.status_code == 409
    assert b"does not allow setup edits" in resp.data
    assert b"Reopen setup first" in resp.data


def test_quickstart_fills_missing_default_base_price(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    with app.app_context():
        auction = Auction(season_id=season_id, budget_mode="uniform", uniform_budget=1000)
        db.session.add(auction)
        db.session.flush()
        db.session.add(
            AuctionCategory(
                auction_id=auction.id,
                name="Rookies",
                default_base_price=None,
                display_order=10,
                max_players=15,
            )
        )
        db.session.commit()

    resp = client.post(f"/seasons/{season_id}/auction/quickstart", follow_redirects=False)
    assert resp.status_code == 302

    with app.app_context():
        auction = Auction.query.filter_by(season_id=season_id).first()
        assert auction is not None
        rookies = AuctionCategory.query.filter_by(auction_id=auction.id, name="Rookies").first()
        assert rookies is not None
        assert int(rookies.default_base_price or 0) == 100
