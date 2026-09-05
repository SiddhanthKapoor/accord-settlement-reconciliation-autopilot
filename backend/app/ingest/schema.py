"""
Source-agnostic CSV ingestion.

The reconciliation engine is not actually Razorpay-shaped: it compares two
sides on amount, reference, date and description, and none of that cares
where the rows came from. What was gateway-specific was only the *loading*
— records arrived as pre-shaped JSONL fixtures.

This module is the seam. Any CSV becomes one of two canonical roles the
engine already understands:

    LEDGER      what the business believes happened — orders, invoices,
                an accounting export
    SETTLEMENT  what happened to the money — a gateway payout file, a bank
                statement

The engine is unchanged. A bank statement is reconciled against an
accounting export by the same code that reconciles a gateway payout
against an order book, because after mapping they are the same shape.

On schema detection: it guesses, and it reports how confident it is. A
column it cannot place is surfaced for the user to map rather than
silently dropped or silently guessed. Reconciliation software that
quietly mis-reads an amount column is worse than software that asks.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

# Bank statements routinely split money in and money out across two
# columns instead of signing one. Detected as a pair so the sign is
# reconstructed rather than half the statement being read as inflow.
DEBIT_PATTERNS = (r"^withdrawal(s)?$", r"^debit(s)?$", r"^paid[_\s-]*out$", r"^money[_\s-]*out$", r"^dr$")
CREDIT_PATTERNS = (r"^deposit(s)?$", r"^credit(s)?$", r"^paid[_\s-]*in$", r"^money[_\s-]*in$", r"^cr$")

CANONICAL_FIELDS = (
    "transaction_id",
    "net_amount",
    "reference",
    "amount",
    "currency",
    "date",
    "settlement_date",
    "description",
    "status",
    "fee",
    "tax",
    "refund_amount",
    "counterparty",
)

REQUIRED_FIELDS = ("amount", "date")


class SourceType(str, Enum):
    """What kind of file this is, and therefore which side it belongs on."""

    PAYMENT_GATEWAY = "PAYMENT_GATEWAY"
    BANK_STATEMENT = "BANK_STATEMENT"
    ACCOUNTING = "ACCOUNTING"
    ORDERS = "ORDERS"
    OTHER = "OTHER"

    @property
    def role(self) -> str:
        """Which side of the reconciliation this source populates.

        A gateway payout and a bank statement both describe money that
        actually moved; orders and accounting exports describe what the
        business expected. That is the only distinction the engine needs.
        """
        return "SETTLEMENT" if self in (SourceType.PAYMENT_GATEWAY, SourceType.BANK_STATEMENT) else "LEDGER"


# Header synonyms, ordered so that a more specific match wins. Deliberately
# conservative: a header that matches nothing here becomes a question for
# the user rather than a guess.
_HEADER_PATTERNS: dict[str, tuple[str, ...]] = {
    "settlement_date": (
        r"settle(ment)?[_\s-]*date", r"payout[_\s-]*date", r"value[_\s-]*date", r"credit(ed)?[_\s-]*date",
    ),
    "transaction_id": (
        r"^(payment|txn|transaction|payout|entry)[_\s-]*(id|no|number)?$",
        r"^id$", r"^payment[_\s-]*id$", r"^razorpay[_\s-]*payment[_\s-]*id$",
        # An order id is this file's own key only when the file also
        # carries a stronger cross-system reference. Deliberately weaker
        # than the reference reading so it loses that contest by default
        # and wins only when nothing better claims the reference slot.
        (r"^order[_\s-]*(id|no|number)$", 0.70),
    ),
    "reference": (
        r"^(invoice|bill|receipt)[_\s-]*(ref(erence)?|no|id|number)?$",
        r"(order|invoice|merchant|external|bill)[_\s-]*(id|ref(erence)?|no|number)",
        r"^ref(erence)?[_\s-]*(no|id|num(ber)?)?$", r"^order[_\s-]*reference$",
        r"^utr$", r"^rrn$", r"^cheque[_\s-]*(no|number)$",
    ),
    "amount": (
        r"^(gross[_\s-]*)?amount$", r"amount[_\s-]*(inr|usd|minor|paise|cents)?$",
        r"^(debit|credit|value|total|txn[_\s-]*amount)$", r"^amt$",
    ),
    "net_amount": (r"^net([_\s-]*amount)?$", r"^settled[_\s-]*amount$", r"^payout[_\s-]*amount$",
                   r"^amount[_\s-]*settled$"),
    "fee": (r"^fee(s)?$", r"commission", r"mdr", r"charge(s)?"),
    "tax": (r"^tax$", r"^gst$", r"^vat$"),
    "refund_amount": (r"refund[_\s-]*(amount|amt|value)?$", r"^reversal[_\s-]*amount$"),
    "currency": (r"^curr(ency)?$", r"^ccy$"),
    "date": (
        r"^(transaction|txn|order|payment|posting|created|booking)[_\s-]*(date|at|time|on)$",
        r"^date$", r"^timestamp$", r"^created[_\s-]*at$",
    ),
    "description": (
        r"^(description|narration|particulars|details|remarks|memo|note(s)?|narrative)$",
        r"transaction[_\s-]*(description|details)", r"^label$",
    ),
    "status": (r"^status$", r"^state$", r"payment[_\s-]*status"),
    "counterparty": (
        r"^(customer|merchant|payee|payer|vendor|beneficiary|counterparty)([_\s-]*name)?$",
        r"^name$",
    ),
}

_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y",
    "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M",
)

_AMOUNT_CLEAN = re.compile(r"[^0-9.\-]")


@dataclass
class ColumnGuess:
    column: str
    canonical: str | None
    confidence: float
    reason: str
    samples: list[str] = field(default_factory=list)


@dataclass
class DetectedSchema:
    columns: list[str]
    mapping: dict[str, str]              # canonical -> column
    guesses: list[ColumnGuess]
    row_count: int
    unmapped_required: list[str]
    amount_scale: str                    # "major" | "minor"
    sample_rows: list[dict] = field(default_factory=list)
    debit_column: str | None = None
    credit_column: str | None = None

    @property
    def needs_user_input(self) -> bool:
        return bool(self.unmapped_required)


def sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def parse_csv(text: str, limit: int | None = None) -> tuple[list[str], list[dict]]:
    """Rows as dicts, with a bounded read so an accidental 2GB upload cannot
    take the process down."""
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text), delimiter=sniff_delimiter(text))
    columns = [c.strip() for c in (reader.fieldnames or [])]
    rows: list[dict] = []
    for i, row in enumerate(reader):
        if limit is not None and i >= limit:
            break
        rows.append({(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()})
    return columns, rows


def _matches_any(column: str, patterns: tuple) -> bool:
    normalized = column.strip().lower()
    return any(re.search(p if isinstance(p, str) else p[0], normalized) for p in patterns)


def _header_candidates(column: str) -> list[tuple[str, float, str]]:
    """Every canonical field this header could plausibly be, best first.

    A header often fits more than one slot, and which reading is right
    depends on what else the file contains. `order_id` is the reference
    in a gateway export and the primary key in an order book — the same
    string, two meanings, decided by whether a better reference column
    exists alongside it. Returning all readings lets the assignment below
    settle that globally instead of by column order.
    """
    normalized = column.strip().lower()
    out: list[tuple[str, float, str]] = []
    for canonical, patterns in _HEADER_PATTERNS.items():
        best: tuple[str, float, str] | None = None
        for entry in patterns:
            pattern, override = entry if isinstance(entry, tuple) else (entry, None)
            if re.search(pattern, normalized):
                anchored = pattern.startswith("^") and pattern.endswith("$")
                confidence = override if override is not None else (0.95 if anchored else 0.8)
                if best is None or confidence > best[1]:
                    best = (canonical, confidence, f"header matches /{pattern}/")
        if best:
            out.append(best)
    return sorted(out, key=lambda c: c[1], reverse=True)


def looks_like_amount(values: list[str]) -> bool:
    usable = [v for v in values if v not in ("", None)][:20]
    if not usable:
        return False
    parsed = sum(1 for v in usable if parse_amount(v) is not None)
    return parsed / len(usable) >= 0.8


def looks_like_date(values: list[str]) -> bool:
    usable = [v for v in values if v not in ("", None)][:20]
    if not usable:
        return False
    parsed = sum(1 for v in usable if parse_date(v) is not None)
    return parsed / len(usable) >= 0.8


def parse_amount(value) -> float | None:
    """Amounts arrive as '1,234.56', '₹1234.56', '(45.00)' for negatives,
    or already numeric. Anything else is not an amount."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")")
    cleaned = _AMOUNT_CLEAN.sub("", text)
    if cleaned in ("", "-", "."):
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    return -amount if negative else amount


