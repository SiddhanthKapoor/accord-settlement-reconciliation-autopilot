"""
Export, and the review queue's scope.

Four user-reported defects meet here. Reconciliation output is only
useful once it leaves the screen, and finance opens spreadsheets — so the
export has to be findable, has to come in both the formats people
actually use, and has to carry the evidence rather than a list of
verdicts. And a queue whose header counts 76 while its list holds 50 is
two true numbers that read as a broken product; the response has to
describe its own page so the screen can state the scope.
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.ledger import db

ORDERS_CSV = """order_id,invoice_ref,amount,currency,order_date,description,status
ORD-1,INV-9001,12500.00,INR,2026-03-01,Cloud platform annual,captured
ORD-2,INV-9002,4300.50,INR,2026-03-02,SoundMax headphones,captured
ORD-3,INV-9003,9100.00,INR,2026-03-03,Studio monitor,captured
"""

GATEWAY_CSV = """payment_id,order_id,amount,fee,tax,net_amount,currency,created_at,settlement_date,status,description
pay_1,INV-9001,12500.00,250.00,45.00,12205.00,INR,2026-03-01,2026-03-03,captured,Cloud platform
pay_2,INV-9002,4999.00,86.01,15.48,4897.51,INR,2026-03-02,2026-03-04,captured,Headphones
"""

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "export_test.db")
    db._local.__dict__.clear()
    db.init_db()
    from app.main import app
    with TestClient(app) as c:
        yield c
    db._local.__dict__.clear()


def _wait(client, run_id, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if client.get(f"/runs/{run_id}").json().get("status") == "COMPLETED":
            return
        time.sleep(0.05)
    raise AssertionError("run did not complete")


def _executed_run(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    client.post(f"/runs/{run_id}/sources",
                files={"file": ("orders.csv", ORDERS_CSV.encode(), "text/csv")},
                data={"source_type": "ORDERS"})
    client.post(f"/runs/{run_id}/sources",
                files={"file": ("gw.csv", GATEWAY_CSV.encode(), "text/csv")},
                data={"source_type": "PAYMENT_GATEWAY"})
    client.post(f"/runs/{run_id}/execute", json={})
    _wait(client, run_id)
    return run_id


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def test_export_defaults_to_csv_and_carries_the_reasoning(client):
    run_id = _executed_run(client)
    response = client.get(f"/runs/{run_id}/export")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    header = response.text.splitlines()[0]
    for column in ("reason", "explanation", "ledger_source_file", "settlement_source_file"):
        assert column in header, f"{column} missing — the evidence is the point of the export"


def test_export_as_xlsx_is_a_real_workbook(client):
    openpyxl = pytest.importorskip("openpyxl")
    run_id = _executed_run(client)
    response = client.get(f"/runs/{run_id}/export", params={"format": "xlsx"})
    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX_MEDIA
    assert response.headers["content-disposition"].endswith('.xlsx"')

    sheet = openpyxl.load_workbook(io.BytesIO(response.content)).active
    assert sheet.freeze_panes == "A2", "the header must stay put while an operator scrolls"
    assert [c.value for c in sheet[1]][:2] == ["record_id", "outcome"]
    assert sheet.max_row == 4, "header plus one row per record"
    # Every column is given a usable width rather than Excel's default.
    assert all(
        sheet.column_dimensions[chr(ord("A") + i)].width >= 10
        for i in range(min(sheet.max_column, 26))
    )


def test_both_export_formats_describe_the_same_records(client):
    openpyxl = pytest.importorskip("openpyxl")
    run_id = _executed_run(client)
    csv_lines = client.get(f"/runs/{run_id}/export").text.strip().splitlines()
    sheet = openpyxl.load_workbook(
        io.BytesIO(client.get(f"/runs/{run_id}/export", params={"format": "xlsx"}).content)
    ).active
    assert sheet.max_row == len(csv_lines)
    assert [c.value for c in sheet[1]] == csv_lines[0].split(",")


def test_export_respects_the_outcome_filter_on_screen(client):
    run_id = _executed_run(client)
    counts = client.get(f"/runs/{run_id}").json()["outcome_counts"]
    for outcome, expected in counts.items():
        response = client.get(f"/runs/{run_id}/export", params={"outcome": outcome})
        rows = response.text.strip().splitlines()[1:]
        assert len(rows) == expected, f"{outcome} export does not match what the screen shows"
        assert outcome.lower().replace("_", "-") in response.headers["content-disposition"]


def test_an_unknown_export_format_is_refused_rather_than_guessed(client):
    run_id = _executed_run(client)
    response = client.get(f"/runs/{run_id}/export", params={"format": "pdf"})
    assert response.status_code == 400
    assert "csv" in response.json()["detail"] and "xlsx" in response.json()["detail"]


def test_export_of_a_run_that_does_not_exist_is_a_404(client):
    assert client.get("/runs/run_nope/export").status_code == 404


# ---------------------------------------------------------------------------
# The review queue states its own scope
# ---------------------------------------------------------------------------

def test_queue_reports_the_page_it_returned_against_the_whole_open_set(client):
    _executed_run(client)
    full = client.get("/review/queue").json()
    total = full["total"]
    if total == 0:
        pytest.skip("this fixture produced no escalations to page through")

    assert full["returned"] == len(full["items"]) == total
    assert full["summary"]["open_count"] == total

    page = client.get("/review/queue", params={"limit": 1}).json()
    assert page["returned"] == 1
    assert page["limit"] == 1
    # The number the header shows and the number the list holds are both
    # returned, so the screen can never present them as a contradiction.
    assert page["total"] == total


def test_queue_export_is_the_whole_queue_not_the_page(client):
    _executed_run(client)
    queue = client.get("/review/queue").json()
    if queue["total"] == 0:
        pytest.skip("this fixture produced no escalations to export")

    response = client.get("/review/queue/export")
    assert response.status_code == 200
    rows = response.text.strip().splitlines()
    assert len(rows) == queue["total"] + 1
    header = rows[0]
    for column in ("reason", "explanation", "recommended_action", "available_actions",
                   "ledger_source_file"):
        assert column in header

    workbook = client.get("/review/queue/export", params={"format": "xlsx"})
    assert workbook.headers["content-type"] == XLSX_MEDIA


def test_an_actioned_record_leaves_both_the_count_and_the_list(client):
    _executed_run(client)
    before = client.get("/review/queue").json()
    if not before["items"]:
        pytest.skip("this fixture produced no escalations to action")

    item = before["items"][0]
    action = item["available_actions"][0]["action"]
    accepted = client.post(f"/review/{item['record_id']}/action",
                           json={"batch_id": before["batch_id"], "action": action})
    assert accepted.status_code == 200

    after = client.get("/review/queue").json()
    assert after["total"] == before["total"] - 1
    assert after["returned"] == before["returned"] - 1
    assert after["summary"]["open_count"] == after["total"]
    assert all(i["record_id"] != item["record_id"] for i in after["items"])


def test_a_draft_workspace_does_not_hijack_the_queue(client):
    """Dropping a file on the upload screen must not empty the queue.

    A draft run is newer than the executed one, and picking the newest
    batch row reported "nothing waiting on a person" about a workspace
    that had never run — while the real run's queue sat untouched behind
    it.
    """
    run_id = _executed_run(client)
    before = client.get("/review/queue").json()

    draft = client.post("/runs", json={"label": "just dropped a file"}).json()["run_id"]
    client.post(f"/runs/{draft}/sources",
                files={"file": ("orders.csv", ORDERS_CSV.encode(), "text/csv")},
                data={"source_type": "ORDERS"})

    after = client.get("/review/queue").json()
    assert after["batch_id"] == before["batch_id"] == run_id
    assert after["total"] == before["total"]
