"""
CSV ingestion: schema detection, column mapping, and the run flow.

The risk this covers is specific and severe: a reconciliation tool that
mis-reads an amount column produces confident, wrong financial output.
Detection is therefore allowed to be uncertain, but never allowed to be
quietly wrong — anything it cannot place must surface for a human to map.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.ingest.mapper import combine, map_rows
from app.ingest.schema import (
    SourceType, detect_amount_scale, detect_schema, parse_amount, parse_csv, parse_date,
)
from app.ledger import db

GATEWAY_CSV = """payment_id,order_id,amount,fee,tax,net_amount,currency,created_at,settlement_date,status,description
pay_1,INV-9001,12500.00,250.00,45.00,12205.00,INR,2026-03-01,2026-03-03,captured,Cloud platform
pay_2,INV-9002,4300.50,86.01,15.48,4199.01,INR,2026-03-02,2026-03-04,captured,Headphones
"""

BANK_CSV = """Value Date,Narration,Withdrawal,Deposit,Closing Balance,Ref No
2026-03-01,UPI/CLDPLTFRM/ANNUAL,,12500.00,84200.00,UTR8891023
2026-03-02,NEFT PAYMENT TO SOUNDMAX,4300.50,,79899.50,UTR8891044
"""

ORDERS_CSV = """order_id,invoice_ref,amount,currency,order_date,description,status
ORD-1,INV-9001,12500.00,INR,2026-03-01,Cloud platform annual,captured
ORD-2,INV-9002,4300.50,INR,2026-03-02,SoundMax headphones,captured
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ingest.db")
    db._local.__dict__.clear()
    db.init_db()
    from app.main import app
    with TestClient(app) as c:
        yield c
    db._local.__dict__.clear()


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1234.56", 1234.56), ("1,234.56", 1234.56), ("₹1,234.56", 1234.56),
    ("(45.00)", -45.0), ("-45.00", -45.0), ("0", 0.0), ("", None), ("abc", None), (None, None),
])
def test_amounts_parse_from_the_forms_exports_actually_use(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", [
    "2026-03-01", "2026/03/01", "01-03-2026", "01/03/2026", "01-Mar-2026",
    "2026-03-01 14:30:00", "2026-03-01T14:30:00Z",
])
def test_dates_parse_from_common_export_formats(raw):
    assert parse_date(raw) is not None


def test_an_unparseable_date_is_none_rather_than_a_guess():
    assert parse_date("last tuesday") is None
    assert parse_date("") is None


def test_amount_scale_distinguishes_paise_from_rupees():
    """Guessing this wrong is a 100x error, so whole numbers with no
    decimal anywhere are the only thing read as minor units."""
    assert detect_amount_scale(["1250000", "430050", "89900"]) == "minor"
    assert detect_amount_scale(["12500.00", "4300.50"]) == "major"
    assert detect_amount_scale(["12500", "4300.50"]) == "major"


# ---------------------------------------------------------------------------
# Schema detection
# ---------------------------------------------------------------------------

def test_a_gateway_export_is_detected_without_help():
    columns, rows = parse_csv(GATEWAY_CSV)
    schema = detect_schema(columns, rows)
    assert schema.mapping["transaction_id"] == "payment_id"
    assert schema.mapping["reference"] == "order_id"
    assert schema.mapping["amount"] == "amount"
    assert schema.mapping["net_amount"] == "net_amount"
    assert schema.unmapped_required == []


def test_an_order_book_does_not_let_its_own_key_claim_the_reference_slot():
    """The bug this pins: `order_id` outbid `invoice_ref` for the reference
    slot purely by column order, so the ledger referenced ORD-1 while the
    gateway referenced INV-9001 and every record came back missing."""
    columns, rows = parse_csv(ORDERS_CSV)
    schema = detect_schema(columns, rows)
    assert schema.mapping["reference"] == "invoice_ref"
    assert schema.mapping["transaction_id"] == "order_id"


