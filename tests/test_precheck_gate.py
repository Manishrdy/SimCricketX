"""The migration precheck must run wherever the real database is in use.

`SIMCRICKETX_TEST_MODE=1` is set by two unrelated things: pytest (always
paired with `SIMCRICKETX_TEST_DB_URI` and a schema built by `db.create_all`),
and the local dev launcher (`.claude/launch.json`), which sets it alone purely
so `utils/turnstile.py` bypasses the CAPTCHA during browser-preview sessions.

Gating the precheck on `test_mode` therefore skipped it for the dev launcher
too — which still opens the real, persistent `cricket_sim.db` — so a newly
registered migration silently never applied on a dev machine whose only
startup path is that launcher. Production was never affected; it sets neither
variable.

These tests pin the gate to the ephemeral test DB instead. They exist because
this was reportedly fixed once before and the fix was not in the tree.

Note on method: pytest re-sets `PYTEST_CURRENT_TEST` around every test phase,
so a non-pytest process cannot be simulated by deleting the variable — the
cases below that stand in for "real process" patch `_is_pytest_runtime`
directly instead.
"""

import pytest

import app as app_module
from app import _is_pytest_runtime, _should_run_precheck


@pytest.fixture
def not_under_pytest(monkeypatch):
    """Stand in for a real (non-pytest) process such as gunicorn or the launcher."""
    monkeypatch.setattr(app_module, "_is_pytest_runtime", lambda: False)
    for var in ("SIMCRICKETX_PRECHECK_RUNNING", "SIMCRICKETX_TEST_MODE",
                "SIMCRICKETX_TEST_DB_URI"):
        monkeypatch.delenv(var, raising=False)


def test_production_runs_precheck(not_under_pytest):
    """Neither variable set — every prod restart migrates."""
    assert _should_run_precheck("") is True


def test_dev_launcher_runs_precheck(not_under_pytest, monkeypatch):
    """TEST_MODE alone, no TEST_DB_URI: the real cricket_sim.db is in use."""
    monkeypatch.setenv("SIMCRICKETX_TEST_MODE", "1")
    assert _should_run_precheck("") is True, (
        "the dev launcher opens the real database, so its schema must be "
        "migrated — gating this on test_mode is what let newly registered "
        "migrations silently never apply on dev"
    )


def test_ephemeral_test_db_skips_precheck(not_under_pytest):
    """pytest's throwaway DB gets its schema from db.create_all instead."""
    assert _should_run_precheck("sqlite:////tmp/pytest_app.db") is False


def test_precheck_cli_does_not_re_enter(not_under_pytest, monkeypatch):
    """migrations/precheck.py's CLI calls run_all itself."""
    monkeypatch.setenv("SIMCRICKETX_PRECHECK_RUNNING", "1")
    assert _should_run_precheck("") is False


def test_pytest_runtime_is_detected_and_skips():
    """Belt and braces: this very run is pytest, so the gate must be closed.

    Unpatched on purpose — it asserts the detector works against the real
    process, which is what makes the skip safe even without a TEST_DB_URI.
    """
    assert _is_pytest_runtime() is True
    assert _should_run_precheck("") is False
