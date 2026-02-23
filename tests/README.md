# SimCricketX Testing Documentation

## 📋 Overview

Complete test suite for SimCricketX with **126 routes** tested across all modules with GitHub Actions CI/CD integration.

## 🎯 Test Coverage

| Module | Routes | Test File | Status |
|--------|---------|-----------|--------|
| **Core** | 6 | `test_core_routes.py` | ✅ |
| **Auth** | 7 | `test_auth_routes.py` | ✅ |
| **Team** | 4 | `test_team_routes.py` | ✅ |
| **Match** | 20 | `test_match_routes.py` | ✅ |
| **Tournament** | 5 | `test_tournament_routes.py` | ✅ |
| **Stats** | 8 | `test_stats_routes.py` | ✅ |
| **Admin** | 64 | `test_admin_routes.py` | ✅ |
| **Legacy** | 12 | `test_admin_security.py`, etc. | ✅ |
| **TOTAL** | **126** | **8 files** | ✅ |

## 🚀 Quick Start

### 1. Install Dependencies

```bash
# Install testing dependencies
pip install -r requirements-dev.txt
```

### 2. Run Tests Locally

```bash
# Run all tests
pytest

# Run tests with coverage
pytest --cov=. --cov-report=html

# Run specific test file
pytest tests/test_auth_routes.py

# Run tests matching a pattern
pytest -k "test_login"

# Run tests with specific marker
pytest -m "auth"

# Verbose output
pytest -v

# Stop at first failure
pytest -x
```

### 3. View Coverage Report

```bash
# Generate HTML coverage report
pytest --cov=. --cov-report=html

# Open report (Windows)
start htmlcov/index.html

# Open report (Linux/Mac)
open htmlcov/index.html
```

## 📁 Project Structure

```
SimCricketX/
├── tests/
│   ├── conftest.py              # Shared fixtures & configuration
│   ├── test_core_routes.py      # Core routes (home, ground conditions)
│   ├── test_auth_routes.py      # Authentication & registration
│   ├── test_team_routes.py      # Team management
│   ├── test_match_routes.py     # Match simulation & archives
│   ├── test_tournament_routes.py # Tournament management
│   ├── test_stats_routes.py     # Statistics & analytics
│   ├── test_admin_routes.py     # Admin panel (64 routes)
│   └── test_admin_security.py   # Legacy security tests
├── .github/
│   └── workflows/
│       └── ci.yml               # GitHub Actions CI/CD
├── pytest.ini                   # Pytest configuration
├── .coveragerc                  # Coverage configuration
└── requirements-dev.txt         # Testing dependencies
```

## 🧪 Test Fixtures Available

### Application Fixtures
- `app` - Flask test application
- `client` - Test client (unauthenticated)
- `authenticated_client` - Client logged in as regular user
- `admin_client` - Client logged in as admin

### User Fixtures
- `regular_user` - Regular user account
- `admin_user` - Admin user account
- `banned_user` - Banned user account

### Data Fixtures
- `test_team` - Sample team with 11 players
- `test_team_2` - Second team for matches
- `test_tournament` - Sample tournament
- `sample_team_data` - Team data dictionary

## 📊 Test Categories

### 1. Authentication Tests (`test_auth_routes.py`)
- ✅ User registration (valid/invalid)
- ✅ Login/logout
- ✅ Password validation
- ✅ Password change
- ✅ Display name management
- ✅ Account deletion
- ✅ Banned user handling

### 2. Core Routes Tests (`test_core_routes.py`)
- ✅ Home page access
- ✅ Ground conditions CRUD
- ✅ Ground conditions modes
- ✅ Maintenance mode

### 3. Team Management Tests (`test_team_routes.py`)
- ✅ Team creation & validation
- ✅ Team listing
- ✅ Team editing
- ✅ Team deletion
- ✅ Player validation
- ✅ Ownership checks

### 4. Match Tests (`test_match_routes.py`)
- ✅ Match setup & creation
- ✅ Toss operations
- ✅ Impact player swaps
- ✅ Match simulation
- ✅ Super over handling
- ✅ Commentary saving
- ✅ Match archiving
- ✅ Bulk operations

### 5. Tournament Tests (`test_tournament_routes.py`)
- ✅ Tournament creation (round robin, knockout, series)
- ✅ Tournament listing
- ✅ Tournament deletion
- ✅ Fixture management
- ✅ Re-simulation
- ✅ Mode validation

### 6. Statistics Tests (`test_stats_routes.py`)
- ✅ Stats dashboard
- ✅ Data export (CSV/JSON)
- ✅ Player comparison
- ✅ Bowling figures
- ✅ Partnership tracking
- ✅ Filtering & aggregation

