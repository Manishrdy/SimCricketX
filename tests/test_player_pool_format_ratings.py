from app import db
from database.models import MasterPlayer, Player, Team, TeamProfile, UserPlayer
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

from migrations.add_player_pool_format_ratings import run_migration


def _format_rating_payload(name="Format Specialist", role="All-rounder"):
    return {
        "name": name,
        "role": role,
        "batting_hand": "Right",
        "bowling_type": "Fast-medium",
        "bowling_hand": "Right",
        "t20_batting_rating": "81",
        "t20_bowling_rating": "62",
        "t20_fielding_rating": "77",
        "list_a_batting_rating": "84",
        "list_a_bowling_rating": "66",
        "list_a_fielding_rating": "79",
        "fc_batting_rating": "88",
        "fc_bowling_rating": "71",
        "fc_fielding_rating": "75",
        "fc_technique_rating": "91",
        "fc_temperament_rating": "86",
        "fc_stamina_rating": "83",
        "is_captain": "true",
        "is_wicketkeeper": "true",
    }


def test_user_form_persists_all_format_ratings(authenticated_client, regular_user):
    response = authenticated_client.post(
        "/player-pool/add",
        data=_format_rating_payload(),
        follow_redirects=False,
    )
    assert response.status_code == 302

    player = UserPlayer.query.filter_by(user_id=regular_user.id, name="Format Specialist").one()
    assert (player.batting_rating, player.bowling_rating, player.fielding_rating) == (81, 62, 77)
    assert (
        player.list_a_batting_rating,
        player.list_a_bowling_rating,
        player.list_a_fielding_rating,
    ) == (84, 66, 79)
    assert (
        player.fc_batting_rating,
        player.fc_bowling_rating,
        player.fc_fielding_rating,
        player.fc_technique_rating,
        player.fc_temperament_rating,
        player.fc_stamina_rating,
    ) == (88, 71, 75, 91, 86, 83)


def test_admin_form_persists_global_format_ratings(admin_client):
    response = admin_client.post(
        "/admin/player-pool/add",
        data=_format_rating_payload(name="Global Format Star"),
        follow_redirects=False,
    )
    assert response.status_code == 302

    player = MasterPlayer.query.filter_by(name="Global Format Star").one()
    assert player.list_a_batting_rating == 84
    assert player.fc_batting_rating == 88
    assert player.fc_technique_rating == 91
    assert player.fc_temperament_rating == 86
    assert player.fc_stamina_rating == 83


def test_bowler_cannot_be_saved_as_wicketkeeper(authenticated_client, regular_user):
    payload = _format_rating_payload(name="Specialist Bowler", role="Bowler")
    response = authenticated_client.post("/player-pool/add", data=payload)
    assert response.status_code == 302

    player = UserPlayer.query.filter_by(user_id=regular_user.id, name="Specialist Bowler").one()
    assert player.is_wicketkeeper is False


def _assert_format_form(response):
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    for field in (
        "t20_batting_rating",
        "list_a_batting_rating",
        "fc_batting_rating",
        "fc_technique_rating",
        "fc_temperament_rating",
        "fc_stamina_rating",
    ):
        assert f'name="{field}"' in html
    assert 'id="wicketkeeperCheckbox"' in html


def test_user_form_renders_all_format_fields(authenticated_client):
    _assert_format_form(authenticated_client.get("/player-pool/add"))


def test_admin_form_renders_all_format_fields(admin_client):
    _assert_format_form(admin_client.get("/admin/player-pool/add"))


def test_squad_add_uses_selected_format_ratings(authenticated_client, regular_user):
    master = MasterPlayer(name="Three Format Pro", role="All-rounder", **{
        "batting_rating": 70,
        "bowling_rating": 60,
        "fielding_rating": 65,
        "list_a_batting_rating": 75,
        "list_a_bowling_rating": 64,
        "list_a_fielding_rating": 68,
        "fc_batting_rating": 82,
        "fc_bowling_rating": 69,
        "fc_fielding_rating": 66,
        "fc_technique_rating": 90,
        "fc_temperament_rating": 87,
        "fc_stamina_rating": 85,
    })
    team = Team(
        user_id=regular_user.id,
        name="Format XI",
        short_code="FMT",
        home_ground="Test Ground",
        pitch_preference="Flat",
        team_color="#0f766e",
        is_draft=True,
    )
    db.session.add_all([master, team])
    db.session.flush()
    profiles = {
        fmt: TeamProfile(team_id=team.id, format_type=fmt)
        for fmt in ("T20", "ListA", "FC")
    }
    db.session.add_all(profiles.values())
    db.session.commit()

    token = f"master_{master.id}"
    for fmt in profiles:
        response = authenticated_client.post(
            f"/api/team/{team.id}/squad/{fmt}/add",
            json={"player_id": token},
        )
        assert response.status_code == 200

    t20 = Player.query.filter_by(profile_id=profiles["T20"].id).one()
    list_a = Player.query.filter_by(profile_id=profiles["ListA"].id).one()
    fc = Player.query.filter_by(profile_id=profiles["FC"].id).one()
    assert (t20.batting_rating, t20.bowling_rating, t20.fielding_rating) == (70, 60, 65)
    assert (list_a.batting_rating, list_a.bowling_rating, list_a.fielding_rating) == (75, 64, 68)
    assert (fc.batting_rating, fc.bowling_rating, fc.fielding_rating) == (82, 69, 66)
    assert (fc.technique_rating, fc.temperament_rating, fc.stamina_rating) == (90, 87, 85)


def test_format_rating_migration_backfills_legacy_values_and_is_idempotent(tmp_path):
    legacy_app = Flask(__name__)
    legacy_app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path / 'legacy_pool.db'}"
    legacy_db = SQLAlchemy(legacy_app)

    with legacy_app.app_context():
        with legacy_db.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE master_players (
                    id INTEGER PRIMARY KEY,
                    batting_rating INTEGER,
                    bowling_rating INTEGER,
                    fielding_rating INTEGER
                )
            """))
            conn.execute(text("""
                CREATE TABLE user_players (
                    id INTEGER PRIMARY KEY,
                    batting_rating INTEGER,
                    bowling_rating INTEGER,
                    fielding_rating INTEGER
                )
            """))
            conn.execute(text("INSERT INTO master_players VALUES (1, 88, 42, 79)"))
            conn.execute(text("INSERT INTO user_players VALUES (1, 73, 65, 81)"))

        run_migration(legacy_db, legacy_app)
        run_migration(legacy_db, legacy_app)

        master_columns = {column["name"] for column in inspect(legacy_db.engine).get_columns("master_players")}
        assert {
            "list_a_batting_rating",
            "fc_batting_rating",
            "fc_technique_rating",
            "fc_temperament_rating",
            "fc_stamina_rating",
        } <= master_columns

        with legacy_db.engine.connect() as conn:
            master = conn.execute(text("""
                SELECT list_a_batting_rating, list_a_bowling_rating,
                       fc_batting_rating, fc_fielding_rating,
                       fc_technique_rating, fc_temperament_rating, fc_stamina_rating
                FROM master_players WHERE id = 1
            """)).one()
            user = conn.execute(text("""
                SELECT list_a_batting_rating, fc_bowling_rating, fc_technique_rating
                FROM user_players WHERE id = 1
            """)).one()

        assert tuple(master) == (88, 42, 88, 79, 50, 50, 50)
        assert tuple(user) == (73, 65, 50)