def test_a_bank_statement_with_split_debit_credit_columns_is_understood():
    columns, rows = parse_csv(BANK_CSV)
    schema = detect_schema(columns, rows)
    assert schema.debit_column == "Withdrawal"
    assert schema.credit_column == "Deposit"
    assert schema.mapping["description"] == "Narration"
    assert schema.mapping["reference"] == "Ref No"
    assert schema.unmapped_required == [], "a value date is a transaction date on a bank line"


def test_detection_reports_confidence_and_a_reason_per_column():
    columns, rows = parse_csv(GATEWAY_CSV)
    schema = detect_schema(columns, rows)
    for guess in schema.guesses:
        assert guess.reason, f"{guess.column} has no stated reason"
        if guess.canonical:
            assert 0 < guess.confidence <= 1


def test_an_unrecognisable_file_asks_instead_of_guessing():
    columns, rows = parse_csv("alpha,beta,gamma\nfoo,bar,baz\nqux,quux,corge\n")
    schema = detect_schema(columns, rows)
    assert schema.needs_user_input
    assert "amount" in schema.unmapped_required
    assert "date" in schema.unmapped_required


def test_content_rescues_a_required_field_the_headers_missed():
    columns, rows = parse_csv("ref,thing,when\nA1,1200.50,2026-03-01\nA2,90.00,2026-03-02\n")
    schema = detect_schema(columns, rows)
    assert schema.mapping.get("amount") == "thing"
    assert schema.mapping.get("date") == "when"


def test_a_semicolon_delimited_file_is_handled():
    columns, _ = parse_csv("amount;date;description\n10.00;2026-03-01;test\n")
    assert columns == ["amount", "date", "description"]


def test_a_byte_order_mark_does_not_corrupt_the_first_header():
    columns, _ = parse_csv("﻿amount,date\n10.00,2026-03-01\n")
    assert columns[0] == "amount"


# ---------------------------------------------------------------------------
# Mapping to canonical records
# ---------------------------------------------------------------------------

def test_a_gateway_row_becomes_a_settlement_record():
    columns, rows = parse_csv(GATEWAY_CSV)
    schema = detect_schema(columns, rows)
    mapped = map_rows(rows, schema.mapping, SourceType.PAYMENT_GATEWAY, "s1", schema.amount_scale)
    assert mapped.role == "SETTLEMENT"
    assert len(mapped.settlement_records) == 2
    first = mapped.settlement_records[0]
    assert first.gross_amount_minor == 1250000
    assert first.fee_minor == 25000
    assert first.order_reference == "INV-9001"


def test_a_stated_net_is_kept_rather_than_recomputed():
    """Deriving net from gross minus fee and tax makes the arithmetic
    check verify a subtraction the mapper just performed, so a payout file
    that genuinely does not add up could never fail."""
    csv = ("payment_id,order_id,amount,fee,tax,net_amount,created_at,status\n"
           "pay_x,INV-1,10000.00,200.00,36.00,6000.00,2026-03-01,captured\n")
    columns, rows = parse_csv(csv)
    schema = detect_schema(columns, rows)
    mapped = map_rows(rows, schema.mapping, SourceType.PAYMENT_GATEWAY, "s1", schema.amount_scale)
    assert mapped.settlement_records[0].net_amount_minor == 600000


def test_a_bank_debit_becomes_an_outflow_not_a_rejected_negative():
    columns, rows = parse_csv(BANK_CSV)
    schema = detect_schema(columns, rows)
    mapped = map_rows(rows, schema.mapping, SourceType.BANK_STATEMENT, "b1",
                      schema.amount_scale, schema.debit_column, schema.credit_column)
    assert len(mapped.settlement_records) == 2
    assert mapped.rejected == []
    withdrawal = next(r for r in mapped.settlement_records if r.payment_id == "UTR8891044")
    assert withdrawal.gross_amount_minor == 430050
    assert withdrawal.refund_amount_minor == 430050, "money out is recorded as a reversal"


