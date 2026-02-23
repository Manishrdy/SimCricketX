"""
Pytest fixtures for SimCricketX testing.
Provides reusable test fixtures for database, app, clients, and test data.
"""

import os
import sys
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone

import pytest
import yaml
from werkzeug.security import generate_password_hash

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Enforce test-safe startup before importing app module.
TEST_SESSION_ROOT = Path(tempfile.mkdtemp(prefix="simcricketx_pytest_"))
os.environ.setdefault("SIMCRICKETX_TEST_MODE", "1")
os.environ.setdefault("SIMCRICKETX_SKIP_GLOBAL_APP", "1")
os.environ.setdefault(
    "SIMCRICKETX_TEST_DB_URI",
    f"sqlite:///{(TEST_SESSION_ROOT / 'session_bootstrap.db').as_posix()}",
)

from app import create_app, db
from database.models import (
    User,
    Team as DBTeam,
    Player as DBPlayer,
    Tournament,
    Match as DBMatch,
    MatchScorecard,
    TournamentFixture,
    ActiveSession,
)


@pytest.fixture(scope="session", autouse=True)
def _session_artifact_cleanup():
    """Clean only artifacts created during this test session."""
    project_root = Path(__file__).resolve().parent.parent
    tracked_dirs = [
        project_root / "data" / "matches",
        project_root / "data" / "backups",
    ]

    before = {}
    for d in tracked_dirs:
        if d.exists():
            before[d] = {p.name for p in d.iterdir()}
        else:
            before[d] = set()

    yield

    for d in tracked_dirs:
        if not d.exists():
            continue
        current = {p.name for p in d.iterdir()}
        created = current - before.get(d, set())
        for name in created:
            path = d / name
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            except Exception:
                pass

    shutil.rmtree(TEST_SESSION_ROOT, ignore_errors=True)


# ==================== Application Fixtures ====================

@pytest.fixture(scope="function")
def test_config(tmp_path):
    """Create a temporary config file for testing."""
    config_path = tmp_path / "config.yaml"
    config_data = {
        "app": {
            "maintenance_mode": False,
            "secret_key": "test-secret-key-for-testing-only-12345",
        },
        "database": {
            "uri": "sqlite:///:memory:",  # In-memory database for tests
        },
        "rate_limits": {
            "max_requests": 100,
            "window_seconds": 60,
            "admin_multiplier": 3,
            "login_limit": "50 per minute",
        },
        "bot_defense": {
            "enabled": False,  # Disable bot defense for tests
            "base_difficulty": 3,
            "elevated_difficulty": 4,
            "high_difficulty": 5,
            "elevated_threshold": 5,
            "high_threshold": 20,
            "window_minutes": 15,
            "ttl_seconds": 180,
            "max_counter": 10000000,
            "max_iterations": 1500000,
            "trusted_ip_prefixes": "",
        },
        "security": {
            "trust_proxy_headers": False,
        },
    }

    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    return config_path


@pytest.fixture(scope="function")
def app(test_config, tmp_path, monkeypatch):
    """Create and configure a test Flask application instance."""
    # Set the config path environment variable
    monkeypatch.setenv("SIMCRICKETX_CONFIG_PATH", str(test_config))
    monkeypatch.setenv("SIMCRICKETX_TEST_MODE", "1")
    monkeypatch.setenv("SIMCRICKETX_SKIP_GLOBAL_APP", "1")
    test_db_file = tmp_path / "pytest_app.db"
    monkeypatch.setenv("SIMCRICKETX_TEST_DB_URI", f"sqlite:///{test_db_file.as_posix()}")

    # Create necessary directories
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "teams").mkdir(exist_ok=True)
    (data_dir / "matches").mkdir(exist_ok=True)
    (data_dir / "stats").mkdir(exist_ok=True)
    (data_dir / "backups").mkdir(exist_ok=True)

    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir(exist_ok=True)

    # Set environment variables for paths
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("UPLOAD_FOLDER", str(uploads_dir))

    # Create app instance
    app = create_app()
    app.config.update({
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,  # Disable CSRF for easier testing
        "SECRET_KEY": "test-secret-key",
        "LOGIN_DISABLED": False,
    })

    # Create application context
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture(scope="function")
def client(app):
    """Create a test client for the app."""
    return app.test_client()


@pytest.fixture(scope="function")
def runner(app):
    """Create a test CLI runner for the app."""
    return app.test_cli_runner()


# ==================== User Fixtures ====================

