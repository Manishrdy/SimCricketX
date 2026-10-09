"""Opt-in shared QA identities for legacy regression fixtures.

Use `pytest -p tests.qa_accounts_plugin ...` against pytest's isolated DB.
No passwords are changed or submitted. Ordinary test runs are unaffected.
"""
import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(fixturedef, request):
    keys = {'regular_user': 'user1', 'admin_user': 'admin',
            'authenticated_client': 'user1', 'admin_client': 'admin'}
    key = keys.get(fixturedef.argname)
    if not key:
        return None
    app = request.getfixturevalue('app')
    from scripts.dev_test_accounts import seed, logged_in_client, ACCOUNTS
    from database import db
    from database.models import User
    if db.session.get(User, ACCOUNTS[key]['email']) is None:
        seed(app)
    result = (logged_in_client(app, key) if fixturedef.argname.endswith('client')
              else db.session.get(User, ACCOUNTS[key]['email']))
    fixturedef.cached_result = (result, fixturedef.cache_key(request), None)
    return result