def test_a_malformed_row_is_rejected_individually_and_reported():
    csv = ("amount,date,description\n"
           "100.00,2026-03-01,fine\n"
           "not-a-number,2026-03-02,broken\n"
           "300.00,not-a-date,also broken\n"
           "400.00,2026-03-04,fine\n")
    columns, rows = parse_csv(csv)
    schema = detect_schema(columns, rows)
    mapped = map_rows(rows, schema.mapping, SourceType.ORDERS, "s1", schema.amount_scale)
    assert mapped.accepted_count == 2, "good rows must survive a bad neighbour"
    assert len(mapped.rejected) == 2
    assert all("row" in r and "error" in r for r in mapped.rejected)


def test_sources_combine_into_two_sides():
    _, gateway_rows = parse_csv(GATEWAY_CSV)
    _, order_rows = parse_csv(ORDERS_CSV)
    gw_schema = detect_schema(*parse_csv(GATEWAY_CSV))
    ord_schema = detect_schema(*parse_csv(ORDERS_CSV))
    ledger, settlements, rejected = combine([
        map_rows(order_rows, ord_schema.mapping, SourceType.ORDERS, "o1", ord_schema.amount_scale),
        map_rows(gateway_rows, gw_schema.mapping, SourceType.PAYMENT_GATEWAY, "g1", gw_schema.amount_scale),
    ])
    assert len(ledger) == 2
    assert len(settlements) == 2
    assert rejected == []


# ---------------------------------------------------------------------------
# The run API
# ---------------------------------------------------------------------------

