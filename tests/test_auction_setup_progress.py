import time
import uuid
import re
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


def _seed_minimal_ready_setup(owner_id, season_id):
    season = db.session.get(Season, season_id)
    assert season is not None

    team_one = Team(user_id=owner_id, name="Ready XI A", short_code=f"A{uuid.uuid4().hex[:5].upper()}", season_id=season_id)
    team_two = Team(user_id=owner_id, name="Ready XI B", short_code=f"B{uuid.uuid4().hex[:5].upper()}", season_id=season_id)
    db.session.add_all([team_one, team_two])
    db.session.flush()

    st_one = SeasonTeam(season_id=season_id, team_id=team_one.id, display_name=team_one.name, purse_remaining=1000)
    st_two = SeasonTeam(season_id=season_id, team_id=team_two.id, display_name=team_two.name, purse_remaining=1000)
    db.session.add_all([st_one, st_two])

    auction = Auction(
        season_id=season_id,
        budget_mode="uniform",
        uniform_budget=1000,
        min_players_per_team=1,
        max_players_per_team=5,
    )
    db.session.add(auction)
    db.session.flush()

    category = AuctionCategory(
        auction_id=auction.id,
        name="Platinum",
        display_order=10,
        default_base_price=100,
    )
    db.session.add(category)
    db.session.flush()

    mp1 = MasterPlayer(name=f"P-{uuid.uuid4().hex[:8]}", role="Batsman")
    mp2 = MasterPlayer(name=f"P-{uuid.uuid4().hex[:8]}", role="Bowler")
    db.session.add_all([mp1, mp2])
    db.session.flush()

    ap1 = AuctionPlayer(
        auction_id=auction.id,
        category_id=category.id,
        master_player_id=mp1.id,
        name=mp1.name,
        role=mp1.role,
    )
    ap2 = AuctionPlayer(
        auction_id=auction.id,
        category_id=category.id,
        master_player_id=mp2.id,
        name=mp2.name,
        role=mp2.role,
    )
    db.session.add_all([ap1, ap2])
    db.session.commit()


def _assert_step_ready(html, step_id, ready):
    pattern = rf'data-step-id="{step_id}"[\s\S]*?data-step-ready="{ready}"'
    assert re.search(pattern, html), f"Expected step '{step_id}' to have ready={ready}"


def test_setup_progress_shows_pending_steps_when_empty(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    resp = client.get(f"/seasons/{season_id}/auction")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Setup progress" in html
    _assert_step_ready(html, "teams", 0)
    _assert_step_ready(html, "config", 0)
    _assert_step_ready(html, "categories", 0)
    _assert_step_ready(html, "players", 0)
    _assert_step_ready(html, "finalize", 0)
    assert "Next best action" in html


def test_setup_progress_marks_all_steps_complete_when_ready(app, client, regular_user):
    app.config["AUCTION_SIMPLIFIED_FLOW"] = True
    _force_login(client, regular_user.id)
    season_id = _create_season(regular_user.id, status="setup")

    with app.app_context():
        _seed_minimal_ready_setup(regular_user.id, season_id)

    resp = client.get(f"/seasons/{season_id}/auction")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    _assert_step_ready(html, "teams", 1)
    _assert_step_ready(html, "config", 1)
    _assert_step_ready(html, "categories", 1)
    _assert_step_ready(html, "players", 1)
    _assert_step_ready(html, "finalize", 1)
    assert "Ready to finalize." in html