@pytest.fixture(scope="function")
def regular_user(app):
    """Create a regular (non-admin) test user."""
    user = User(
        id="testuser@example.com",
        email="testuser@example.com",
        password_hash=generate_password_hash("Password123!"),
        display_name="Test User",
        is_admin=False,
        is_banned=False,
        force_password_reset=False,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture(scope="function")
def admin_user(app):
    """Create an admin test user."""
    user = User(
        id="admin@example.com",
        email="admin@example.com",
        password_hash=generate_password_hash("Admin123!"),
        display_name="Admin User",
        is_admin=True,
        is_banned=False,
        force_password_reset=False,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture(scope="function")
def banned_user(app):
    """Create a banned test user."""
    user = User(
        id="banned@example.com",
        email="banned@example.com",
        password_hash=generate_password_hash("Banned123!"),
        display_name="Banned User",
        is_admin=False,
        is_banned=True,
        force_password_reset=False,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(user)
    db.session.commit()
    return user


# ==================== Authentication Helpers ====================

@pytest.fixture(scope="function")
def authenticated_client(client, regular_user):
    """Return a client logged in as a regular user."""
    with client:
        client.post("/login", data={
            "email": regular_user.email,
            "password": "Password123!",
        }, follow_redirects=True)
        yield client


@pytest.fixture(scope="function")
def admin_client(client, admin_user):
    """Return a client logged in as an admin user."""
    with client:
        client.post("/login", data={
            "email": admin_user.email,
            "password": "Admin123!",
        }, follow_redirects=True)
        yield client


# ==================== Team Fixtures ====================

@pytest.fixture(scope="function")
def sample_team_data():
    """Return sample team data for testing.

    Roles use the casing expected by the production route validator:
    Wicketkeeper, Batsman, Bowler, All-rounder.
    12 players minimum satisfies active-team validation (12–25 required).
    """
    return {
        "name": "Test Warriors",
        "short_code": "TW",
        "players": [
            {
                "name": "Bob Keeper",
                "role": "Wicketkeeper",
                "batting_hand": "right",
                "bowling_type": "",
                "bowling_hand": "right",
                "is_wicketkeeper": True,
            },
            {
                "name": "John Doe",
                "role": "Batsman",
                "batting_hand": "right",
                "bowling_type": "",
                "bowling_hand": "right",
                "is_wicketkeeper": False,
            },
            # 6 All-rounders to satisfy the ≥6 bowler/all-rounder requirement
            *[{
                "name": f"Allrounder {i}",
                "role": "All-rounder",
                "batting_hand": "right",
                "bowling_type": "medium",
                "bowling_hand": "right",
                "is_wicketkeeper": False,
            } for i in range(1, 7)],
            # Fill to 12 with Batsmen
            *[{
                "name": f"Batsman {i}",
                "role": "Batsman",
                "batting_hand": "right",
                "bowling_type": "",
                "bowling_hand": "right",
                "is_wicketkeeper": False,
            } for i in range(1, 5)],
        ],
    }


@pytest.fixture(scope="function")
def test_team(app, regular_user, sample_team_data):
    """Create a test team directly in the database (bypasses route validation)."""
    db_team = DBTeam(
        name=sample_team_data["name"],
        short_code=sample_team_data["short_code"],
        user_id=regular_user.id,
        is_placeholder=False,
        is_draft=False,
    )
    db.session.add(db_team)
    db.session.flush()

    for player_data in sample_team_data["players"]:
        db_player = DBPlayer(
            team_id=db_team.id,
            name=player_data["name"],
            role=player_data["role"],
            batting_hand=player_data["batting_hand"],
            bowling_type=player_data["bowling_type"],
            bowling_hand=player_data["bowling_hand"],
            is_wicketkeeper=player_data["is_wicketkeeper"],
        )
        db.session.add(db_player)

    db.session.commit()
    return db_team


@pytest.fixture(scope="function")
def test_team_2(app, regular_user):
    """Create a second test team for matches/tournaments (12 players, 6 all-rounders)."""
    players = [
        {
            "name": "Champion WK",
            "role": "Wicketkeeper",
            "batting_hand": "right",
            "bowling_type": "",
            "bowling_hand": "right",
            "is_wicketkeeper": True,
        },
        *[{
            "name": f"Champion {i}",
            "role": "All-rounder",
            "batting_hand": "right",
            "bowling_type": "medium",
            "bowling_hand": "right",
            "is_wicketkeeper": False,
        } for i in range(1, 7)],
        *[{
            "name": f"Champ Bat {i}",
            "role": "Batsman",
            "batting_hand": "right",
            "bowling_type": "",
            "bowling_hand": "right",
            "is_wicketkeeper": False,
        } for i in range(1, 6)],
    ]

    db_team = DBTeam(
        name="Test Champions",
        short_code="TC",
        user_id=regular_user.id,
        is_placeholder=False,
        is_draft=False,
    )
    db.session.add(db_team)
    db.session.flush()

    for player_data in players:
        db_player = DBPlayer(
            team_id=db_team.id,
            name=player_data["name"],
            role=player_data["role"],
            batting_hand=player_data["batting_hand"],
            bowling_type=player_data["bowling_type"],
            bowling_hand=player_data["bowling_hand"],
            is_wicketkeeper=player_data["is_wicketkeeper"],
        )
        db.session.add(db_player)

    db.session.commit()
    return db_team


# ==================== Tournament Fixtures ====================

@pytest.fixture(scope="function")
def test_tournament(app, regular_user, test_team, test_team_2):
    """Create a test tournament with fixtures."""
    tournament = Tournament(
        name="Test Tournament",
        user_id=regular_user.id,
        mode="round_robin",
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(tournament)
    db.session.commit()
    return tournament


# ==================== Utility Functions ====================

def login_user(client, email, password):
    """Helper to log in a user via the test client."""
    return client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=True
    )


def logout_user(client):
    """Helper to log out the current user."""
    return client.post("/logout", follow_redirects=True)


# ==================== Pytest Configuration ====================

def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )
    config.addinivalue_line(
        "markers", "unit: marks tests as unit tests"
    )
