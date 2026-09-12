"""Batched player-pool import (/api/admin/player-pool/import/chunk).

A multi-MB pool file cannot be posted in one request (the proxy answers 413),
so importers send it a few hundred rows at a time. These tests pin the batch
endpoint's contract: row cap, duplicate handling, CSV batches, row numbering
across batches, and admin-only access.
"""

from app import db
from database.models import MasterPlayer
from routes.player_pool_routes import IMPORT_FIELDS, MAX_IMPORT_CHUNK_ROWS

CHUNK_URL = "/api/admin/player-pool/import/chunk"


def _row(name, **overrides):
    row = {
        "name": name,
        "role": "All-rounder",
        "batting_rating": 70,
        "bowling_rating": 60,
        "fielding_rating": 65,
        "list_a_batting_rating": 71,
        "list_a_bowling_rating": 61,
        "list_a_fielding_rating": 66,
        "fc_batting_rating": 72,
        "fc_bowling_rating": 62,
        "fc_fielding_rating": 67,
        "fc_technique_rating": 73,
        "fc_temperament_rating": 74,
        "fc_stamina_rating": 75,
        "batting_hand": "Right",
        "bowling_type": "Medium",
        "bowling_hand": "Right",
        "is_captain": False,
        "is_wicketkeeper": False,
    }
    row.update(overrides)
    return row


def test_batch_imports_rows(admin_client):
    res = admin_client.post(CHUNK_URL, json={"rows": [_row("Batch One"), _row("Batch Two")]})
    assert res.status_code == 200
    body = res.get_json()
    assert (body["ok"], body["imported"], body["updated"], body["skipped"], body["failed"]) == (True, 2, 0, 0, 0)
    assert body["received"] == 2
    assert MasterPlayer.query.filter_by(name="Batch One").one().fc_technique_rating == 73


def test_successive_batches_accumulate(admin_client):
    first = admin_client.post(CHUNK_URL, json={"rows": [_row("Seq A")], "offset": 0})
    second = admin_client.post(CHUNK_URL, json={"rows": [_row("Seq B")], "offset": 1})
    assert first.status_code == second.status_code == 200
    assert MasterPlayer.query.count() == 2


def test_skip_mode_leaves_existing_ratings_untouched(admin_client):
    admin_client.post(CHUNK_URL, json={"rows": [_row("Steady Eddie", batting_rating=40)]})
    res = admin_client.post(CHUNK_URL, json={"rows": [_row("Steady Eddie", batting_rating=99)]})
    body = res.get_json()
    assert (body["imported"], body["skipped"], body["updated"]) == (0, 1, 0)
    assert MasterPlayer.query.filter_by(name="Steady Eddie").one().batting_rating == 40


def test_update_mode_overwrites_existing_ratings(admin_client):
    admin_client.post(CHUNK_URL, json={"rows": [_row("Refreshed Rick", batting_rating=40)]})
    res = admin_client.post(CHUNK_URL, json={
        "rows": [_row("Refreshed Rick", batting_rating=99, fc_stamina_rating=88)],
        "mode": "update",
    })
    body = res.get_json()
    assert (body["imported"], body["skipped"], body["updated"]) == (0, 0, 1)
    player = MasterPlayer.query.filter_by(name="Refreshed Rick").one()
    assert (player.batting_rating, player.fc_stamina_rating) == (99, 88)
    assert MasterPlayer.query.filter_by(name="Refreshed Rick").count() == 1


def test_duplicate_name_inside_one_batch_is_skipped_not_duplicated(admin_client):
    res = admin_client.post(CHUNK_URL, json={"rows": [_row("Twin Tim"), _row("Twin Tim")]})
    body = res.get_json()
    assert (body["imported"], body["skipped"]) == (1, 1)
    assert MasterPlayer.query.filter_by(name="Twin Tim").count() == 1


def test_offset_numbers_errors_against_the_whole_file(admin_client):
    res = admin_client.post(CHUNK_URL, json={
        "rows": [_row("Bad Role", role="Slogger")],
        "offset": 400,
    })
    body = res.get_json()
    assert body["failed"] == 1
    assert body["errors"][0].startswith("Row 401:")
    assert MasterPlayer.query.count() == 0


def test_batch_over_the_row_cap_is_rejected(admin_client):
    rows = [_row(f"Crowd {i}") for i in range(MAX_IMPORT_CHUNK_ROWS + 1)]
    res = admin_client.post(CHUNK_URL, json={"rows": rows})
    assert res.status_code == 400
    body = res.get_json()
    assert body["max_chunk_rows"] == MAX_IMPORT_CHUNK_ROWS
    assert "Send fewer rows per request" in body["error"]
    assert MasterPlayer.query.count() == 0


def test_csv_batch_is_accepted(admin_client):
    row = _row("CSV Carl")
    header = ",".join(IMPORT_FIELDS)
    line = ",".join(str(row[field]).lower() if isinstance(row[field], bool) else str(row[field])
                    for field in IMPORT_FIELDS)
    res = admin_client.post(CHUNK_URL, json={"csv_text": header + "\n" + line})
    assert res.status_code == 200
    body = res.get_json()
    assert (body["imported"], body["received"]) == (1, 1)
    assert MasterPlayer.query.filter_by(name="CSV Carl").one().fc_temperament_rating == 74


def test_bad_request_bodies_are_rejected(admin_client):
    assert admin_client.post(CHUNK_URL, json={"rows": []}).status_code == 400
    assert admin_client.post(CHUNK_URL, json={"rows": "nope"}).status_code == 400
    assert admin_client.post(CHUNK_URL, json={"rows": [_row("X")], "mode": "wipe"}).status_code == 400
    assert admin_client.post(CHUNK_URL, json={"rows": [_row("X")], "offset": -1}).status_code == 400
    assert admin_client.post(CHUNK_URL, json={
        "rows": [_row("X")], "csv_text": "a,b",
    }).status_code == 400
    assert MasterPlayer.query.count() == 0


def test_limits_endpoint_reports_the_cap(admin_client):
    res = admin_client.get("/api/admin/player-pool/import/limits")
    assert res.status_code == 200
    body = res.get_json()
    assert body["max_chunk_rows"] == MAX_IMPORT_CHUNK_ROWS
    assert body["default_chunk_rows"] <= body["max_chunk_rows"]
    assert body["modes"] == ["skip", "update"]
    assert body["fields"] == IMPORT_FIELDS


def test_non_admin_cannot_batch_import(authenticated_client):
    res = authenticated_client.post(CHUNK_URL, json={"rows": [_row("Sneaky Sam")]})
    assert res.status_code == 403
    assert MasterPlayer.query.count() == 0
