"""Real request boundaries over shared QA identities and a controllable clock."""
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts.dev_test_accounts import seed, logged_in_client, ACCOUNTS
from services.match_store import INCOMPLETE_IDLE_SECONDS


@pytest.fixture
def context(app):
    seed(app)
    store = app.extensions['match_store']
    clock = [0.0]
    store._clock = lambda: clock[0]
    owner = ACCOUNTS['user1']['email']
    match_id = str(uuid4())
    match = SimpleNamespace(data={'match_id': match_id, 'created_by': owner,
        'current_state': 'in_progress', 'match_format': 'T20'}, is_fc=False, is_hundred=False)
    with store.acquire(match_id, loader=lambda: match):
        pass
    return SimpleNamespace(app=app, store=store, clock=clock, match=match, mid=match_id,
        client=logged_in_client(app, 'user1'), stranger=logged_in_client(app, 'user2'))


@pytest.mark.parametrize('fail', [False, True])
def test_image_upload_is_protected_until_save_returns_or_raises(context, monkeypatch, fail):
    from werkzeug.datastructures import FileStorage
    ctx = context
    ctx.clock[0] = INCOMPLETE_IDLE_SECONDS + 1
    def save(self, destination, *args, **kwargs):
        outcome = ctx.store.sweep('incomplete')
        assert outcome['removed'] == 0 and outcome['protected'] == 1
        if fail:
            raise OSError('injected image write failure')
    monkeypatch.setattr(FileStorage, 'save', save)
    response = ctx.client.post(f'/match/{ctx.mid}/save-scorecard-images', data={
        'first_innings_image': (io.BytesIO(b'image'), 'card.png', 'image/png')})
    assert response.status_code == (500 if fail else 200)
    assert ctx.store.metrics()['protected'] == 0
    # Uploads do not refresh gameplay inactivity.
    assert ctx.store.sweep('incomplete')['removed'] == 1


def test_commentary_and_admin_inspection_do_not_touch_gameplay(context):
    ctx = context
    ctx.clock[0] = 300
    response = ctx.client.post(f'/match/{ctx.mid}/save-commentary',
        json={'commentary_html': '<div class="code-line">QA commentary</div>'})
    assert response.status_code == 200
    assert ctx.match.frontend_commentary_html
    before = ctx.store.describe(ctx.mid)['access_age_seconds']
    admin = logged_in_client(ctx.app, 'admin')
    assert admin.get('/admin/health').status_code == 200
    assert ctx.store.describe(ctx.mid)['access_age_seconds'] == before == 300
    assert ctx.store.metrics()['protected'] == 0


def test_unauthorized_upload_releases_lease_without_touching(context):
    ctx = context
    ctx.clock[0] = 50
    response = ctx.stranger.post(f'/match/{ctx.mid}/save-commentary',
        json={'commentary_html': '<div>not yours</div>'})
    assert response.status_code == 403
    assert not hasattr(ctx.match, 'frontend_commentary_html')
    assert ctx.store.describe(ctx.mid)['access_age_seconds'] == 50
    assert ctx.store.metrics()['protected'] == 0


def test_archive_build_uses_lease_and_records_failure(context, monkeypatch):
    import match_archiver
    ctx = context
    ctx.match.data['current_state'] = 'completed'
    with ctx.store.acquire(ctx.mid) as lease:
        ctx.store.mark_completed(lease)
    ctx.clock[0] = INCOMPLETE_IDLE_SECONDS * 2
    class Archiver:
        folder_name = 'qa-not-written'
        def __init__(self, metadata, instance):
            assert instance is ctx.match
        def create_archive(self, **kwargs):
            assert ctx.store.describe(ctx.mid)['protected'] >= 1
            assert ctx.store.sweep('incomplete')['removed'] == 0
            assert ctx.store.sweep('completed')['protected'] == 1
            return False
    monkeypatch.setattr(match_archiver, 'MatchArchiver', Archiver)
    response = ctx.client.post(f'/match/{ctx.mid}/download-archive', json={})
    assert response.status_code == 500
    state = ctx.store.describe(ctx.mid)
    assert state['archive_status'] == 'failed'
    assert state['protected'] == 0
    assert ctx.store.sweep('completed')['pending'] == 1


