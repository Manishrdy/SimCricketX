import uuid
import time
from datetime import datetime

from app import db
from database.models import ActiveSession, Auction, League, Season


def _build_config_payload():
    return {
        "budget_mode": "uniform",
        "uniform_budget": "1500",
        "min_players_per_team": "12",
        "max_players_per_team": "25",
        "per_player_timer_seconds": "20",
        "draft_pick_timer_seconds": "30",
        "reauction_rounds": "0",
        "reauction_price_reduction_pct": "0",
        "category_order_mode": "manual",
        "bid_increment": "7",
    }


def _create_season(owner_id, *, status):
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
        # Auth middleware requires a recent human-verification stamp
        # for authenticated, non-exempt endpoints.
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


def test_auction_ready_allows_setup_edits_when_flag_disabled(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = False
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="auction_ready")

    resp = client.post(
        f"/seasons/{season_id}/auction/config",
        data=_build_config_payload(),
        follow_redirects=False,
    )

    assert resp.status_code == 302
    with app.app_context():
        auction = Auction.query.filter_by(season_id=season_id).first()
        assert auction is not None
        assert int(auction.bid_increment or 0) == 7
        assert int(auction.uniform_budget or 0) == 1500


def test_auction_ready_blocks_setup_edits_when_flag_enabled(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="auction_ready")

    resp = client.post(
        f"/seasons/{season_id}/auction/config",
        data=_build_config_payload(),
        follow_redirects=False,
    )

    assert resp.status_code == 409
    assert b"does not allow setup edits" in resp.data
    assert b"Reopen setup first" in resp.data

    with app.app_context():
        # _require_editable runs before _get_or_create_auction in config route.
        assert Auction.query.filter_by(season_id=season_id).first() is None


def test_setup_status_remains_editable_when_flag_enabled(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    resp = client.post(
        f"/seasons/{season_id}/auction/config",
        data=_build_config_payload(),
        follow_redirects=False,
    )

    assert resp.status_code == 302
    with app.app_context():
        auction = Auction.query.filter_by(season_id=season_id).first()
        assert auction is not None
        assert int(auction.bid_increment or 0) == 7


def test_reopen_still_unlocks_finalized_setup_when_flag_enabled(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="auction_ready")

    with app.app_context():
        auction = Auction(season_id=season_id, category_order="[10,20,30]")
        db.session.add(auction)
        db.session.commit()

    resp = client.post(
        f"/seasons/{season_id}/auction/reopen",
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with app.app_context():
        season = db.session.get(Season, season_id)
        auction = Auction.query.filter_by(season_id=season_id).first()
        assert season is not None
        assert season.status == "setup"
        assert auction is not None
        assert auction.category_order == "[]"
