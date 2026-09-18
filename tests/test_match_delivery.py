"""Lost replies and competing transports must not simulate extra deliveries."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from services.match_delivery import MatchDelivery, DeliveryConflict


def test_duplicate_and_late_retry_replay_original_result():
    guard = MatchDelivery()
    first = guard.token
    calls = []
    def advance():
        calls.append(1)
        return {"score": len(calls)}, None
    result, _ = guard.advance(first, advance)
    guard.advance(guard.token, advance)
    assert guard.advance(first, advance)[0] == result
    assert guard.recover(first)["delivery"] == result
    assert len(calls) == 2


def test_socket_and_http_retry_are_serialized():
    guard = MatchDelivery()
    token = guard.token
    entered, release = Event(), Event()
    calls = []
    def advance():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {"score": 1}, None
    with ThreadPoolExecutor(max_workers=2) as pool:
        socket = pool.submit(guard.advance, token, advance)
        assert entered.wait(5)
        http = pool.submit(guard.advance, token, advance)
        release.set()
        assert socket.result() == http.result()
    assert len(calls) == 1


def test_unknown_evicted_and_previous_instance_tokens_never_advance():
    guard = MatchDelivery()
    old = guard.token
    for _ in range(33):
        guard.advance(guard.token, lambda: ({"score": 1}, None))
    for token in (old, "unknown", MatchDelivery().token):
        with pytest.raises(DeliveryConflict):
            guard.advance(token, lambda: pytest.fail("must not advance"))
        with pytest.raises(DeliveryConflict):
            guard.recover(token)


def test_engine_exception_invalidates_token_instead_of_bowling_again():
    guard = MatchDelivery()
    token = guard.token
    def broken():
        raise RuntimeError("failed after mutation")
    with pytest.raises(RuntimeError):
        guard.advance(token, broken)
    with pytest.raises(DeliveryConflict):
        guard.advance(token, lambda: pytest.fail("must not retry"))
    assert guard.recover()["delivery_token"] != token


def test_read_only_recovery_does_not_consume_ready_token():
    guard = MatchDelivery()
    assert guard.recover(guard.token) == guard.recover()


def test_http_recovery_replays_lost_card_and_rejects_stale_instance(app, authenticated_client, regular_user):
    import app as app_module
    mid = 'delivery-recovery-test'
    calls = []
    def advance():
        calls.append(1)
        return {"score": 8, "scorecard_data": {"innings": 1}, "innings_end": True}
    match = SimpleNamespace(data={"created_by": regular_user.id}, is_fc=False, next_ball=advance)
    app_module.MATCH_INSTANCES[mid] = match
    try:
        url = f'/match/{mid}'
        initial = authenticated_client.get(url + '/live-state?delivery=1').get_json()
        token = initial['delivery_token']
        response = authenticated_client.post(url + '/next-ball', json={"delivery_token": token})
        assert response.status_code == 200
        result = response.get_json()
        # The other transport must replay the same response, not bowl again.
        socket = app_module.socketio.test_client(app, flask_test_client=authenticated_client)
        try:
            socket.emit('next_ball', {'match_id': mid, 'delivery_token': token})
            events = socket.get_received()
            assert next(event['args'][0] for event in events if event['name'] == 'ball_result') == result
        finally:
            socket.disconnect()
        assert result['pending_card_id']
        recovered = authenticated_client.get(url + '/live-state?delivery=1&request_id=' + token).get_json()
        assert recovered['delivery'] == result
        assert authenticated_client.post(url + '/next-ball', json={"delivery_token": token}).get_json() == result
        assert len(calls) == 1
        match._delivery_guard = MatchDelivery()  # eviction/rebuild has a fresh epoch
        assert authenticated_client.post(url + '/next-ball', json={"delivery_token": token}).status_code == 409
        assert len(calls) == 1
    finally:
        app_module.MATCH_INSTANCES.pop(mid, None)
