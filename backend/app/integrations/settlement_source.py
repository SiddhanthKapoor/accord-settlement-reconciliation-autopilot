"""
The boundary between real Razorpay data and synthetic evaluation data.

Everything downstream of this module — matching, policy, audit,
evaluation — consumes `RazorpaySettlementRecord` objects and cannot tell
which source produced them. That is the point: the reconciliation engine
is not written against a fixture, and pointing it at a merchant's real
account is a configuration change rather than a rewrite.

It also means the distinction has to be explicit and loud, because
nothing further down enforces it. Every source reports its own
`provenance`, which is surfaced in the API and the UI so a reader is
never left guessing whether a number came from Razorpay or from a
generator.

    LIVE_RAZORPAY  real API responses from a real account
    SYNTHETIC      generated records; evaluation and demo only
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from app.domain.models import RazorpaySettlementRecord


class Provenance(str, Enum):
    LIVE_RAZORPAY = "LIVE_RAZORPAY"
    SYNTHETIC = "SYNTHETIC"


@dataclass
class SourceStatus:
    provenance: Provenance
    available: bool
    record_count: int
    detail: str


class SettlementSource(Protocol):
    provenance: Provenance

    def status(self) -> SourceStatus: ...
    def fetch(self, limit: int = 500) -> list[RazorpaySettlementRecord]: ...


class SyntheticSettlementSource:
    """Records from the generated dataset. Never presented as real."""

    provenance = Provenance.SYNTHETIC

    def __init__(self, pool_path: Path | None = None) -> None:
        self._path = pool_path or (
            Path(__file__).resolve().parent.parent.parent / "data" / "datasets" / "razorpay_pool.jsonl"
        )

    def status(self) -> SourceStatus:
        if not self._path.exists():
            return SourceStatus(self.provenance, False, 0,
                                f"No generated dataset at {self._path.name}. "
                                "Run data/generate_dataset.py first.")
        count = sum(1 for _ in self._path.open())
        return SourceStatus(self.provenance, True, count,
                            f"{count} settlement records from the labelled evaluation dataset "
                            f"({self._path.name}). Generated and labelled as such; kept separate "
                            "from anything uploaded into a workspace.")

    def fetch(self, limit: int = 500) -> list[RazorpaySettlementRecord]:
        records = []
        with self._path.open() as f:
            for line in f:
                if len(records) >= limit:
                    break
                records.append(RazorpaySettlementRecord.model_validate_json(line))
        return records


class RazorpayLiveSettlementSource:
    """Real Razorpay Settlements API. See razorpay_settlements.py for the
    verified state of what a test-mode account can actually return."""

    provenance = Provenance.LIVE_RAZORPAY

    def status(self) -> SourceStatus:
        from app.integrations import razorpay_settlements

        try:
            records = razorpay_settlements.fetch_live_settlements(count=100)
        except razorpay_settlements.RazorpayNotConfigured as exc:
            return SourceStatus(self.provenance, False, 0, str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced, never silently swallowed
            return SourceStatus(self.provenance, False, 0,
                                f"Razorpay API call failed: {type(exc).__name__}: {exc}")

        if not records:
            return SourceStatus(
                self.provenance, True, 0,
                "Connected. This account has no settlement history yet — a settlement appears "
                "once a payment is captured through checkout and its bank settlement cycle "
                "completes.",
            )
        return SourceStatus(self.provenance, True, len(records),
                            f"{len(records)} live settlement records fetched from Razorpay.")

    def fetch(self, limit: int = 500) -> list[RazorpaySettlementRecord]:
        from app.integrations import razorpay_settlements

        return razorpay_settlements.fetch_live_settlements(count=limit)


def describe_sources() -> dict:
    """What each source can currently supply.

    Read as provenance, not as a fault report: it says which settlement
    source is in use so a number on screen is never mistaken for one
    pulled from a live merchant account. The `provenance` values are the
    stable part and are what callers switch on; the prose is there to be
    read by a person.
    """
    live = RazorpayLiveSettlementSource().status()
    synthetic = SyntheticSettlementSource().status()
    return {
        "active_source": Provenance.SYNTHETIC.value,
        "active_source_reason": (
            "Reconciliation runs on the labelled evaluation dataset. The Razorpay Settlements "
            "integration is configured and reports its own status alongside it."
        ),
        "sources": [
            {"provenance": s.provenance.value, "available": s.available,
             "record_count": s.record_count, "detail": s.detail}
            for s in (live, synthetic)
        ],
    }
