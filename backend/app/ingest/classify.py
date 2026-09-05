"""
What kind of file is this, and who produced it — decided from the file's
contents, and said out loud.

Uploading a folder of month-end exports and being asked "which of these
is the bank statement?" for each one is the friction this removes. The
constraint is that removing it must not create a worse failure: a file
classified as a settlement when it is an order book puts the same money
on both sides of a reconciliation, and every record comes back clean.

So three things hold here.

*Content outranks names.* Column headers and the shape of the identifier
values inside the file carry almost all the weight. A filename can only
nudge; it can never on its own produce a confident answer, and when it is
the only evidence the classification says so and asks.

*Unknown providers still classify.* The provider table exists to put a
human name on a file ("Razorpay", "ICICI Bank"), not to gate support. A
CSV from a gateway nobody here has heard of still lands in
PAYMENT_GATEWAY on its column semantics, with `provider=None` and a
lower confidence — which is the honest description of what was worked
out.

*Low confidence blocks, it does not guess.* `needs_confirmation` is not
advisory. A source whose role was inferred rather than stated has to be
confirmed by a person before the run can execute.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from app.ingest.schema import (
    DetectedSchema, SourceType, detect_schema, parse_amount, parse_date,
)

# Below this the role was not established well enough to reconcile on.
CONFIDENCE_THRESHOLD = 0.65

# A filename is corroboration, never proof. Even a perfectly named file
# stays under the confirmation threshold on its own.
FILENAME_ONLY_CEILING = 0.45

# Rows scanned for value-shape evidence and for the date/amount ranges.
# Larger scans are reported as complete; a capped one is flagged sampled.
VALUE_SCAN_ROWS = 400
RANGE_SCAN_ROWS = 50_000

# Money-flow stage each source type binds to. PAYMENT_GATEWAY splits:
# a payments export and a settlement/payout export come from the same
# system but sit at different points in the flow.
STAGE_ORDERS = "ORDERS"
STAGE_GATEWAY = "PAYMENT_GATEWAY"
STAGE_SETTLEMENT = "SETTLEMENT"
STAGE_BANK = "BANK"
STAGE_ACCOUNTING = "ACCOUNTING"
STAGE_UNASSIGNED = "UNASSIGNED"

FLOW_STAGES = (STAGE_ORDERS, STAGE_GATEWAY, STAGE_SETTLEMENT, STAGE_BANK, STAGE_ACCOUNTING)

STAGE_LABELS = {
    STAGE_ORDERS: "Orders",
    STAGE_GATEWAY: "Payment gateway",
    STAGE_SETTLEMENT: "Settlement / payout",
    STAGE_BANK: "Bank",
    STAGE_ACCOUNTING: "Accounting",
    STAGE_UNASSIGNED: "Unassigned",
}


@dataclass
class Evidence:
    kind: str                       # "column" | "value" | "combination" | "filename"
    weight: float
    reason: str
    source_type: SourceType | None = None
    provider: str | None = None


@dataclass
class SourceClassification:
    source_type: SourceType
    confidence: float
    provider: str | None
    reasons: list[str]
    date_range: dict | None
    amount_range: dict | None
    currency: str | None
    suggested_role: str
    needs_confirmation: bool
    stage: str = STAGE_UNASSIGNED
    currencies: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    provider_confidence: float = 0.0
    evidence: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "detected_source_type": self.source_type.value,
            "detection_confidence": round(self.confidence, 3),
            "provider": self.provider,
            "provider_confidence": round(self.provider_confidence, 3),
            "reasons": self.reasons,
            "date_range": self.date_range,
            "amount_range": self.amount_range,
            "currency": self.currency,
            "currencies": self.currencies,
            "suggested_role": self.suggested_role,
            "needs_confirmation": self.needs_confirmation,
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "scores": {k: round(v, 3) for k, v in self.scores.items()},
            "evidence": self.evidence,
        }


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

# (pattern, source_type, weight, human reason)
COLUMN_SIGNALS: tuple[tuple[str, SourceType, float, str], ...] = (
    # Bank statement
    (r"^utr(\b|[_\s-]*(no|number|ref))?$", SourceType.BANK_STATEMENT, 1.4, "a UTR column is a bank transfer reference"),
    (r"^narration$", SourceType.BANK_STATEMENT, 1.4, "'narration' is bank-statement wording"),
    (r"^withdrawal(s|[_\s-]*amt|[_\s-]*amount)?$", SourceType.BANK_STATEMENT, 1.3, "a withdrawal column"),
    (r"^deposit(s|[_\s-]*amt|[_\s-]*amount)?$", SourceType.BANK_STATEMENT, 1.3, "a deposit column"),
    (r"(closing|running|book|available)[_\s-]*balance|^balance$", SourceType.BANK_STATEMENT, 1.1,
     "a running balance column only exists on a statement"),
    (r"^value[_\s-]*date$", SourceType.BANK_STATEMENT, 0.8, "'value date' is bank wording"),
    (r"^(cheque|chq)[_\s-]*(no|number)?$", SourceType.BANK_STATEMENT, 0.9, "a cheque number column"),
    (r"^ifsc([_\s-]*code)?$", SourceType.BANK_STATEMENT, 0.7, "an IFSC column"),
    (r"^account[_\s-]*(no|number)$", SourceType.BANK_STATEMENT, 0.6, "an account number column"),

    # Payment gateway / settlement
    (r"^settlement[_\s-]*(id|no|number)$", SourceType.PAYMENT_GATEWAY, 1.6, "a settlement id column"),
    (r"^payout[_\s-]*(id|no|number)$", SourceType.PAYMENT_GATEWAY, 1.6, "a payout id column"),
    (r"^settlement[_\s-]*(utr|amount|date|status)$", SourceType.PAYMENT_GATEWAY, 1.0, "settlement-side columns"),
    (r"^payout[_\s-]*(amount|date|status)$", SourceType.PAYMENT_GATEWAY, 1.0, "payout-side columns"),
    (r"^(razorpay[_\s-]*)?payment[_\s-]*id$", SourceType.PAYMENT_GATEWAY, 1.2, "a payment id column"),
    (r"^(mdr|commission([_\s-]*amount)?)$", SourceType.PAYMENT_GATEWAY, 0.9, "an MDR/commission column"),
    (r"^fee(s)?([_\s-]*amount)?$", SourceType.PAYMENT_GATEWAY, 0.7, "a gateway fee column"),
    (r"^(gross|net)[_\s-]*amount$", SourceType.PAYMENT_GATEWAY, 0.6, "gross/net amount split"),
    (r"^(arn|acquirer([_\s-]*ref)?)$", SourceType.PAYMENT_GATEWAY, 0.7, "an acquirer reference column"),
    (r"^payment[_\s-]*(method|mode)$", SourceType.PAYMENT_GATEWAY, 0.4, "a payment method column"),

    # Orders
    (r"^financial[_\s-]*status$", SourceType.ORDERS, 1.6, "'financial status' is an e-commerce order field"),
    (r"^fulfillment[_\s-]*status$", SourceType.ORDERS, 1.4, "a fulfilment status column"),
    (r"(^|[_\s-])sku$|^seller[_\s-]*sku$", SourceType.ORDERS, 1.4, "an SKU column"),
    (r"line[_\s-]*items?", SourceType.ORDERS, 1.4, "line-item columns"),
    (r"^(quantity|qty)$", SourceType.ORDERS, 0.8, "a quantity column"),
    (r"^(product|item)([_\s-]*name|[_\s-]*title)?$", SourceType.ORDERS, 0.8, "a product column"),
    (r"^customer[_\s-]*(email|name|id|phone)$", SourceType.ORDERS, 0.7, "customer detail columns"),
    (r"^shipping([_\s-]*\w+)?$", SourceType.ORDERS, 0.6, "shipping columns"),
    (r"^order[_\s-]*(date|status)$", SourceType.ORDERS, 0.5, "order date/status columns"),
    (r"^order[_\s-]*(id|no|number|reference)$", SourceType.ORDERS, 0.4, "an order id column"),
    (r"^(discount|coupon)([_\s-]*\w+)?$", SourceType.ORDERS, 0.4, "discount columns"),

    # Accounting
    (r"^voucher[_\s-]*(no|number|type|date)$", SourceType.ACCOUNTING, 1.6, "a voucher column"),
    (r"^ledger([_\s-]*name|[_\s-]*account)?$", SourceType.ACCOUNTING, 1.5, "a ledger name column"),
    (r"^journal([_\s-]*\w+)?$", SourceType.ACCOUNTING, 1.2, "a journal column"),
    (r"^(gl[_\s-]*)?account([_\s-]*code|[_\s-]*head|[_\s-]*name)$", SourceType.ACCOUNTING, 0.9, "an account code column"),
    (r"^particulars$", SourceType.ACCOUNTING, 0.7, "'particulars' is accounting wording"),
    (r"^(debit|credit)([_\s-]*amount)?$", SourceType.ACCOUNTING, 0.45, "paired debit/credit columns"),
    (r"^(dr|cr)([_\s-]*amount)?$", SourceType.ACCOUNTING, 0.4, "Dr/Cr columns"),
    (r"cost[_\s-]*cent(re|er)", SourceType.ACCOUNTING, 0.6, "a cost centre column"),
    (r"^(gstin|gst[_\s-]*no)$", SourceType.ACCOUNTING, 0.5, "a GSTIN column"),
)

# (pattern, source_type or None, provider or None, weight, reason)
VALUE_SIGNALS: tuple[tuple[str, SourceType | None, str | None, float, str], ...] = (
    (r"^pay_[A-Za-z0-9]{14}$", SourceType.PAYMENT_GATEWAY, "Razorpay", 1.8,
     "identifier values match Razorpay's pay_XXXXXXXXXXXXXX payment id"),
    (r"^setl_[A-Za-z0-9]{14}$", SourceType.PAYMENT_GATEWAY, "Razorpay", 1.6,
     "identifier values match Razorpay's setl_ settlement id"),
    (r"^rfnd_[A-Za-z0-9]{14}$", SourceType.PAYMENT_GATEWAY, "Razorpay", 1.2,
     "identifier values match Razorpay's rfnd_ refund id"),
    (r"^order_[A-Za-z0-9]{14}$", None, "Razorpay", 1.2,
     "identifier values match Razorpay's order_ order id"),
    (r"^rzp_(test|live)_[A-Za-z0-9]+$", None, "Razorpay", 1.0, "a Razorpay key prefix appears in the data"),
    (r"^\d{3}-\d{7}-\d{7}$", SourceType.ORDERS, "Amazon", 1.8, "values match Amazon's 3-7-7 order id"),
    (r"^OD\d{15,}$", SourceType.ORDERS, "Flipkart", 1.5, "values match Flipkart's OD order id"),
    (r"^[A-Z]{4}0[A-Z0-9]{6}$", SourceType.BANK_STATEMENT, None, 1.0, "values are IFSC codes"),
    (r"(^|[/\s-])(UPI|NEFT|IMPS|RTGS|NACH|ECS|ATM|POS|CHQ|EMI)([/\s-]|$)", SourceType.BANK_STATEMENT, None, 1.0,
     "descriptions carry bank transfer channel codes (UPI/NEFT/IMPS/…)"),
    (r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]{2}$", SourceType.ACCOUNTING, None, 0.5, "values are GSTINs"),
)

# Providers recognised by their own column names. Absence from this table
# is not absence of support — it only means the file is classified by
# semantics with provider=None.
PROVIDER_COLUMN_SIGNALS: tuple[tuple[str, str, float, str], ...] = (
    (r"^razorpay[_\s-]*(payment|order|settlement)[_\s-]*id$", "Razorpay", 1.6, "Razorpay-prefixed column names"),
    (r"^(tracking[_\s-]*id|bank[_\s-]*ref[_\s-]*no)$", "CCAvenue", 1.0, "CCAvenue column names"),
    (r"^(billdesk[_\s-]*\w+|txn[_\s-]*reference[_\s-]*no)$", "BillDesk", 1.0, "BillDesk column names"),
    (r"^(lineitem[_\s-]*\w+|financial[_\s-]*status|fulfillment[_\s-]*status)$", "Shopify", 1.0,
     "Shopify order-export column names"),
    (r"^(_order_key|woocommerce[_\s-]*\w+|order[_\s-]*number)$", "WooCommerce", 0.7, "WooCommerce column names"),
    (r"^(amazon[_\s-]*order[_\s-]*id|asin|order[_\s-]*item[_\s-]*id|settlement[_\s-]*id[_\s-]*amazon)$",
     "Amazon", 1.2, "Amazon settlement/order column names"),
    (r"^(fsn|flipkart[_\s-]*\w+)$", "Flipkart", 1.2, "Flipkart column names"),
    (r"^(voucher[_\s-]*(no|number|type)|ledger[_\s-]*name)$", "Tally", 0.8, "Tally voucher/ledger column names"),
    (r"^(zoho[_\s-]*\w+|entry[_\s-]*number)$", "Zoho Books", 0.7, "Zoho Books column names"),
    (r"^(quickbooks[_\s-]*\w+|memo[_\s-]*description|split)$", "QuickBooks", 0.7, "QuickBooks column names"),
)

# Indian banks, recognised by IFSC prefix (real evidence, in the data) and
# by filename token (weak). The list is illustrative, not a whitelist —
# an unlisted bank still classifies as BANK_STATEMENT on its columns.
BANK_IFSC_PREFIXES: dict[str, str] = {
    "HDFC": "HDFC Bank", "ICIC": "ICICI Bank", "SBIN": "State Bank of India", "UTIB": "Axis Bank",
    "KKBK": "Kotak Mahindra Bank", "YESB": "Yes Bank", "INDB": "IndusInd Bank",
    "PUNB": "Punjab National Bank", "BARB": "Bank of Baroda", "CNRB": "Canara Bank",
    "IDFB": "IDFC First Bank", "RATN": "RBL Bank", "FDRL": "Federal Bank", "IBKL": "IDBI Bank",
    "UBIN": "Union Bank of India", "BKID": "Bank of India", "CBIN": "Central Bank of India",
    "IDIB": "Indian Bank", "AUBL": "AU Small Finance Bank", "BDBL": "Bandhan Bank",
    "DBSS": "DBS Bank", "CITI": "Citibank", "HSBC": "HSBC", "SCBL": "Standard Chartered",
}

BANK_NAME_TOKENS: dict[str, str] = {
    "hdfc": "HDFC Bank", "icici": "ICICI Bank", "sbi": "State Bank of India",
    "statebank": "State Bank of India", "axis": "Axis Bank", "kotak": "Kotak Mahindra Bank",
    "yesbank": "Yes Bank", "indusind": "IndusInd Bank", "pnb": "Punjab National Bank",
    "punjabnational": "Punjab National Bank", "bob": "Bank of Baroda", "baroda": "Bank of Baroda",
    "canara": "Canara Bank", "idfc": "IDFC First Bank", "rbl": "RBL Bank", "federal": "Federal Bank",
    "idbi": "IDBI Bank", "unionbank": "Union Bank of India", "bandhan": "Bandhan Bank",
    "aubank": "AU Small Finance Bank", "citibank": "Citibank", "hsbc": "HSBC",
    "standardchartered": "Standard Chartered", "dbs": "DBS Bank",
}

PROVIDER_NAME_TOKENS: dict[str, str] = {
    "ccavenue": "CCAvenue", "billdesk": "BillDesk",
    "shopify": "Shopify", "woocommerce": "WooCommerce", "woo": "WooCommerce", "amazon": "Amazon",
    "flipkart": "Flipkart", "tally": "Tally", "zoho": "Zoho Books", "quickbooks": "QuickBooks",
    "qbo": "QuickBooks", "instamojo": "Instamojo", "stripe": "Stripe", "payphi": "PayPhi",
    "easebuzz": "Easebuzz",
}

FILENAME_TYPE_TOKENS: tuple[tuple[str, SourceType, float], ...] = (
    ("settlement", SourceType.PAYMENT_GATEWAY, 0.5),
    ("settlements", SourceType.PAYMENT_GATEWAY, 0.5),
    ("payout", SourceType.PAYMENT_GATEWAY, 0.5),
    ("payouts", SourceType.PAYMENT_GATEWAY, 0.5),
    ("gateway", SourceType.PAYMENT_GATEWAY, 0.4),
    ("payments", SourceType.PAYMENT_GATEWAY, 0.3),
    ("statement", SourceType.BANK_STATEMENT, 0.5),
    ("bank", SourceType.BANK_STATEMENT, 0.5),
    ("passbook", SourceType.BANK_STATEMENT, 0.5),
    ("account", SourceType.BANK_STATEMENT, 0.2),
    ("orders", SourceType.ORDERS, 0.5),
    ("order", SourceType.ORDERS, 0.4),
    ("sales", SourceType.ORDERS, 0.4),
    ("invoices", SourceType.ORDERS, 0.3),
    ("ledger", SourceType.ACCOUNTING, 0.5),
    ("journal", SourceType.ACCOUNTING, 0.5),
    ("books", SourceType.ACCOUNTING, 0.4),
    ("tally", SourceType.ACCOUNTING, 0.4),
    ("gl", SourceType.ACCOUNTING, 0.3),
    ("trialbalance", SourceType.ACCOUNTING, 0.5),
)

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _normalize(name: str) -> str:
    return (name or "").strip().lower()


def _filename_tokens(filename: str) -> list[str]:
    stem = _normalize(filename).rsplit(".", 1)[0]
    return [t for t in _TOKEN_SPLIT.split(stem) if t]


# ---------------------------------------------------------------------------
# Evidence gathering
# ---------------------------------------------------------------------------

def _column_evidence(columns: list[str]) -> list[Evidence]:
    normalized = [_normalize(c) for c in columns]
    out: list[Evidence] = []
    seen: set[tuple[str, str]] = set()
    for pattern, source_type, weight, reason in COLUMN_SIGNALS:
        hits = [c for c in normalized if re.search(pattern, c)]
        if not hits:
            continue
        key = (source_type.value, reason)
        if key in seen:
            continue
        seen.add(key)
        out.append(Evidence("column", weight, f"{reason} ({', '.join(hits[:3])})", source_type=source_type))
    for pattern, provider, weight, reason in PROVIDER_COLUMN_SIGNALS:
        hits = [c for c in normalized if re.search(pattern, c)]
        if hits:
            out.append(Evidence("column", weight, f"{reason} ({', '.join(hits[:3])})", provider=provider))
    return out


def _combination_evidence(columns: list[str]) -> list[Evidence]:
    """Rules that only make sense over the whole column set.

    `particulars` with `debit`/`credit` is a day book; the same three
    columns alongside a running balance is a bank statement. Neither
    header decides it alone, which is why these are scored separately
    from the per-column signals.
    """
    normalized = {_normalize(c) for c in columns}

    def has(*patterns: str) -> bool:
        return all(any(re.search(p, c) for c in normalized) for p in patterns)

    out: list[Evidence] = []
    bankish = has(r"^narration$") or has(r"^withdrawal") or has(r"^deposit") or has(r"balance") or has(r"^utr")
    if has(r"^particulars$", r"^debit", r"^credit") and not bankish:
        out.append(Evidence("combination", 1.2,
                            "particulars with paired debit/credit columns and no bank balance — a day book",
                            source_type=SourceType.ACCOUNTING))
    if has(r"^debit", r"^credit") and has(r"balance"):
        out.append(Evidence("combination", 0.9,
                            "debit and credit alongside a running balance — a bank statement",
                            source_type=SourceType.BANK_STATEMENT))
    if (has(r"^withdrawal") or has(r"^deposit")) and has(r"balance"):
        out.append(Evidence("combination", 0.8,
                            "withdrawal/deposit alongside a running balance",
                            source_type=SourceType.BANK_STATEMENT))
    if has(r"settlement|payout") and has(r"^(gross|net)[_\s-]*amount$|^fee|^mdr|^commission"):
        out.append(Evidence("combination", 0.8,
                            "settlement identifiers with a fee/net breakdown — a gateway payout file",
                            source_type=SourceType.PAYMENT_GATEWAY))
    return out


def _value_evidence(columns: list[str], rows: list[dict]) -> list[Evidence]:
    """Identifier shapes actually present in the data.

    Stronger than a header, because a column called `payment_id` says
    what somebody named it and a column full of `pay_JK4hV2m8Qw1XyZ`
    says which system minted it.
    """
    sample = rows[:VALUE_SCAN_ROWS]
    if not sample:
        return []
    out: list[Evidence] = []
    per_column: dict[str, list[str]] = {
        column: [str(r.get(column, "") or "").strip() for r in sample] for column in columns
    }

    for pattern, source_type, provider, weight, reason in VALUE_SIGNALS:
        compiled = re.compile(pattern)
        best_column, best_ratio = None, 0.0
        for column, values in per_column.items():
            usable = [v for v in values if v]
            if not usable:
                continue
            ratio = sum(1 for v in usable if compiled.search(v)) / len(usable)
            if ratio > best_ratio:
                best_column, best_ratio = column, ratio
        # A shape has to dominate its column to count. A single stray
        # match is a coincidence, not a provider.
        if best_ratio >= 0.5 and best_column is not None:
            out.append(Evidence("value", weight * min(1.0, best_ratio + 0.2),
                                f"{reason} — {best_ratio:.0%} of '{best_column}'",
                                source_type=source_type, provider=provider))

    # IFSC prefixes name the actual bank, which is a fact in the file
    # rather than a guess from the filename.
    ifsc = re.compile(r"^([A-Z]{4})0[A-Z0-9]{6}$")
    prefixes: dict[str, int] = {}
    for values in per_column.values():
        for value in values:
            found = ifsc.match(value)
            if found:
                prefixes[found.group(1)] = prefixes.get(found.group(1), 0) + 1
    if prefixes:
        top = max(prefixes, key=lambda k: prefixes[k])
        bank = BANK_IFSC_PREFIXES.get(top)
        if bank:
            out.append(Evidence("value", 1.2, f"IFSC codes in the data begin {top}0 — {bank}",
                                source_type=SourceType.BANK_STATEMENT, provider=bank))
    return out


def _filename_evidence(filename: str) -> list[Evidence]:
    tokens = _filename_tokens(filename)
    joined = "".join(tokens)
    out: list[Evidence] = []
    for token, source_type, weight in FILENAME_TYPE_TOKENS:
        if token in tokens:
            out.append(Evidence("filename", weight, f"filename contains '{token}'", source_type=source_type))
    for token, provider in PROVIDER_NAME_TOKENS.items():
        if token in tokens or (len(token) > 4 and token in joined):
            out.append(Evidence("filename", 0.6, f"filename mentions '{token}'", provider=provider))
    for token, bank in BANK_NAME_TOKENS.items():
        if token in tokens or (len(token) > 3 and token in joined):
            out.append(Evidence("filename", 0.6, f"filename mentions '{token}'",
                                provider=bank, source_type=SourceType.BANK_STATEMENT))
    return out


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------

def _amount_columns(detected: DetectedSchema) -> list[str]:
    if detected.debit_column and detected.credit_column:
        return [detected.debit_column, detected.credit_column]
    column = detected.mapping.get("amount")
    return [column] if column else []


def summarise_ranges(detected: DetectedSchema, rows: list[dict]) -> tuple[dict | None, dict | None, str | None, list[str]]:
    """Date span, amount span and currency, from the mapped columns only.

    Derived from what detection actually resolved, so a file whose amount
    column is still unmapped reports no amount range instead of a number
    pulled from whichever column looked numeric.
    """
    scanned = rows[:RANGE_SCAN_ROWS]
    sampled = len(rows) > RANGE_SCAN_ROWS

    date_range = None
    date_column = detected.mapping.get("date")
    if date_column:
        parsed = [parse_date(r.get(date_column)) for r in scanned]
        parsed = [p for p in parsed if p is not None]
        if parsed:
            date_range = {
                "from": min(parsed).date().isoformat(),
                "to": max(parsed).date().isoformat(),
                "column": date_column,
                "parsed_rows": len(parsed),
                "sampled": sampled,
            }

    amount_range = None
    columns = _amount_columns(detected)
    if columns:
        values: list[float] = []
        for row in scanned:
            for column in columns:
                amount = parse_amount(row.get(column))
                if amount is not None:
                    values.append(abs(amount))
        values = [v for v in values if v != 0] or values
        if values:
            scale = detected.amount_scale
            low, high = min(values), max(values)
            amount_range = {
                "min": round(low / 100, 2) if scale == "minor" else round(low, 2),
                "max": round(high / 100, 2) if scale == "minor" else round(high, 2),
                "min_minor": int(round(low if scale == "minor" else low * 100)),
                "max_minor": int(round(high if scale == "minor" else high * 100)),
                "scale": scale,
                "columns": columns,
                "sampled": sampled,
            }

    currency, currencies = None, []
    currency_column = detected.mapping.get("currency")
    if currency_column:
        seen = {str(r.get(currency_column, "")).strip().upper() for r in scanned}
        currencies = sorted(c for c in seen if c)
        if len(currencies) == 1:
            currency = currencies[0]
    return date_range, amount_range, currency, currencies


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _stage_for(source_type: SourceType, columns: list[str]) -> str:
    if source_type is SourceType.ORDERS:
        return STAGE_ORDERS
    if source_type is SourceType.BANK_STATEMENT:
        return STAGE_BANK
    if source_type is SourceType.ACCOUNTING:
        return STAGE_ACCOUNTING
    if source_type is SourceType.PAYMENT_GATEWAY:
        normalized = [_normalize(c) for c in columns]
        settlementish = any(re.search(r"settlement|payout", c) for c in normalized)
        return STAGE_SETTLEMENT if settlementish else STAGE_GATEWAY
    return STAGE_UNASSIGNED


def _confidence(top: float, second: float) -> float:
    """How sure this is, as a function of how much evidence there is and
    how clearly it beats the runner-up.

    Both halves matter. A pile of evidence that supports two readings
    equally is not a confident classification, and a single clean
    identifier match with nothing arguing against it should not be
    treated as certainty either.
    """
    if top <= 0:
        return 0.0
    strength = 1 - math.exp(-top / 1.2)
    margin = (top - second) / top
    separation = 0.5 + 0.5 * max(0.0, margin)
    return min(0.97, strength * separation)


def classify_source(
    filename: str,
    columns: list[str],
    rows: list[dict],
    detected: DetectedSchema | None = None,
) -> SourceClassification:
    """What this file is, how sure that is, and why — in that order."""
    detected = detected or detect_schema(columns, rows)

    evidence = (
        _column_evidence(columns)
        + _combination_evidence(columns)
        + _value_evidence(columns, rows)
        + _filename_evidence(filename)
    )

    scores: dict[SourceType, float] = {t: 0.0 for t in SourceType if t is not SourceType.OTHER}
    provider_scores: dict[str, float] = {}
    provider_kinds: dict[str, set[str]] = {}
    for item in evidence:
        if item.source_type is not None and item.source_type is not SourceType.OTHER:
            scores[item.source_type] += item.weight
        if item.provider:
            provider_scores[item.provider] = provider_scores.get(item.provider, 0.0) + item.weight
            provider_kinds.setdefault(item.provider, set()).add(item.kind)

    content_evidence = [e for e in evidence if e.kind != "filename"]
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_type, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0

    reasons: list[str] = []
    for item in sorted(evidence, key=lambda e: e.weight, reverse=True)[:8]:
        target = item.source_type.value if item.source_type else (item.provider or "provider")
        reasons.append(f"{item.reason} → {target}")

    if top_score <= 0:
        source_type = SourceType.OTHER
        confidence = 0.0
        reasons.append("no column, value or filename signal identified this file — it needs to be told what it is")
    else:
        source_type = top_type
        confidence = _confidence(top_score, second_score)
        if not content_evidence:
            confidence = min(confidence, FILENAME_ONLY_CEILING)
            reasons.append(
                "the filename is the only evidence — a filename is never enough on its own, "
                "so this needs confirming"
            )

    # Provider is a label on the file, not a gate. It is only asserted
    # when something inside the file supports it; a filename-only match
    # is reported as such rather than presented as identification.
    provider = None
    provider_confidence = 0.0
    if provider_scores:
        candidate = max(provider_scores, key=lambda k: provider_scores[k])
        kinds = provider_kinds.get(candidate, set())
        provider = candidate
        provider_confidence = _confidence(provider_scores[candidate],
                                          max((v for k, v in provider_scores.items() if k != candidate), default=0.0))
        if kinds == {"filename"}:
            provider_confidence = min(provider_confidence, FILENAME_ONLY_CEILING)
            reasons.append(f"'{candidate}' comes from the filename only — confirm it")

    needs_confirmation = (
        confidence < CONFIDENCE_THRESHOLD
        or source_type is SourceType.OTHER
        or bool(detected.unmapped_required)
    )
    if detected.unmapped_required:
        reasons.append(
            "required column(s) still unmapped: " + ", ".join(detected.unmapped_required)
        )

    date_range, amount_range, currency, currencies = summarise_ranges(detected, rows)

    return SourceClassification(
        source_type=source_type,
        confidence=confidence,
        provider=provider,
        reasons=reasons,
        date_range=date_range,
        amount_range=amount_range,
        currency=currency,
        currencies=currencies,
        suggested_role=source_type.role,
        needs_confirmation=needs_confirmation,
        stage=_stage_for(source_type, columns),
        scores={t.value: s for t, s in scores.items()},
        provider_confidence=provider_confidence,
        evidence=[{"kind": e.kind, "weight": round(e.weight, 3), "reason": e.reason,
                   "source_type": e.source_type.value if e.source_type else None,
                   "provider": e.provider} for e in evidence],
    )