### 7. Admin Panel Tests (`test_admin_routes.py`)
- ✅ User management (64+ endpoints)
- ✅ Database operations
- ✅ System configuration
- ✅ Security features
- ✅ Audit logs
- ✅ Maintenance mode
- ✅ Data export
- ✅ File management
- ✅ Analytics
- ✅ Impersonation

## 🔧 GitHub Actions CI

### Workflow Features
- ✅ Multi-OS testing (Ubuntu, Windows)
- ✅ Multi-Python version (3.9, 3.10, 3.11)
- ✅ Automated test execution
- ✅ Coverage reporting
- ✅ Code quality checks (flake8, black, isort)
- ✅ Security scanning (safety, bandit)

### Triggers
- Push to `main` or `develop` branches
- Pull requests to `main` or `develop`
- Manual workflow dispatch

### View Results
1. Go to GitHub repository
2. Click "Actions" tab
3. View workflow runs and test results

## 📈 Coverage Goals

- **Overall Coverage**: 80%+
- **Critical Routes**: 90%+
- **Core Business Logic**: 95%+

## 🎨 Test Markers

Use pytest markers to selectively run tests:

```bash
# Run only auth tests
pytest -m auth

# Run only admin tests
pytest -m admin

# Run integration tests
pytest -m integration

# Run unit tests only
pytest -m unit

# Skip slow tests
pytest -m "not slow"
```

## 🐛 Debugging Tests

```bash
# Show print statements
pytest -s

# Show local variables on failure
pytest -l

# Enter debugger on failure
pytest --pdb

# Show detailed traceback
pytest --tb=long

# Run last failed tests
pytest --lf

# Run failed tests first, then others
pytest --ff
```

## 📝 Writing New Tests

### Template for Route Test

```python
class TestNewRoute:
    """Tests for new route."""

    def test_route_authenticated(self, authenticated_client):
        """Test accessing route when logged in."""
        response = authenticated_client.get("/new-route")
        assert response.status_code == 200

    def test_route_unauthenticated(self, client):
        """Test accessing route without login."""
        response = client.get("/new-route")
        assert response.status_code in [302, 401]

    def test_route_data_creation(self, authenticated_client, app):
        """Test creating data via route."""
        response = authenticated_client.post(
            "/new-route",
            data={"field": "value"},
            follow_redirects=True
        )
        assert response.status_code == 200
        
        # Verify in database
        with app.app_context():
            # Check database state
            pass
```

## 🔒 Security Testing

Security tests included:
- ✅ Authentication bypass attempts
- ✅ Authorization checks
- ✅ CSRF protection
- ✅ SQL injection prevention
- ✅ XSS prevention
- ✅ Rate limiting
- ✅ Session management

## 📦 Continuous Integration

### Local Pre-commit

```bash
# Run tests before committing
pytest

# Run with coverage check
pytest --cov=. --cov-report=term --cov-fail-under=80
```

### CI Pipeline Steps
1. **Checkout code**
2. **Setup Python** (multiple versions)
3. **Install dependencies**
4. **Run tests** with coverage
5. **Upload coverage** to Codecov
6. **Run linters** (flake8, black, isort)
7. **Security scan** (safety, bandit)
8. **Archive results**

## 🎯 Best Practices

1. **Isolation**: Each test is independent
2. **Fixtures**: Reuse common setup via fixtures
3. **Naming**: Clear, descriptive test names
4. **Assertions**: Specific, meaningful assertions
5. **Cleanup**: Automatic cleanup via fixtures
6. **Coverage**: Aim for high coverage
7. **Documentation**: Document complex test logic

## 🚨 Common Issues

### Issue: Tests fail locally but pass in CI
**Solution**: Check for environment-specific dependencies

### Issue: Database errors
**Solution**: Ensure in-memory SQLite is used for tests

### Issue: Slow tests
**Solution**: Mark slow tests with `@pytest.mark.slow`

### Issue: Flaky tests
**Solution**: Investigate timing issues, add retries if needed

## 📚 Resources

- [Pytest Documentation](https://docs.pytest.org/)
- [Flask Testing Guide](https://flask.palletsprojects.com/en/latest/testing/)
- [Coverage.py Docs](https://coverage.readthedocs.io/)

## 🎉 Success Metrics

- ✅ All 126 routes have tests
- ✅ CI/CD pipeline configured
- ✅ Multi-OS, multi-Python testing
- ✅ Coverage reporting
- ✅ Code quality checks
- ✅ Security scanning

## 📞 Support

For issues or questions:
1. Check test output for detailed errors
2. Review test documentation
3. Check GitHub Actions logs
4. Create an issue with test logs

---

**Status**: ✅ Complete  
**Routes Tested**: 126  
**Test Files**: 8  
**CI/CD**: ✅ Active
