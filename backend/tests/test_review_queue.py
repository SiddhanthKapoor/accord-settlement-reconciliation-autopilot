"""
The human review queue.

Two things make this a real workflow rather than a dashboard: the items
are the pipeline's own decisions (so the queue cannot drift from what the
engine actually decided), and every human action lands in the same
hash-chained ledger as the automated ones, carrying why the automation
escalated in the first place.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.ledger import audit, db, store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review_test.db")
    db._local.__dict__.clear()
    db.init_db()
    from app.main import app
    with TestClient(app) as c:
        yield c
    db._local.__dict__.clear()


def _run_batch(client, limit=40):
    started = client.post("/batch/run", json={"dataset": "dev", "limit": limit})
    if started.status_code != 200:
        pytest.skip("dataset not generated in this environment")
    deadline = time.time() + 60
    while time.time() < deadline:
        batch = client.get("/batch/latest").json()["batch"]
        if batch and batch["status"] == "COMPLETED":
            return batch
        time.sleep(0.05)
    raise AssertionError("batch did not complete")


def test_queue_is_empty_before_any_batch(client):
    body = client.get("/review/queue").json()
    assert body["items"] == []
    assert body["summary"]["open_count"] == 0


def test_queue_contains_real_pipeline_decisions(client):
    batch = _run_batch(client)
    body = client.get("/review/queue", params={"batch_id": batch["batch_id"]}).json()

    listed = client.get(f"/batch/{batch['batch_id']}/records").json()
    needing_review = {r["record_id"] for r in listed if r["outcome"] in ("HUMAN_REVIEW", "EXCEPTION")}
    assert {i["record_id"] for i in body["items"]} <= needing_review, \
        "the queue must be a view over real decisions, not a separate store"
    assert body["summary"]["open_count"] >= len(body["items"])


def test_queue_is_ordered_worst_first(client):
    batch = _run_batch(client)
    items = client.get("/review/queue", params={"batch_id": batch["batch_id"], "limit": 50}).json()["items"]
    if len(items) < 2:
        pytest.skip("not enough review items in this batch")

    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, None: 3}
    severities = [rank.get(i.get("severity"), 3) for i in items]
    assert severities == sorted(severities), "an operator's first item should be the most serious one"


def test_every_item_explains_itself_and_offers_only_sensible_actions(client):
    batch = _run_batch(client)
    items = client.get("/review/queue", params={"batch_id": batch["batch_id"]}).json()["items"]
    if not items:
        pytest.skip("no review items in this batch")

    for item in items:
        assert item["explanation"], "a review item with no explanation is not actionable"
        assert item["recommended_action"]
        assert item["exception_type"]
        actions = {a["action"] for a in item["available_actions"]}
        assert actions, "an open item must offer at least one action"
        if not item["matched_payment_id"] and not item["considered_candidates"]:
            assert "APPROVE_MATCH" not in actions, \
                "cannot offer to approve a match when there is no candidate to approve"


def test_a_human_action_is_written_to_the_audit_ledger(client):
    batch = _run_batch(client)
    items = client.get("/review/queue", params={"batch_id": batch["batch_id"]}).json()["items"]
    if not items:
        pytest.skip("no review items in this batch")
    item = items[0]

    before = client.get("/audit/verify").json()["total_events"]
    response = client.post(
        f"/review/{item['record_id']}/action",
        json={"batch_id": batch["batch_id"], "action": "ESCALATE", "note": "checking with provider"},
    )
    assert response.status_code == 200
    assert response.json()["review_state"] == "ESCALATED"

    after = client.get("/audit/verify").json()
    assert after["total_events"] == before + 1
    assert after["intact"] is True, "a human action must not break the chain"

    trail = client.get(f"/records/{item['record_id']}",
                       params={"batch_id": batch["batch_id"]}).json()["audit_trail"]
    action_events = [e for e in trail if e["event_type"] == "HUMAN_REVIEW_ACTION"]
    assert action_events, "the action must appear on the record's own trail"
    event = action_events[-1]
    assert event["prior_state"] == "OPEN"
    assert event["new_state"] == "ESCALATED"
    assert event["payload"]["reviewer"]
    assert event["payload"]["note"] == "checking with provider"
    assert event["payload"]["escalated_because"], \
        "the ledger must preserve why automation escalated, not only what the human did"


def test_an_actioned_item_leaves_the_open_queue(client):
    batch = _run_batch(client)
    items = client.get("/review/queue", params={"batch_id": batch["batch_id"]}).json()["items"]
    if not items:
        pytest.skip("no review items in this batch")
    item = items[0]

    client.post(f"/review/{item['record_id']}/action",
                json={"batch_id": batch["batch_id"], "action": "DEFER"})
    remaining = client.get("/review/queue", params={"batch_id": batch["batch_id"], "limit": 100}).json()
    assert item["record_id"] not in {i["record_id"] for i in remaining["items"]}

    deferred = client.get("/review/queue",
                          params={"batch_id": batch["batch_id"], "state": "DEFERRED"}).json()
    assert item["record_id"] in {i["record_id"] for i in deferred["items"]}


def test_the_same_item_cannot_be_actioned_twice(client):
    batch = _run_batch(client)
    items = client.get("/review/queue", params={"batch_id": batch["batch_id"]}).json()["items"]
    if not items:
        pytest.skip("no review items in this batch")
    record_id = items[0]["record_id"]

    first = client.post(f"/review/{record_id}/action",
                        json={"batch_id": batch["batch_id"], "action": "DEFER"})
    second = client.post(f"/review/{record_id}/action",
                         json={"batch_id": batch["batch_id"], "action": "ESCALATE"})
    assert first.status_code == 200
    assert second.status_code == 409, "a decided item must not be silently re-decided"


def test_unknown_action_is_rejected(client):
    batch = _run_batch(client)
    items = client.get("/review/queue", params={"batch_id": batch["batch_id"]}).json()["items"]
    if not items:
        pytest.skip("no review items in this batch")
    response = client.post(f"/review/{items[0]['record_id']}/action",
                           json={"batch_id": batch["batch_id"], "action": "DELETE_EVERYTHING"})
    assert response.status_code == 400


def test_action_on_a_missing_record_is_a_404(client):
    batch = _run_batch(client)
    response = client.post("/review/does-not-exist/action",
                           json={"batch_id": batch["batch_id"], "action": "DEFER"})
    assert response.status_code == 404


def test_a_reviewed_record_is_not_reopened_by_re_running_the_batch(client):
    """Re-running reconciliation is routine. Silently discarding a
    reviewer's decision because the engine ran again would make the queue
    untrustworthy."""
    batch = _run_batch(client)
    items = client.get("/review/queue", params={"batch_id": batch["batch_id"]}).json()["items"]
    if not items:
        pytest.skip("no review items in this batch")
    record_id = items[0]["record_id"]
    client.post(f"/review/{record_id}/action", json={"batch_id": batch["batch_id"], "action": "DEFER"})

    record = store.get_record(record_id, batch["batch_id"])
    from app.domain.models import MerchantRecord, ReconciliationRecord, ReconciliationResult
    import json as _json
    rebuilt = ReconciliationRecord(
        record_id=record_id, merchant=MerchantRecord.model_validate(_json.loads(record["merchant_json"])))
    result = ReconciliationResult(
        record_id=record_id, outcome=record["outcome"], reason=record["reason"], checks=[],
        candidate_count=0, policy_threshold=0.85, latency_ms=1.0,
    )
    store.save_record(batch["batch_id"], 0, rebuilt, result, [])

    assert store.get_record(record_id, batch["batch_id"])["review_state"] == "DEFERRED"
