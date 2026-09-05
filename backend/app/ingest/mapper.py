"""
Mapped CSV rows -> the canonical records the engine already understands.

`MerchantRecord` and `RazorpaySettlementRecord` keep their names because
renaming them would churn the engine, the tests and three frozen
evaluations for no behavioural gain. Read them as the two *roles* rather
than as two vendors: the ledger side (what the business believes) and the
settlement side (what happened to the money). A bank statement lands in
the settlement role exactly as a gateway payout does.

Rows that cannot be mapped are rejected individually and reported. One
malformed line in a 40,000-row bank export must not cost the other 39,999
their reconciliation, and it must not be silently dropped either — a
reconciliation that quietly ignored rows is worse than one that refuses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.models import MerchantRecord, RazorpaySettlementRecord
from app.ingest.schema import SourceType, parse_amount, parse_date

# Free-text status values seen in real exports, mapped onto the domain's
# closed set. Anything unrecognised becomes the neutral default rather
# than failing the row: status is corroborating detail, not identity.
_LEDGER_STATUS = {
    "captured": "captured", "paid": "captured", "success": "captured", "successful": "captured",
    "completed": "captured", "settled": "captured", "authorized": "captured", "complete": "captured",
    "refunded": "refunded", "reversed": "refunded", "returned": "refunded",
    "partially_refunded": "partially_refunded", "partial_refund": "partially_refunded",
}
_SETTLEMENT_STATUS = {
    "settled": "settled", "paid": "settled", "processed": "settled", "credited": "settled",
    "success": "settled", "completed": "settled", "captured": "settled",
    "refunded": "refunded", "reversed": "refunded", "returned": "refunded",
    "partially_refunded": "partially_refunded", "partial_refund": "partially_refunded",
}


@dataclass
class MappedSource:
    source_id: str
    source_type: SourceType
    role: str
    ledger_records: list[MerchantRecord] = field(default_factory=list)
    settlement_records: list[RazorpaySettlementRecord] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return len(self.ledger_records) + len(self.settlement_records)


def _value(row: dict, mapping: dict[str, str], canonical: str, default=None):
    column = mapping.get(canonical)
    if not column:
        return default
    value = row.get(column)
    return default if value in ("", None) else value


def _to_minor(amount: float | None, scale: str) -> int | None:
    if amount is None:
        return None
    return int(round(amount)) if scale == "minor" else int(round(amount * 100))


def map_rows(
    rows: list[dict],
    mapping: dict[str, str],
    source_type: SourceType,
    source_id: str,
    amount_scale: str = "major",
    debit_column: str | None = None,
    credit_column: str | None = None,
) -> MappedSource:
    """Map every row, keeping the failures.

    Amount sign carries meaning on a bank statement: a debit is a refund
    or a payout out, a credit is money in. Rather than reject negatives —
    which the domain model forbids — the magnitude becomes the amount and
    the sign is recorded as a refund, which is what a negative line
    actually represents on the settlement side.
    """
    mapped = MappedSource(source_id=source_id, source_type=source_type, role=source_type.role)

    for index, row in enumerate(rows, start=1):
        try:
            if debit_column and credit_column:
                # Split money-in / money-out columns: exactly one is
                # populated per line, and which one carries the sign.
                credit = parse_amount(row.get(credit_column))
                debit = parse_amount(row.get(debit_column))
                raw_amount = credit if credit else (-debit if debit else None)
            else:
                raw_amount = parse_amount(_value(row, mapping, "amount"))
            when = parse_date(_value(row, mapping, "date"))
            if raw_amount is None:
                raise ValueError("amount is missing or not a number")
            if when is None:
                raise ValueError("date is missing or unparseable")

            amount_minor = _to_minor(abs(raw_amount), amount_scale)
            is_outflow = raw_amount < 0

            reference = _value(row, mapping, "reference") or _value(row, mapping, "transaction_id")
            transaction_id = _value(row, mapping, "transaction_id") or reference or f"{source_id}-{index}"
            description = str(_value(row, mapping, "description", "") or "")
            counterparty = _value(row, mapping, "counterparty")
            if counterparty:
                description = f"{description} {counterparty}".strip()
            currency = str(_value(row, mapping, "currency", "INR") or "INR").upper()[:8]
            raw_status = str(_value(row, mapping, "status", "") or "").strip().lower()

            refund_amount = _to_minor(parse_amount(_value(row, mapping, "refund_amount")), amount_scale) or 0
            if is_outflow and not refund_amount:
                refund_amount = amount_minor

            if mapped.role == "LEDGER":
                status = _LEDGER_STATUS.get(raw_status, "captured")
                if refund_amount and status == "captured":
                    status = "partially_refunded" if refund_amount < amount_minor else "refunded"
                mapped.ledger_records.append(MerchantRecord(
                    order_id=str(transaction_id),
                    reference_id=str(reference) if reference else None,
                    amount_minor=amount_minor,
                    currency=currency,
                    order_date=when,
                    status=status,
                    refund_amount_minor=min(refund_amount, amount_minor),
                    description=description,
                ))
            else:
                fee = _to_minor(parse_amount(_value(row, mapping, "fee")), amount_scale) or 0
                tax = _to_minor(parse_amount(_value(row, mapping, "tax")), amount_scale) or 0
                refund = min(refund_amount, amount_minor)
                status = _SETTLEMENT_STATUS.get(raw_status, "settled")
                if refund and status == "settled":
                    status = "partially_refunded" if refund < amount_minor else "refunded"
                # If the file states a net amount, keep it. Deriving it
                # from gross minus fee and tax would make the arithmetic
                # check vacuous — it would be verifying a subtraction this
                # code had just performed, so a genuinely inconsistent
                # payout file could never fail it.
                stated_net = _to_minor(parse_amount(_value(row, mapping, "net_amount")), amount_scale)
                settled_on = parse_date(_value(row, mapping, "settlement_date")) or when
                mapped.settlement_records.append(RazorpaySettlementRecord(
                    payment_id=str(transaction_id),
                    order_reference=str(reference) if reference else "",
                    settlement_id=str(_value(row, mapping, "transaction_id") or transaction_id),
                    gross_amount_minor=amount_minor,
                    fee_minor=fee,
                    tax_minor=tax,
                    net_amount_minor=stated_net if stated_net is not None
                    else amount_minor - fee - tax - refund,
                    refund_amount_minor=refund,
                    order_date=when,
                    settlement_date=settled_on,
                    currency=currency,
                    status=status,
                    description=description,
                ))
        except Exception as exc:  # noqa: BLE001 — every malformed row degrades the same way
            mapped.rejected.append({
                "row": index,
                "error": f"{type(exc).__name__}: {exc}",
                "raw": {k: v for k, v in list(row.items())[:6]},
            })

    return mapped


def combine(sources: list[MappedSource]) -> tuple[list[MerchantRecord], list[RazorpaySettlementRecord], list[dict]]:
    """Fold several mapped sources into the two sides of one run.

    Several ledger sources (say orders plus an accounting export) are
    concatenated; so are several settlement sources (a gateway payout file
    plus a bank statement). Cross-source duplicates are left in place
    deliberately — two sources describing the same payment is a real
    finding for the claim-integrity pass to surface, not something to
    quietly de-duplicate here.
    """
    ledger: list[MerchantRecord] = []
    settlements: list[RazorpaySettlementRecord] = []
    rejected: list[dict] = []
    for source in sources:
        ledger.extend(source.ledger_records)
        settlements.extend(source.settlement_records)
        for row in source.rejected:
            rejected.append({**row, "source_id": source.source_id,
                             "source_type": source.source_type.value})
    return ledger, settlements, rejected