def _wait(client, run_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/runs/{run_id}").json()
        if run["status"] == "COMPLETED":
            return run
        time.sleep(0.05)
    raise AssertionError("run did not complete")


def test_a_run_reconciles_uploaded_csvs_end_to_end(client):
    run_id = client.post("/runs", json={"label": "test"}).json()["run_id"]
    client.post(f"/runs/{run_id}/sources",
                files={"file": ("orders.csv", ORDERS_CSV.encode(), "text/csv")},
                data={"source_type": "ORDERS"})
    client.post(f"/runs/{run_id}/sources",
                files={"file": ("gw.csv", GATEWAY_CSV.encode(), "text/csv")},
                data={"source_type": "PAYMENT_GATEWAY"})

    started = client.post(f"/runs/{run_id}/execute", json={})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["ledger_records"] == 2 and body["settlement_records"] == 2

    run = _wait(client, run_id)
    assert run["outcome_counts"].get("RECONCILED") == 2, \
        "matching references and amounts should reconcile without a model"


def test_a_run_refuses_to_execute_with_only_one_side(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    client.post(f"/runs/{run_id}/sources",
                files={"file": ("orders.csv", ORDERS_CSV.encode(), "text/csv")},
                data={"source_type": "ORDERS"})
    response = client.post(f"/runs/{run_id}/execute", json={})
    assert response.status_code == 400
    assert "settlement" in response.json()["detail"].lower()


def test_a_run_refuses_to_execute_while_a_required_column_is_unmapped(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    client.post(f"/runs/{run_id}/sources",
                files={"file": ("mystery.csv", b"alpha,beta\nfoo,bar\n", "text/csv")},
                data={"source_type": "ORDERS"})
    client.post(f"/runs/{run_id}/sources",
                files={"file": ("gw.csv", GATEWAY_CSV.encode(), "text/csv")},
                data={"source_type": "PAYMENT_GATEWAY"})
    response = client.post(f"/runs/{run_id}/execute", json={})
    assert response.status_code == 400
    assert "map the required columns" in response.json()["detail"]


def test_a_user_can_correct_a_detected_mapping(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    upload = client.post(f"/runs/{run_id}/sources",
                         files={"file": ("orders.csv", ORDERS_CSV.encode(), "text/csv")},
                         data={"source_type": "ORDERS"}).json()
    updated = client.put(
        f"/runs/{run_id}/sources/{upload['source_id']}/mapping",
        json={"mapping": {**upload["mapping"], "reference": "order_id"}},
    )
    assert updated.status_code == 200
    assert updated.json()["mapping"]["reference"] == "order_id"


def test_mapping_to_a_column_that_does_not_exist_is_rejected(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    upload = client.post(f"/runs/{run_id}/sources",
                         files={"file": ("orders.csv", ORDERS_CSV.encode(), "text/csv")},
                         data={"source_type": "ORDERS"}).json()
    response = client.put(
        f"/runs/{run_id}/sources/{upload['source_id']}/mapping",
        json={"mapping": {"amount": "no_such_column"}},
    )
    assert response.status_code == 400


def test_an_empty_or_headerless_file_is_rejected_with_a_reason(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    empty = client.post(f"/runs/{run_id}/sources",
                        files={"file": ("empty.csv", b"", "text/csv")},
                        data={"source_type": "ORDERS"})
    assert empty.status_code == 400
    headers_only = client.post(f"/runs/{run_id}/sources",
                               files={"file": ("h.csv", b"amount,date\n", "text/csv")},
                               data={"source_type": "ORDERS"})
    assert headers_only.status_code == 400
    assert "no rows" in headers_only.json()["detail"]


def test_an_unknown_source_type_is_rejected(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    response = client.post(f"/runs/{run_id}/sources",
                           files={"file": ("o.csv", ORDERS_CSV.encode(), "text/csv")},
                           data={"source_type": "TELEPATHY"})
    assert response.status_code == 400


def test_runs_are_isolated_from_each_other(client):
    """Two runs over the same records must not share results — re-running
    reconciliation is routine, and the earlier run has to stay intact."""
    ids = []
    for _ in range(2):
        run_id = client.post("/runs", json={}).json()["run_id"]
        client.post(f"/runs/{run_id}/sources",
                    files={"file": ("orders.csv", ORDERS_CSV.encode(), "text/csv")},
                    data={"source_type": "ORDERS"})
        client.post(f"/runs/{run_id}/sources",
                    files={"file": ("gw.csv", GATEWAY_CSV.encode(), "text/csv")},
                    data={"source_type": "PAYMENT_GATEWAY"})
        client.post(f"/runs/{run_id}/execute", json={})
        _wait(client, run_id)
        ids.append(run_id)

    for run_id in ids:
        records = client.get(f"/batch/{run_id}/records").json()
        assert len(records) == 2, f"{run_id} lost its records to the other run"


def test_export_returns_csv_with_the_decision_evidence(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    client.post(f"/runs/{run_id}/sources",
                files={"file": ("orders.csv", ORDERS_CSV.encode(), "text/csv")},
                data={"source_type": "ORDERS"})
    client.post(f"/runs/{run_id}/sources",
                files={"file": ("gw.csv", GATEWAY_CSV.encode(), "text/csv")},
                data={"source_type": "PAYMENT_GATEWAY"})
    client.post(f"/runs/{run_id}/execute", json={})
    _wait(client, run_id)

    response = client.get(f"/runs/{run_id}/export")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("record_id,outcome,exception_type")
    assert len(lines) == 3, "header plus one row per record"


def test_upload_and_execution_are_written_to_the_audit_ledger(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    client.post(f"/runs/{run_id}/sources",
                files={"file": ("orders.csv", ORDERS_CSV.encode(), "text/csv")},
                data={"source_type": "ORDERS"})
    events = client.get("/audit/log", params={"limit": 50}).json()["events"]
    kinds = {e["event_type"] for e in events}
    assert {"RUN_CREATED", "SOURCE_UPLOADED"} <= kinds
    assert client.get("/audit/verify").json()["intact"] is True