def parse_date(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    if text.isdigit() and len(text) == 10:          # unix seconds
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    return None


def detect_amount_scale(values: list[str]) -> str:
    """Whether an amount column is already in minor units.

    A gateway export usually gives paise as integers; a bank statement
    gives rupees with decimals. Guessing wrong is a 100x error, so the
    rule is deliberately narrow: only whole numbers with no decimal point
    anywhere are treated as minor units, and the choice is reported so a
    user can override it.
    """
    usable = [str(v) for v in values if v not in ("", None)][:50]
    if not usable:
        return "major"
    if any("." in v for v in usable):
        return "major"
    parsed = [parse_amount(v) for v in usable]
    parsed = [p for p in parsed if p is not None]
    if not parsed:
        return "major"
    return "minor" if all(float(p).is_integer() for p in parsed) else "major"


def detect_schema(columns: list[str], rows: list[dict]) -> DetectedSchema:
    """Guess which column is which, and say how sure it is.

    Header names decide first because they are the strongest signal. Where
    a header says nothing, column *content* is used to fill a required
    field that is still missing — but only for amount and date, where the
    content is distinctive enough to be worth trusting.
    """
    guesses: list[ColumnGuess] = []
    mapping: dict[str, str] = {}

    debit_column = next((c for c in columns if _matches_any(c, DEBIT_PATTERNS)), None)
    credit_column = next((c for c in columns if _matches_any(c, CREDIT_PATTERNS)), None)
    paired_amounts = bool(debit_column and credit_column)

    samples = {c: [v for v in (str(r.get(c, "")) for r in rows[:50]) if v][:3] for c in columns}
    resolved: dict[str, tuple[str, float, str]] = {}   # column -> (canonical, conf, reason)

    # Highest-confidence pairing wins globally, then both the column and
    # the canonical field are consumed. This is what makes `order_id` fall
    # through to transaction_id when an `invoice_ref` column outbids it
    # for the reference slot — first-come assignment got that backwards
    # and silently reconciled an order book against nothing.
    proposals: list[tuple[float, str, str, str]] = []
    for column in columns:
        if paired_amounts and column in (debit_column, credit_column):
            continue
        for canonical, confidence, reason in _header_candidates(column):
            proposals.append((confidence, column, canonical, reason))
    proposals.sort(key=lambda p: (-p[0], p[1]))

    for confidence, column, canonical, reason in proposals:
        if column in resolved or canonical in mapping:
            continue
        mapping[canonical] = column
        resolved[column] = (canonical, confidence, reason)

    for column in columns:
        values = samples[column]
        if paired_amounts and column in (debit_column, credit_column):
            side = "money out" if column == debit_column else "money in"
            guesses.append(ColumnGuess(column, "amount", 0.9,
                                       f"paired debit/credit column ({side})", samples=values))
            mapping.setdefault("amount", credit_column)
            continue
        if column in resolved:
            canonical, confidence, reason = resolved[column]
            guesses.append(ColumnGuess(column, canonical, confidence, reason, samples=values))
        else:
            taken = [c for c, _, _ in _header_candidates(column) if c in mapping]
            reason = (f"'{mapping[taken[0]]}' is a better match for {taken[0]}"
                      if taken else "no known synonym for this header")
            guesses.append(ColumnGuess(column, None, 0.0, reason, samples=values))

    # Content-based rescue, only for required fields the headers missed.
    for required in REQUIRED_FIELDS:
        if required in mapping:
            continue
        for guess in guesses:
            if guess.canonical is not None:
                continue
            values = [str(r.get(guess.column, "")) for r in rows[:50]]
            if required == "amount" and looks_like_amount(values):
                guess.canonical, guess.confidence = "amount", 0.6
                guess.reason = "values parse as amounts"
                mapping["amount"] = guess.column
                break
            if required == "date" and looks_like_date(values):
                guess.canonical, guess.confidence = "date", 0.6
                guess.reason = "values parse as dates"
                mapping["date"] = guess.column
                break

    # A statement whose only date column is the value date still has a
    # transaction date — they are the same event on a bank line.
    if "date" not in mapping and "settlement_date" in mapping:
        mapping["date"] = mapping["settlement_date"]

    amount_scale = "major"
    if "amount" in mapping:
        scale_source = [str(r.get(mapping["amount"], "")) for r in rows[:50]]
        if paired_amounts:
            scale_source += [str(r.get(debit_column, "")) for r in rows[:50]]
        amount_scale = detect_amount_scale(scale_source)

    return DetectedSchema(
        columns=columns,
        mapping=mapping,
        guesses=guesses,
        row_count=len(rows),
        unmapped_required=[f for f in REQUIRED_FIELDS if f not in mapping],
        amount_scale=amount_scale,
        sample_rows=rows[:5],
        debit_column=debit_column if paired_amounts else None,
        credit_column=credit_column if paired_amounts else None,
    )