def test_both_transports_hold_scope_through_engine_call(context):
    import app as app_module
    ctx = context
    calls = []
    def advance():
        calls.append(1)
        # Advance fake time during work; cleanup must not remove this match.
        ctx.clock[0] += INCOMPLETE_IDLE_SECONDS + 1
        assert ctx.store.sweep('incomplete')['protected'] == 1
        return {'error': 'QA no-delivery', 'score': 0}
    ctx.match.next_ball = advance
    assert ctx.client.post(f'/match/{ctx.mid}/next-ball').status_code == 200
    socket = app_module.socketio.test_client(ctx.app, flask_test_client=ctx.client)
    try:
        socket.emit('next_ball', {'match_id': ctx.mid})
        assert any(event['name'] == 'ball_result' for event in socket.get_received())
    finally:
        socket.disconnect()
    assert len(calls) == 2
    assert ctx.store.metrics()['protected'] == 0


def test_invalidation_rejects_all_match_operations_without_resurrection(context):
    ctx = context
    ctx.store.invalidate(ctx.mid)
    for suffix, payload in [('next-ball', {}), ('save-commentary', {'commentary_html': '<div>QA</div>'}),
                            ('download-archive', {}), ('save-scorecard-images', {})]:
        assert ctx.client.post(f'/match/{ctx.mid}/{suffix}', json=payload).status_code == 410
    assert ctx.store.metrics()['total'] == 0


def test_cleanup_does_not_delete_recoverable_cached_or_completed_snapshots(context, tmp_path, monkeypatch):
    import app as app_module
    ctx = context
    directory = tmp_path / 'data/matches'
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app_module, 'PROJECT_ROOT', str(tmp_path))
    for mid, state in ((ctx.mid, 'in_progress'), ('complete', 'completed'), ('orphan', 'in_progress')):
        path = directory / f'match_{mid}.json'
        path.write_text(json.dumps({'match_id': mid, 'created_by': ACCOUNTS['user1']['email'], 'current_state': state}))
        os.utime(path, (1, 1))
    ctx.clock[0] = INCOMPLETE_IDLE_SECONDS + 1
    with ctx.store.acquire(ctx.mid):
        app_module.cleanup_old_match_instances(ctx.app)
        assert (directory / f'match_{ctx.mid}.json').exists()
    assert (directory / 'match_complete.json').exists()
    assert not (directory / 'match_orphan.json').exists()
    assert ctx.store.metrics()['total'] == 1


@pytest.mark.parametrize('extra,status', [({'current_state': 'completed'}, 409),
    ({'tournament_id': 999999, 'fixture_id': 999999}, 410)])
def test_stale_setup_json_is_not_reconstructed_as_a_new_simulation(context, extra, status):
    import app as app_module
    ctx = context
    ctx.store.discard(ctx.mid)
    path = Path(app_module.PROJECT_ROOT) / 'data/matches' / f'match_{ctx.mid}.json'
    path.write_text(json.dumps(dict(ctx.match.data, **extra)))
    try:
        response = ctx.client.post(f'/match/{ctx.mid}/next-ball')
        assert response.status_code == status
        assert ctx.store.describe(ctx.mid) is None
    finally:
        path.unlink(missing_ok=True)


def test_failed_result_persistence_is_visible_and_retained(context, monkeypatch):
    import app as app_module
    ctx = context
    ctx.match.innings = 2
    ctx.match.score = 1
    ctx.match.wickets = 0
    ctx.match.result = 'QA finished'
    ctx.match.get_final_live_board = lambda: {}
    ctx.match.next_ball = lambda: {'match_over': True, 'result': 'QA finished'}
    class FailedArchiver:
        def __init__(self, *args):
            pass
        def _save_to_database(self):
            return False
    monkeypatch.setattr(app_module, 'MatchArchiver', FailedArchiver)
    assert ctx.client.post(f'/match/{ctx.mid}/next-ball').status_code == 200
    state = ctx.store.describe(ctx.mid)
    assert state['completed'] and state['result_status'] == 'failed'
    assert state['result_error']
    ctx.clock[0] += INCOMPLETE_IDLE_SECONDS * 2
    assert ctx.store.sweep('incomplete')['removed'] == 0
    assert ctx.store.sweep('completed')['pending'] == 1
