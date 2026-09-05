"""
Many files, of many kinds, from many systems — the workspace case.

The engine has always folded any number of sources into two sides; what
did not exist was a way to hand it a folder of month-end exports without
telling it what each one was. These tests cover that path, and they
weight heavily towards the ways it could go wrong quietly:

  * a file classified into the wrong role puts a ledger on both sides of
    the reconciliation and every record comes back clean,
  * two files with the same amount and nothing else in common must not
    be allowed to satisfy each other,
  * a record whose provenance is lost cannot be checked by the person
    who has to answer for it.

Files are constructed here rather than fixtured, and driven through the
real API, because the failure modes live in the boundary between reading
a file and deciding what it is.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.ingest.classify import classify_source
from app.ingest.mapper import combine, combine_provenance, map_rows
from app.ingest.reader import UnreadableFile, read_table
from app.ingest.schema import SourceType, detect_schema, parse_csv
from app.ledger import db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # These tests are about reading, classifying and routing files, and
    # they run with the offline heuristic verifier so their outcomes are
    # a property of the code rather than of a model's mood or a network.
    # The AI path has its own tests.
    monkeypatch.setenv("ACCORD_AI_DISABLED", "1")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "multifile.db")
    db._local.__dict__.clear()
    db.init_db()
    from app.main import app
    with TestClient(app) as c:
        yield c
    db._local.__dict__.clear()


# ---------------------------------------------------------------------------
# File builders — every amount is unique unless a test wants a collision
# ---------------------------------------------------------------------------

def _csv(header: list[str], rows: list[list]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode()


def orders_file(tag: str, n: int, *, amount_base: float) -> bytes:
    header = ["order_id", "invoice_ref", "amount", "currency", "order_date",
              "customer_email", "sku", "quantity", "description", "status"]
    rows = [[f"ORD-{tag}-{i}", f"INV-{tag}-{i}", f"{amount_base + i * 1.13:.2f}", "INR",
             f"2026-03-{(i % 27) + 1:02d}", f"buyer{i}@example.com", f"SKU-{i}", 1,
             f"Order {tag} {i}", "captured"] for i in range(1, n + 1)]
    return _csv(header, rows)


def gateway_file(tag: str, n: int, *, amount_base: float, fee_rate: float = 0.02) -> bytes:
    header = ["payment_id", "order_id", "settlement_id", "amount", "fee", "tax", "net_amount",
              "currency", "created_at", "settlement_date", "status", "description"]
    rows = []
    for i in range(1, n + 1):
        gross = amount_base + i * 1.13
        fee = round(gross * fee_rate, 2)
        tax = round(fee * 0.18, 2)
        rows.append([f"pay_{tag}{i:04d}", f"INV-{tag}-{i}", f"setl_{tag}", f"{gross:.2f}",
                     f"{fee:.2f}", f"{tax:.2f}", f"{gross - fee - tax:.2f}", "INR",
                     f"2026-03-{(i % 27) + 1:02d}", f"2026-03-{(i % 27) + 2:02d}",
                     "captured", f"Payout {tag} {i}"])
    return _csv(header, rows)


def bank_file(tag: str, n: int, *, amount_base: float) -> bytes:
    header = ["Value Date", "Narration", "Withdrawal", "Deposit", "Closing Balance", "Ref No"]
    rows = []
    balance = 500000.0
    for i in range(1, n + 1):
        credit = amount_base + i * 1.13
        balance += credit
        rows.append([f"2026-03-{(i % 27) + 1:02d}", f"NEFT/{tag}/CR{i}", "", f"{credit:.2f}",
                     f"{balance:.2f}", f"UTR{tag}{i:05d}"])
    return _csv(header, rows)


def accounting_file(tag: str, n: int, *, amount_base: float) -> bytes:
    header = ["Date", "Voucher No", "Voucher Type", "Ledger Name", "Particulars", "Debit", "Credit"]
    rows = [[f"2026-03-{(i % 27) + 1:02d}", f"V-{tag}-{i}", "Sales", "Revenue",
             f"Being sale {tag} {i}", "0.00", f"{amount_base + i * 1.13:.2f}"]
            for i in range(1, n + 1)]
    return _csv(header, rows)


def upload(client, run_id, files, *, source_type=None, source_types=None):
    """`files` is a list of (filename, bytes)."""
    payload = [("files", (name, content, "text/csv")) for name, content in files]
    data = {}
    if source_type:
        data["source_type"] = source_type
    if source_types:
        data["source_types"] = source_types
    return client.post(f"/runs/{run_id}/sources", files=payload, data=data)


def wait_for(client, run_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/runs/{run_id}").json()
        if run["status"] == "COMPLETED":
            return run
        time.sleep(0.05)
    raise AssertionError("run did not complete in time")


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

def _write_xlsx(path: Path, rows: list[list], sheet_title="Sheet1"):
    import openpyxl
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = sheet_title
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return path.read_bytes()


def test_an_xlsx_reads_into_the_same_shape_as_a_csv(tmp_path):
    raw = _write_xlsx(tmp_path / "gw.xlsx", [
        ["payment_id", "order_id", "amount", "currency", "created_at", "status"],
        ["pay_1", "INV-1", 12500.0, "INR", "2026-03-01", "captured"],
        ["pay_2", "INV-2", 4300.5, "INR", "2026-03-02", "captured"],
    ])
    result = read_table("gw.xlsx", raw)
    assert result.fmt == "xlsx"
    assert result.columns == ["payment_id", "order_id", "amount", "currency", "created_at", "status"]
    assert len(result.rows) == 2
    schema = detect_schema(result.columns, result.rows)
    assert schema.unmapped_required == []
    assert schema.amount_scale == "major", \
        "a float amount must keep its decimal point or paise detection inverts it"


def test_an_xlsx_whose_header_is_not_on_the_first_row_is_still_understood(tmp_path):
    """Bank and ERP exports open with a title block. Reading row 1 as the
    header names every column wrong and loses the whole file."""
    raw = _write_xlsx(tmp_path / "stmt.xlsx", [
        ["ACME TRADING PRIVATE LIMITED"],
        [],
        ["Statement of account 01-Mar-2026 to 31-Mar-2026"],
        ["Value Date", "Narration", "Withdrawal", "Deposit", "Closing Balance", "Ref No"],
        ["2026-03-01", "UPI/ACME/CR", None, 12500.0, 512500.0, "UTR900001"],
        ["2026-03-02", "NEFT/VENDOR/DR", 4300.5, None, 508199.5, "UTR900002"],
    ])
    result = read_table("stmt.xlsx", raw)
    assert result.header_row == 4, f"header found on row {result.header_row}"
    assert result.columns[:2] == ["Value Date", "Narration"]
    assert len(result.rows) == 2
    schema = detect_schema(result.columns, result.rows)
    assert schema.debit_column == "Withdrawal" and schema.credit_column == "Deposit"


def test_a_csv_renamed_xlsx_is_read_as_the_csv_it_actually_is():
    raw = b"amount,date\n100.00,2026-03-01\n"
    result = read_table("settlements.xlsx", raw)
    assert result.fmt == "csv"
    assert any("not a workbook" in note for note in result.notes)
    assert result.rows == [{"amount": "100.00", "date": "2026-03-01"}]


def test_an_xlsx_renamed_csv_is_read_as_the_workbook_it_actually_is(tmp_path):
    raw = _write_xlsx(tmp_path / "x.xlsx", [["amount", "date"], [100.0, "2026-03-01"]])
    result = read_table("settlements.csv", raw)
    assert result.fmt == "xlsx"
    assert any("bytes are an XLSX workbook" in note for note in result.notes)


def test_a_legacy_xls_is_refused_with_something_a_user_can_act_on():
    with pytest.raises(UnreadableFile) as exc:
        read_table("old.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
    assert "re-save it as .xlsx or CSV" in str(exc.value)


def test_a_truncated_read_is_reported_rather_than_silent():
    body = "".join(f"{i}.00,2026-03-01\n" for i in range(1, 51))
    result = read_table("big.csv", ("amount,date\n" + body).encode(), max_rows=10)
    assert result.truncated is True
    assert len(result.rows) == 10
    assert any("the file has more" in note for note in result.notes)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename,builder,expected", [
    ("orders_march.csv", lambda: orders_file("A", 3, amount_base=1000), SourceType.ORDERS),
    ("razorpay_settlements.csv", lambda: gateway_file("A", 3, amount_base=1000), SourceType.PAYMENT_GATEWAY),
    ("hdfc_statement.csv", lambda: bank_file("A", 3, amount_base=1000), SourceType.BANK_STATEMENT),
    ("tally_daybook.csv", lambda: accounting_file("A", 3, amount_base=1000), SourceType.ACCOUNTING),
])
def test_each_kind_of_export_is_classified_from_its_contents(filename, builder, expected):
    columns, rows = parse_csv(builder().decode())
    result = classify_source(filename, columns, rows)
    assert result.source_type is expected
    assert result.confidence >= 0.65, result.reasons
    assert result.reasons, "a classification with no stated reason is not reviewable"


def test_a_provider_nobody_has_heard_of_still_classifies_by_column_semantics():
    """The provider table names files; it does not gate support. An
    unrecognised gateway must still land on the settlement side."""
    raw = _csv(
        ["txn_ref", "merchant_order_no", "settlement_id", "amount", "commission",
         "currency", "txn_date", "settlement_date", "status"],
        [["T1", "INV-1", "S-1", "1200.00", "24.00", "INR", "2026-03-01", "2026-03-03", "settled"]],
    )
    columns, rows = parse_csv(raw.decode())
    result = classify_source("export.csv", columns, rows)
    assert result.source_type is SourceType.PAYMENT_GATEWAY
    assert result.provider is None, "no provider should be invented for an unknown system"
    assert result.suggested_role == "SETTLEMENT"


def test_a_filename_alone_never_produces_a_confident_classification():
    raw = _csv(["a", "b", "c"], [["1.00", "2026-03-01", "x"]])
    columns, rows = parse_csv(raw.decode())
    result = classify_source("razorpay_settlement_march.csv", columns, rows)
    assert result.confidence <= 0.45
    assert result.needs_confirmation is True
    assert any("filename" in reason for reason in result.reasons)


def test_a_bank_name_in_the_filename_is_reported_as_coming_from_the_filename():
    columns, rows = parse_csv(bank_file("A", 3, amount_base=1000).decode())
    result = classify_source("ICICI_January.csv", columns, rows)
    assert result.provider == "ICICI Bank"
    assert result.provider_confidence <= 0.45
    assert any("filename only" in reason for reason in result.reasons)


def test_a_date_and_amount_range_are_reported_from_the_mapped_columns():
    columns, rows = parse_csv(orders_file("A", 5, amount_base=1000).decode())
    detected = detect_schema(columns, rows)
    result = classify_source("orders.csv", columns, rows, detected)
    assert result.date_range["from"] <= result.date_range["to"]
    assert result.amount_range["min"] == pytest.approx(1001.13, abs=0.01)
    assert result.amount_range["max"] == pytest.approx(1005.65, abs=0.01)
    assert result.currency == "INR"


# ---------------------------------------------------------------------------
# Multi-file upload
# ---------------------------------------------------------------------------

def test_a_single_file_upload_still_returns_the_single_file_shape(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    response = client.post(
        f"/runs/{run_id}/sources",
        files={"file": ("orders.csv", orders_file("A", 2, amount_base=1000), "text/csv")},
        data={"source_type": "ORDERS"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source_id"].startswith("src_")
    assert body["mapping"]["amount"] == "amount"
    assert body["count"] == 1 and len(body["sources"]) == 1


def test_twelve_files_of_five_kinds_upload_and_reconcile_end_to_end(client):
    run_id = client.post("/runs", json={"label": "12-file close"}).json()["run_id"]

    files = []
    for i in range(1, 5):
        files.append((f"shopify_orders_week{i}.csv", orders_file(f"O{i}", 50, amount_base=1000 * i)))
    for i in range(1, 4):
        files.append((f"razorpay_settlements_{i}.csv", gateway_file(f"O{i}", 50, amount_base=1000 * i)))
    for i in range(1, 4):
        files.append((f"hdfc_statement_{i}.csv", bank_file(f"B{i}", 20, amount_base=50000 * i)))
    for i in range(1, 3):
        files.append((f"tally_daybook_{i}.csv", accounting_file(f"T{i}", 10, amount_base=90000 * i)))
    assert len(files) == 12

    started = time.perf_counter()
    response = upload(client, run_id, files)
    upload_seconds = time.perf_counter() - started
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 12, body.get("errors")
    assert body["errors"] == []

    kinds = {}
    for source in body["sources"]:
        kinds[source["source_type"]] = kinds.get(source["source_type"], 0) + 1
        assert source["needs_confirmation"] is False, (source["filename"], source["reasons"])
    assert kinds == {"ORDERS": 4, "PAYMENT_GATEWAY": 3, "BANK_STATEMENT": 3, "ACCOUNTING": 2}, kinds

    execute = client.post(f"/runs/{run_id}/execute", json={})
    assert execute.status_code == 200, execute.text
    summary = execute.json()
    # 4 order files x 50 rows + 2 accounting files x 10 rows
    assert summary["ledger_records"] == 220
    # 3 gateway files x 50 rows + 3 bank files x 20 rows
    assert summary["settlement_records"] == 210
    assert summary["rejected_count"] == 0

    run_started = time.perf_counter()
    run = wait_for(client, run_id)
    run_seconds = time.perf_counter() - run_started
    print(f"\n[12-file] upload {upload_seconds:.2f}s, reconcile {run_seconds:.2f}s, "
          f"{summary['ledger_records']} ledger + {summary['settlement_records']} settlement records")

    # Three of the four order files have a matching gateway export; the
    # fourth deliberately does not, and its orders must surface as
    # exceptions rather than quietly disappear from the count.
    counts = run["outcome_counts"]
    assert counts.get("RECONCILED") == 150
    assert counts.get("EXCEPTION", 0) + counts.get("HUMAN_REVIEW", 0) == 70, \
        "50 unbacked orders and 20 accounting entries with no settlement, none of them lost"
    assert sum(counts.values()) == 220
    for source in client.get(f"/runs/{run_id}").json()["sources"]:
        assert source["accepted_count"] > 0, f"{source['filename']} contributed nothing"


def test_fifty_files_ingest_in_one_request_and_stay_bounded(client):
    run_id = client.post("/runs", json={"label": "50-file workspace"}).json()["run_id"]
    files = []
    for i in range(1, 26):
        files.append((f"orders_{i}.csv", orders_file(f"S{i}", 20, amount_base=1000 * i)))
        files.append((f"settlements_{i}.csv", gateway_file(f"S{i}", 20, amount_base=1000 * i)))
    assert len(files) == 50

    started = time.perf_counter()
    response = upload(client, run_id, files)
    elapsed = time.perf_counter() - started
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 50 and body["errors"] == []
    print(f"\n[50-file] ingest {elapsed:.2f}s for {sum(s['detection']['row_count'] for s in body['sources'])} rows")
    assert elapsed < 60, f"50 small files took {elapsed:.1f}s to ingest"

    plan_started = time.perf_counter()
    plan = client.get(f"/runs/{run_id}/plan").json()
    plan_seconds = time.perf_counter() - plan_started
    assert plan["file_count"] == 50
    assert plan["total_records"] == 1000
    print(f"[50-file] plan {plan_seconds:.2f}s, {len(plan['relationships'])} proposed relationships")
    assert plan_seconds < 30

    execute_started = time.perf_counter()
    execute = client.post(f"/runs/{run_id}/execute", json={}).json()
    run = wait_for(client, run_id)
    execute_seconds = time.perf_counter() - execute_started
    print(f"[50-file] reconcile {execute_seconds:.2f}s, {execute['ledger_records']} ledger + "
          f"{execute['settlement_records']} settlement records")
    assert execute["ledger_records"] == 500 and execute["settlement_records"] == 500
    assert run["outcome_counts"].get("RECONCILED") == 500
    assert execute_seconds < 120


def test_a_fifty_first_file_is_refused_with_a_reason(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    files = [(f"orders_{i}.csv", orders_file(f"S{i}", 1, amount_base=1000 + i)) for i in range(50)]
    assert upload(client, run_id, files).status_code == 200
    response = upload(client, run_id, [("one_too_many.csv", orders_file("X", 1, amount_base=99))])
    assert response.status_code == 400
    assert "limit of 50" in response.json()["detail"]


def test_several_files_of_the_same_kind_all_contribute(client):
    """Three bank statements and two gateway exports are one settlement
    side, not a contest over which file wins."""
    run_id = client.post("/runs", json={}).json()["run_id"]
    files = [("orders.csv", orders_file("M", 6, amount_base=2000))]
    files += [(f"gateway_{i}.csv", gateway_file("M", 3, amount_base=2000)) for i in range(1, 2)]
    files += [("gateway_2.csv", _csv(
        ["payment_id", "order_id", "settlement_id", "amount", "fee", "tax", "net_amount",
         "currency", "created_at", "settlement_date", "status", "description"],
        [[f"pay_M2{i:04d}", f"INV-M-{i}", "setl_M2", f"{2000 + i * 1.13:.2f}", "0.00", "0.00",
          f"{2000 + i * 1.13:.2f}", "INR", f"2026-03-{i:02d}", f"2026-03-{i + 1:02d}",
          "captured", f"second gateway {i}"] for i in range(4, 7)],
    ))]
    files += [(f"bank_{i}.csv", bank_file(f"BK{i}", 2, amount_base=70000 * i)) for i in range(1, 4)]
    assert len(files) == 6

    assert upload(client, run_id, files).status_code == 200
    execute = client.post(f"/runs/{run_id}/execute", json={}).json()
    assert execute["ledger_records"] == 6
    assert execute["settlement_records"] == 3 + 3 + 6      # two gateways plus three statements

    run = wait_for(client, run_id)
    assert run["outcome_counts"].get("RECONCILED") == 6, \
        "orders 1-3 come from one gateway file and 4-6 from another; both must count"
    for source in run["sources"]:
        assert source["accepted_count"] > 0, source["filename"]


def test_two_different_bank_accounts_are_kept_as_two_sources(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    icici = _csv(
        ["Value Date", "Narration", "Withdrawal", "Deposit", "Closing Balance", "Ref No", "IFSC"],
        [["2026-03-01", "NEFT/ACME/CR", "", "5000.00", "105000.00", "UTRIC001", "ICIC0001234"]],
    )
    hdfc = _csv(
        ["Value Date", "Narration", "Withdrawal", "Deposit", "Closing Balance", "Ref No", "IFSC"],
        [["2026-03-01", "NEFT/ACME/CR", "", "6000.00", "206000.00", "UTRHD001", "HDFC0004321"]],
    )
    response = upload(client, run_id, [("account_a.csv", icici), ("account_b.csv", hdfc)])
    assert response.status_code == 200
    providers = {s["filename"]: s["provider"] for s in response.json()["sources"]}
    assert providers == {"account_a.csv": "ICICI Bank", "account_b.csv": "HDFC Bank"}, providers

    plan = client.get(f"/runs/{run_id}/plan").json()
    bank_stage = next(s for s in plan["stages"] if s["stage"] == "BANK")
    assert bank_stage["file_count"] == 2
    assert {s["provider"] for s in bank_stage["sources"]} == {"ICICI Bank", "HDFC Bank"}


def test_an_xlsx_with_a_title_block_uploads_and_reconciles(client, tmp_path):
    run_id = client.post("/runs", json={}).json()["run_id"]
    workbook = _write_xlsx(tmp_path / "settlements.xlsx", [
        ["ACME TRADING PRIVATE LIMITED"],
        [],
        ["Settlement report — March 2026"],
        ["payment_id", "order_id", "amount", "fee", "tax", "net_amount", "currency",
         "created_at", "settlement_date", "status"],
        ["pay_X0001", "INV-X-1", 1001.13, 20.02, 3.6, 977.51, "INR", "2026-03-02", "2026-03-04", "captured"],
        ["pay_X0002", "INV-X-2", 1002.26, 20.05, 3.61, 978.6, "INR", "2026-03-03", "2026-03-05", "captured"],
    ])
    response = client.post(
        f"/runs/{run_id}/sources",
        files=[("files", ("orders.csv", orders_file("X", 2, amount_base=1000), "text/csv")),
               ("files", ("settlements.xlsx", workbook,
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
    )
    assert response.status_code == 200, response.text
    sheet = next(s for s in response.json()["sources"] if s["filename"] == "settlements.xlsx")
    assert sheet["format"] == "xlsx"
    assert sheet["detection"]["header_row"] == 4
    assert sheet["source_type"] == "PAYMENT_GATEWAY"

    assert client.post(f"/runs/{run_id}/execute", json={}).status_code == 200
    run = wait_for(client, run_id)
    assert run["outcome_counts"].get("RECONCILED") == 2


def test_a_duplicate_file_is_flagged_not_swallowed(client):
    """The same statement uploaded twice is a real finding — reporting it
    is more use than silently dropping the second copy."""
    run_id = client.post("/runs", json={}).json()["run_id"]
    content = bank_file("D", 3, amount_base=4000)
    response = upload(client, run_id, [("march.csv", content), ("march_copy.csv", content)])
    assert response.status_code == 200
    first, second = response.json()["sources"]
    assert first["duplicate_of"] is None
    assert second["duplicate_of"] == first["source_id"]
    assert second["duplicate_of_filename"] == "march.csv"

    plan = client.get(f"/runs/{run_id}/plan").json()
    assert [d["filename"] for d in plan["duplicates"]] == ["march_copy.csv"]
    assert plan["file_count"] == 2, "the duplicate is kept, not rejected"


# ---------------------------------------------------------------------------
# Refusing to guess
# ---------------------------------------------------------------------------

AMBIGUOUS = _csv(
    ["txn_date", "narration", "voucher_no", "debit", "credit"],
    [["2026-03-01", "NEFT/ACME/CR", "V-1", "0.00", "5000.00"],
     ["2026-03-02", "NEFT/VENDOR/DR", "V-2", "2500.00", "0.00"]],
)


def test_a_genuinely_ambiguous_file_asks_instead_of_choosing(client):
    """A narration column says bank statement; a voucher number says day
    book. Neither wins, so the answer is a question."""
    columns, rows = parse_csv(AMBIGUOUS.decode())
    result = classify_source("export_2026_03.csv", columns, rows)
    assert result.needs_confirmation is True
    assert result.confidence < 0.65, result.reasons

    run_id = client.post("/runs", json={}).json()["run_id"]
    response = upload(client, run_id, [("export_2026_03.csv", AMBIGUOUS),
                                       ("orders.csv", orders_file("A", 2, amount_base=1000))])
    ambiguous = next(s for s in response.json()["sources"] if s["filename"] == "export_2026_03.csv")
    assert ambiguous["needs_confirmation"] is True
    assert ambiguous["role_confirmed"] is False

    blocked = client.post(f"/runs/{run_id}/execute", json={})
    assert blocked.status_code == 400
    assert "confirm what these file(s) are" in blocked.json()["detail"]

    plan = client.get(f"/runs/{run_id}/plan").json()
    assert plan["can_execute"] is False
    assert any(b["kind"] == "UNCONFIRMED_ROLE" for b in plan["blocking"])

    confirmed = client.put(f"/runs/{run_id}/plan", json={
        "sources": [{"source_id": ambiguous["source_id"], "source_type": "BANK_STATEMENT",
                     "confirmed": True}],
        "confirmed": True,
    })
    assert confirmed.status_code == 200
    assert confirmed.json()["can_execute"] is True
    assert client.post(f"/runs/{run_id}/execute", json={}).status_code == 200
    wait_for(client, run_id)


def test_a_file_with_an_unmapped_required_column_blocks_the_run(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    mystery = _csv(["alpha", "beta", "gamma"], [["foo", "bar", "baz"], ["qux", "quux", "corge"]])
    response = upload(client, run_id, [("mystery.csv", mystery),
                                       ("gw.csv", gateway_file("U", 2, amount_base=1000))],
                      source_types=["ORDERS", "PAYMENT_GATEWAY"])
    assert response.status_code == 200
    unmapped = next(s for s in response.json()["sources"] if s["filename"] == "mystery.csv")
    assert "date" in unmapped["detection"]["unmapped_required"]

    blocked = client.post(f"/runs/{run_id}/execute", json={})
    assert blocked.status_code == 400
    assert "map the required columns" in blocked.json()["detail"]

    plan = client.get(f"/runs/{run_id}/plan").json()
    assert any(b["kind"] == "UNMAPPED_REQUIRED" for b in plan["blocking"])


def test_the_same_amount_in_two_unrelated_files_does_not_reconcile(client):
    """The safety property this whole product rests on. An order and a
    bank credit for the identical amount, with nothing else in common,
    must never be allowed to satisfy each other."""
    run_id = client.post("/runs", json={}).json()["run_id"]
    orders = _csv(
        ["order_id", "invoice_ref", "amount", "currency", "order_date", "customer_email",
         "sku", "quantity", "description", "status"],
        [["ORD-C1", "INV-C1", "7777.77", "INR", "2026-03-01", "a@example.com", "SKU-1", 1,
          "Annual licence", "captured"]],
    )
    bank = _csv(
        ["Value Date", "Narration", "Withdrawal", "Deposit", "Closing Balance", "Ref No"],
        [["2026-03-01", "NEFT/UNRELATED PARTY/CR", "", "7777.77", "107777.77", "UTRZZZ99"]],
    )
    assert upload(client, run_id, [("orders.csv", orders), ("hdfc_statement.csv", bank)]).status_code == 200
    assert client.post(f"/runs/{run_id}/execute", json={}).status_code == 200
    run = wait_for(client, run_id)

    record = client.get("/records/ORD-C1", params={"batch_id": run_id}).json()
    assert record["outcome"] != "RECONCILED", (
        f"a same-amount coincidence was auto-reconciled: {record['reason']}"
    )
    assert run["outcome_counts"].get("RECONCILED") in (None, 0)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def test_combine_provenance_stays_aligned_with_combine():
    """Aligned by position across two functions, so drifting by one would
    attribute every record to the wrong line of the wrong file."""
    sources = []
    for tag, kind, builder in (("A", SourceType.ORDERS, orders_file),
                               ("B", SourceType.ORDERS, orders_file)):
        columns, rows = parse_csv(builder(tag, 3, amount_base=1000).decode())
        schema = detect_schema(columns, rows)
        sources.append(map_rows(rows, schema.mapping, kind, f"src_{tag}", schema.amount_scale,
                                filename=f"{tag}.csv"))
    ledger, settlements, _ = combine(sources)
    ledger_provenance, settlement_provenance = combine_provenance(sources)
    assert len(ledger) == len(ledger_provenance) == 6
    assert settlements == [] and settlement_provenance == []
    assert [p["filename"] for p in ledger_provenance] == ["A.csv"] * 3 + ["B.csv"] * 3
    assert [p["file_row"] for p in ledger_provenance] == [2, 3, 4, 2, 3, 4]


def test_provenance_survives_into_the_stored_record_across_sources(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    assert upload(client, run_id, [
        ("shopify_orders.csv", orders_file("P", 3, amount_base=1000)),
        ("ICICI_January.csv", _csv(
            ["Value Date", "Narration", "Withdrawal", "Deposit", "Closing Balance", "Ref No"],
            [["2026-03-01", "pad", "", "1.00", "1.00", "INV-PAD"],
             ["2026-03-02", "NEFT/ACME/CR", "", "1002.26", "1003.26", "INV-P-2"]],
        )),
    ]).status_code == 200
    assert client.post(f"/runs/{run_id}/execute", json={}).status_code == 200
    wait_for(client, run_id)

    record = client.get("/records/ORD-P-2", params={"batch_id": run_id}).json()
    provenance = json.loads(record["provenance_json"])
    assert provenance["ledger"]["filename"] == "shopify_orders.csv"
    assert provenance["ledger"]["file_row"] == 3, "row 1 is the header, so order 2 is on line 3"
    assert record["outcome"] == "RECONCILED"
    assert provenance["settlement"]["filename"] == "ICICI_January.csv"
    assert provenance["settlement"]["file_row"] == 3

    export = client.get(f"/runs/{run_id}/export").text
    rows = list(csv.DictReader(io.StringIO(export)))
    matched = next(r for r in rows if r["record_id"] == "ORD-P-2")
    assert matched["ledger_source_file"] == "shopify_orders.csv"
    assert matched["settlement_source_file"] == "ICICI_January.csv"
    assert matched["settlement_source_row"] == "3"


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def test_the_plan_reports_absent_stages_rather_than_implying_coverage(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    assert upload(client, run_id, [
        ("shopify_orders.csv", orders_file("F", 3, amount_base=1000)),
        ("hdfc_statement.csv", bank_file("F", 3, amount_base=4000)),
    ]).status_code == 200

    plan = client.get(f"/runs/{run_id}/plan").json()
    assert plan["stages_present"] == ["ORDERS", "BANK"]
    assert set(plan["stages_absent"]) == {"PAYMENT_GATEWAY", "SETTLEMENT", "ACCOUNTING"}
    for stage in plan["stages"]:
        if not stage["present"]:
            assert stage["sources"] == []
            assert stage["absent_reason"] == "no uploaded file covers this stage"
    assert "does not match stage-by-stage" in plan["engine_note"]

    # Adjacency skips the empty stages rather than proposing nothing.
    assert [(r["from_stage"], r["to_stage"]) for r in plan["relationships"]] == [("ORDERS", "BANK")]
    assert plan["relationships"][0]["from_filename"] == "shopify_orders.csv"


def test_the_plan_counts_files_records_and_categories(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    upload(client, run_id, [
        ("orders_a.csv", orders_file("G1", 4, amount_base=1000)),
        ("orders_b.csv", orders_file("G2", 4, amount_base=2000)),
        ("razorpay_settlements.csv", gateway_file("G1", 4, amount_base=1000)),
        ("hdfc_statement.csv", bank_file("G3", 2, amount_base=9000)),
        ("tally_daybook.csv", accounting_file("G4", 2, amount_base=8000)),
    ])
    plan = client.get(f"/runs/{run_id}/plan").json()
    assert plan["file_count"] == 5
    assert plan["total_records"] == 16
    assert plan["source_type_counts"] == {
        "ORDERS": 2, "PAYMENT_GATEWAY": 1, "BANK_STATEMENT": 1, "ACCOUNTING": 1,
    }
    assert plan["role_counts"] == {"LEDGER": 3, "SETTLEMENT": 2}
    assert plan["coverage"]["available"] is False, "no run yet — nothing to report per stage"


def test_a_confirmed_relationship_is_persisted_on_the_run(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    response = upload(client, run_id, [
        ("shopify_orders.csv", orders_file("R", 3, amount_base=1000)),
        ("razorpay_settlements.csv", gateway_file("R", 3, amount_base=1000)),
    ])
    orders, gateway = response.json()["sources"]

    plan = client.get(f"/runs/{run_id}/plan").json()
    proposed = plan["relationships"][0]
    assert proposed["status"] == "PROPOSED"
    assert proposed["shared_identifier_count"] == 3, \
        "both files carry INV-R-1..3, which is real evidence they relate"

    updated = client.put(f"/runs/{run_id}/plan", json={
        "relationships": [{"from_source_id": orders["source_id"],
                           "to_source_id": gateway["source_id"],
                           "confirmed": True, "note": "same invoice numbers"}],
        "confirmed": True,
    }).json()
    assert updated["relationships"][0]["status"] == "CONFIRMED"
    assert updated["confirmed"] is True

    reread = client.get(f"/runs/{run_id}/plan").json()
    assert reread["relationships"][0]["status"] == "CONFIRMED"
    assert reread["relationships"][0]["note"] == "same invoice numbers"


def test_stage_counts_after_a_run_come_from_real_provenance(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    upload(client, run_id, [
        ("shopify_orders.csv", orders_file("C", 3, amount_base=1000)),
        ("razorpay_settlements.csv", gateway_file("C", 3, amount_base=1000)),
    ])
    client.post(f"/runs/{run_id}/execute", json={})
    wait_for(client, run_id)

    plan = client.get(f"/runs/{run_id}/plan").json()
    assert plan["coverage"]["available"] is True
    assert plan["coverage"]["records_without_provenance"] == 0
    orders_stage = next(s for s in plan["stages"] if s["stage"] == "ORDERS")
    settlement_stage = next(s for s in plan["stages"] if s["stage"] == "SETTLEMENT")
    assert orders_stage["records_sourced_here"] == 3
    assert settlement_stage["records_settled_here"] == 3
    bank_stage = next(s for s in plan["stages"] if s["stage"] == "BANK")
    assert bank_stage["records_settled_here"] == 0
    assert "No stage percentage is estimated" in plan["coverage"]["note"]


def test_the_plan_endpoints_404_on_a_run_that_does_not_exist(client):
    assert client.get("/runs/run_nope/plan").status_code == 404
    assert client.put("/runs/run_nope/plan", json={}).status_code == 404


def test_confirming_a_source_that_is_not_in_this_run_is_refused(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    upload(client, run_id, [("orders.csv", orders_file("N", 2, amount_base=1000))])
    response = client.put(f"/runs/{run_id}/plan", json={
        "sources": [{"source_id": "src_elsewhere", "source_type": "ORDERS"}],
    })
    assert response.status_code == 404
    assert "src_elsewhere" in response.json()["detail"]


def test_an_unknown_source_type_is_still_refused_up_front(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    response = upload(client, run_id, [("orders.csv", orders_file("Z", 1, amount_base=1000))],
                      source_type="TELEPATHY")
    assert response.status_code == 400
    assert "TELEPATHY" in response.json()["detail"]
