"""
API-level tests.

These exist because two of the bugs this project actually shipped were
invisible to unit tests and to a casual curl: a literal route registered
after a parameterised one that swallowed it, and an endpoint whose
response shape changed depending on whether it found anything. Both were
found by clicking through a running server, which is exactly the kind of
verification that does not happen reliably. They are pinned here instead.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.ledger import audit, db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "api_test.db")
    db._local.__dict__.clear()
    db.init_db()

    from app.main import app
    with TestClient(app) as c:
        yield c
    db._local.__dict__.clear()


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def test_batch_latest_is_not_shadowed_by_the_parameterised_batch_route(client):
    """`/batch/{batch_id}` was registered before `/batch/latest`, so the
    literal path was matched as a batch whose id was the string 'latest'
    and always 404'd. Route order is load-bearing and nothing else
    enforces it."""
    response = client.get("/batch/latest")
    assert response.status_code == 200
    assert response.json() == {"batch": None}


def test_route_ordering_puts_literals_before_their_parameterised_siblings(client):
    """Generalises the bug above: any literal segment registered after a
    sibling path parameter at the same position is unreachable."""
    from app.api.routes import router

    seen_params: dict[tuple[int, str], str] = {}
    problems = []
    for route in router.routes:
        parts = route.path.strip("/").split("/")
        for i, part in enumerate(parts):
            prefix = "/".join(parts[:i])
            key = (i, prefix)
            if part.startswith("{"):
                seen_params.setdefault(key, route.path)
            elif key in seen_params:
                problems.append(f"{route.path} is shadowed by {seen_params[key]}")
    assert not problems, "unreachable routes: " + "; ".join(problems)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

def test_batch_latest_keeps_one_shape_whether_or_not_a_batch_exists(client):
    """It used to return the batch object directly when one existed and
    {"batch": null} when none did, so the client's `if (b.batch)` check
    was false in both cases and the console never restored a batch."""
    empty = client.get("/batch/latest").json()
    assert set(empty) == {"batch"} and empty["batch"] is None

    started = client.post("/batch/run", json={"dataset": "dev", "limit": 1})
    if started.status_code != 200:
        pytest.skip("dataset not generated in this environment")
    _wait_for_completion(client)

    populated = client.get("/batch/latest").json()
    assert set(populated) == {"batch"}
    assert populated["batch"]["batch_id"]
    assert "outcome_counts" in populated["batch"]


def test_unknown_batch_is_a_404_not_an_empty_success(client):
    assert client.get("/batch/does-not-exist").status_code == 404


def test_unknown_record_is_a_404(client):
    assert client.get("/records/nope").status_code == 404


def test_health_reports_the_service(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# Batch execution
# ---------------------------------------------------------------------------

def _wait_for_completion(client, timeout_seconds: float = 60.0) -> dict:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        batch = client.get("/batch/latest").json()["batch"]
        if batch and batch["status"] == "COMPLETED":
            return batch
        time.sleep(0.05)
    raise AssertionError("batch did not complete in time")


def test_a_batch_runs_end_to_end_and_reports_consistent_counts(client):
    started = client.post("/batch/run", json={"dataset": "dev", "limit": 5})
    if started.status_code != 200:
        pytest.skip("dataset not generated in this environment")
    batch = _wait_for_completion(client)

    assert batch["processed_records"] == batch["total_records"]
    assert sum(batch["outcome_counts"].values()) == batch["total_records"], \
        "outcome counts must account for every processed record"

    records = client.get(f"/batch/{batch['batch_id']}/records").json()
    assert len(records) == batch["total_records"], \
        "the batch's own listing must return exactly the records it reported processing"


def test_an_empty_batch_completes_cleanly(client):
    started = client.post("/batch/run", json={"dataset": "dev", "limit": 0})
    if started.status_code != 200:
        pytest.skip("dataset not generated in this environment")
    batch = _wait_for_completion(client)
    assert batch["total_records"] == 0
    assert batch["status"] == "COMPLETED"
    assert client.get(f"/batch/{batch['batch_id']}/records").json() == []


def test_re_running_the_same_dataset_leaves_the_first_batch_listable(client):
    first = client.post("/batch/run", json={"dataset": "dev", "limit": 3})
    if first.status_code != 200:
        pytest.skip("dataset not generated in this environment")
    first_id = first.json()["batch_id"]
    _wait_for_completion(client)

    client.post("/batch/run", json={"dataset": "dev", "limit": 3})
    _wait_for_completion(client)

    assert len(client.get(f"/batch/{first_id}/records").json()) == 3, \
        "the second run must not consume the first batch's rows"


def test_record_detail_exposes_the_evidence_behind_the_decision(client):
    started = client.post("/batch/run", json={"dataset": "dev", "limit": 3})
    if started.status_code != 200:
        pytest.skip("dataset not generated in this environment")
    batch = _wait_for_completion(client)
    records = client.get(f"/batch/{batch['batch_id']}/records").json()

    detail = client.get(f"/records/{records[0]['record_id']}",
                        params={"batch_id": batch["batch_id"]}).json()
    assert detail["merchant"]["order_id"]
    assert isinstance(detail["checks"], list) and detail["checks"]
    assert isinstance(detail["candidates"], list)
    assert detail["policy_threshold"] > 0
    for event in detail["audit_trail"]:
        assert isinstance(event["payload"], dict), "payload must be parsed JSON, not a string"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def test_audit_chain_verifies_over_a_real_batch(client):
    started = client.post("/batch/run", json={"dataset": "dev", "limit": 5})
    if started.status_code != 200:
        pytest.skip("dataset not generated in this environment")
    _wait_for_completion(client)

    verification = client.get("/audit/verify").json()
    assert verification["intact"] is True
    assert verification["breaks"] == []
    assert verification["total_events"] > 0


def test_resume_point_prefers_an_explicit_cursor():
    from app.api.routes import resume_point
    assert resume_point(2, None, 99) == 2
    assert resume_point(2, "50", 99) == 2, "an explicit ?since= outranks the header"


def test_resume_point_uses_the_last_event_id_header_when_no_cursor_is_given():
    """Browsers resend Last-Event-ID automatically after a dropped
    connection; honouring it is what makes a reconnect lossless."""
    from app.api.routes import resume_point
    assert resume_point(None, "7", 99) == 7
    assert resume_point(None, " 7 ", 99) == 7


def test_resume_point_defaults_to_the_head_not_the_beginning():
    """Replaying the whole ledger to every newly-attached client is how
    the console fell over during a large batch."""
    from app.api.routes import resume_point
    assert resume_point(None, None, 4210) == 4210


def test_resume_point_ignores_a_junk_header_and_clamps_negatives():
    from app.api.routes import resume_point
    assert resume_point(None, "not-a-number", 12) == 12
    assert resume_point(None, "", 12) == 12
    assert resume_point(-5, None, 12) == 0


# The live stream itself is intentionally not exercised through
# TestClient: the endpoint never completes by design, so any assertion
# against it is a race with a sleep. Its resumption logic is covered by
# the pure-function tests above, and the wire format is verified by hand
# against a running server (see frontend/e2e/ for the browser suites).


def test_admin_reset_clears_state(client):
    audit.append_event(transaction_id="t", event_type="RECORD_DECIDED",
                       prior_state=None, new_state="RECONCILED", payload={})
    assert client.post("/admin/reset").json()["status"] == "reset"
    assert client.get("/batch/latest").json() == {"batch": None}
    assert client.get("/audit/verify").json()["total_events"] == 0


# ---------------------------------------------------------------------------
# A run's status must describe the run, not the moment its row was inserted.
#
# `create_batch` stamps RUNNING before a single source is uploaded, so every
# abandoned workspace reported itself as running forever. Twenty such rows
# made a working product look like it failed constantly — and it was simply
# a false statement about state.
# ---------------------------------------------------------------------------

def test_a_workspace_that_never_ran_is_a_draft_not_a_running_run(client):
    run_id = client.post("/runs", json={"label": "abandoned"}).json()["run_id"]

    listed = client.get("/runs?include_drafts=true").json()["runs"]
    mine = next(r for r in listed if r["batch_id"] == run_id)
    assert mine["status"] == "DRAFT"
    assert mine["status"] != "RUNNING"


def test_drafts_are_hidden_by_default_but_not_destroyed(client):
    run_id = client.post("/runs", json={"label": "abandoned"}).json()["run_id"]

    default = client.get("/runs").json()["runs"]
    assert all(r["batch_id"] != run_id for r in default), "a draft leaked into the default list"

    with_drafts = client.get("/runs?include_drafts=true").json()["runs"]
    assert any(r["batch_id"] == run_id for r in with_drafts), "the draft was hidden AND lost"


def test_a_run_that_stopped_advancing_is_reported_interrupted(client):
    """A killed process leaves its row RUNNING forever. Reporting that
    honestly is the difference between 'still working' and 'this died'."""
    from datetime import datetime, timedelta, timezone

    from app.ledger import store

    store.create_batch("batch_stalled", "stalled", "upload", 0)
    store.set_batch_total("batch_stalled", 100, "stalled")
    old = (datetime.now(timezone.utc) - timedelta(seconds=store.STALE_RUN_SECONDS + 60)).isoformat()
    store.get_conn().execute(
        "UPDATE batches SET started_at=? WHERE batch_id=?", (old, "batch_stalled")
    )

    listed = client.get("/runs").json()["runs"]
    stalled = next(r for r in listed if r["batch_id"] == "batch_stalled")
    assert stalled["status"] == "INTERRUPTED"


def test_a_run_in_flight_is_still_reported_running(client):
    """The staleness rule must not slander a run that is genuinely working."""
    from app.ledger import store

    store.create_batch("batch_live", "live", "upload", 0)
    store.set_batch_total("batch_live", 100, "live")

    listed = client.get("/runs").json()["runs"]
    live = next(r for r in listed if r["batch_id"] == "batch_live")
    assert live["status"] == "RUNNING"


def test_the_evaluation_page_serves_the_frozen_result_not_a_scratch_run(client):
    """`data/eval_reports/` is a scratch pad — ad-hoc runs on arbitrary
    datasets land there. Serving the newest of them let the Evaluation page
    display 97.7% from a 999-record run on an unrelated seed while every
    document, the frozen report and the commit said 88.3%. A product that
    contradicts its own evidence in front of a reviewer is worse than one
    that shows no number at all."""
    body = client.get("/evaluation/latest").json()

    assert body.get("source") == "frozen"
    assert body.get("evaluation_id") == "ACCORD"
    assert body["metrics"]["record_count"] == 1000
    assert body.get("code_commit"), "a served evaluation must name the commit that produced it"
    assert body.get("working_tree_dirty") is False, "a dirty-tree report describes no commit"
