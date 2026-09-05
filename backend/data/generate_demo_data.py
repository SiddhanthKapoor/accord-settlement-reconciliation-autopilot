"""
Curated demo data — fixed, not random.

Every row here is written by hand so the demo shows the same thing every
time. A demo that depends on generator output is a demo that eventually
surprises you on stage.

Three files, three different schemas on purpose:

    orders.csv            an order book        (ledger side)
    gateway_payouts.csv   a gateway export     (settlement side)
    bank_statement.csv    a bank statement     (settlement side, with
                                                truncated narration and
                                                split debit/credit columns)

The set is built to demonstrate, in order:

  1. ordinary deterministic matches that need no model at all
  2. a reference reformatted between systems, still resolved deterministically
  3. a genuinely semantic match — bank narration is truncated beyond
     recognition and no identifier survives
  4. a same-amount collision that must be REFUSED, not matched
  5. a settlement that has not happened yet (pending, not missing)
  6. a genuinely missing settlement
  7. a fee/tax arithmetic exception
  8. a currency mismatch
  9. two orders competing for one settlement (double-claim)

Usage:
    python backend/data/generate_demo_data.py
"""

from __future__ import annotations

import csv
from pathlib import Path

OUT_DIR = Path(__file__).parent / "demo"

# (order_id, reference, amount_rupees, date, description, status)
ORDERS = [
    # 1. ordinary matches — reference matches exactly on the gateway side
    ("ORD-5001", "INV-2048", "12500.00", "2026-03-01", "Annual cloud platform subscription", "captured"),
    ("ORD-5002", "INV-2049", "4300.50", "2026-03-01", "SoundMax Pro headphones", "captured"),
    ("ORD-5003", "INV-2050", "899.00", "2026-03-02", "Starter plan monthly", "captured"),
    ("ORD-5004", "INV-2051", "24999.00", "2026-03-02", "Enterprise onboarding fee", "captured"),
    ("ORD-5005", "INV-2052", "1750.00", "2026-03-03", "Priority support add-on", "captured"),
    ("ORD-5006", "INV-2053", "6300.00", "2026-03-03", "Team seats x3 quarterly", "captured"),
    ("ORD-5007", "INV-2054", "15400.00", "2026-03-04", "Data export service annual", "captured"),
    ("ORD-5008", "INV-2055", "2200.00", "2026-03-04", "Widget bundle standard", "captured"),

    # 2. reference reformatted between systems — deterministic, no model
    ("ORD-5009", "INV/2056/A", "8750.00", "2026-03-05", "Consulting session block", "captured"),

    # 3. semantic: bank narration truncated, no identifier survives
    ("ORD-5010", "INV-2057", "31200.00", "2026-03-05", "Annual cloud platform renewal - Northwind Retail", "captured"),

    # 4. same-amount collision — an unrelated bank credit shares the amount
    ("ORD-5011", "INV-2058", "5000.00", "2026-03-06", "Gift card purchase", "captured"),

    # 5. pending — captured today, settlement not due yet
    ("ORD-5012", "INV-2059", "9400.00", "2026-03-12", "Express shipping add-on", "captured"),

    # 6. genuinely missing — nothing anywhere corresponds
    ("ORD-5013", "INV-2060", "18650.00", "2026-03-07", "Custom integration build", "captured"),

    # 7. fee/tax arithmetic exception
    ("ORD-5014", "INV-2061", "7700.00", "2026-03-07", "Pro subscription annual", "captured"),

    # 8. currency mismatch — booked in USD, settled in INR
    ("ORD-5015", "INV-2062", "1200.00", "2026-03-08", "Overseas licence fee", "captured"),

    # 9. double claim — two orders, one settlement
    ("ORD-5016", "INV-2063", "3300.00", "2026-03-09", "Annual membership renewal", "captured"),
    ("ORD-5017", "INV-2063", "3300.00", "2026-03-09", "Annual membership renewal duplicate entry", "captured"),

    # partial refund, consistent on both sides
    ("ORD-5018", "INV-2064", "11000.00", "2026-03-10", "Widget bundle premium", "partially_refunded"),
]

# Orders 15 is USD on the ledger side.
ORDER_CURRENCY = {"ORD-5015": "USD"}
ORDER_REFUND = {"ORD-5018": "2500.00"}

# (payment_id, order_reference, amount, fee, tax, date, settled, status, description)
GATEWAY = [
    ("pay_AX9001", "INV-2048", "12500.00", "250.00", "45.00", "2026-03-01", "2026-03-03", "captured", "Payment for INV-2048 cloud platform"),
    ("pay_AX9002", "INV-2049", "4300.50", "86.01", "15.48", "2026-03-01", "2026-03-03", "captured", "Payment for INV-2049 SoundMax Pro"),
    ("pay_AX9003", "INV-2050", "899.00", "17.98", "3.24", "2026-03-02", "2026-03-04", "captured", "Payment for INV-2050 starter plan"),
    ("pay_AX9004", "INV-2051", "24999.00", "499.98", "90.00", "2026-03-02", "2026-03-04", "captured", "Payment for INV-2051 onboarding"),
    ("pay_AX9005", "INV-2052", "1750.00", "35.00", "6.30", "2026-03-03", "2026-03-05", "captured", "Payment for INV-2052 priority support"),
    ("pay_AX9006", "INV-2053", "6300.00", "126.00", "22.68", "2026-03-03", "2026-03-05", "captured", "Payment for INV-2053 team seats"),
    ("pay_AX9007", "INV-2054", "15400.00", "308.00", "55.44", "2026-03-04", "2026-03-06", "captured", "Payment for INV-2054 data export"),
    ("pay_AX9008", "INV-2055", "2200.00", "44.00", "7.92", "2026-03-04", "2026-03-06", "captured", "Payment for INV-2055 widget bundle"),
    # reformatted reference: INV/2056/A -> INV-2056
    ("pay_AX9009", "INV-2056", "8750.00", "175.00", "31.50", "2026-03-05", "2026-03-07", "captured", "Payment for INV-2056 consulting"),
    # fee/tax arithmetic broken deliberately (fee does not reconcile)
    ("pay_AX9014", "INV-2061", "7700.00", "980.00", "176.40", "2026-03-07", "2026-03-09", "captured", "Payment for INV-2061 pro subscription"),
    # currency mismatch: ledger says USD, gateway says INR
    ("pay_AX9015", "INV-2062", "1200.00", "24.00", "4.32", "2026-03-08", "2026-03-10", "captured", "Payment for INV-2062 licence fee"),
    # one settlement, two competing orders
    ("pay_AX9016", "INV-2063", "3300.00", "66.00", "11.88", "2026-03-09", "2026-03-11", "captured", "Payment for INV-2063 membership"),
    # partial refund consistent on both sides
    ("pay_AX9018", "INV-2064", "11000.00", "220.00", "39.60", "2026-03-10", "2026-03-12", "partially_refunded", "Payment for INV-2064 widget bundle premium"),
]
GATEWAY_REFUND = {"pay_AX9018": "2500.00"}

# Bank statement: truncated narration, split debit/credit, no order refs.
# (value_date, narration, withdrawal, deposit, ref_no)
BANK = [
    # 3. the semantic case — this is ORD-5010, and nothing but the amount
    #    and the mangled merchant name connects them
    ("2026-03-07", "NEFT INWARD CLDPLTFRM RENEWAL NORTHWND", "", "31200.00", "UTR774120"),
    # 4. the collision — same amount as ORD-5011, entirely unrelated payer
    ("2026-03-06", "UPI/COLLECT/MEDIQUIP SUPPLIES/9921", "", "5000.00", "UTR774090"),
    # unrelated bank traffic, present so the population is realistic
    ("2026-03-02", "IMPS INWARD ACME LOGISTICS", "", "7800.00", "UTR773980"),
    ("2026-03-04", "NEFT OUTWARD OFFICE RENT MARCH", "45000.00", "", "UTR774002"),
    ("2026-03-05", "UPI/COLLECT/BRIGHTPATH/1180", "", "2650.00", "UTR774041"),
    ("2026-03-08", "BANK CHARGES QTR", "590.00", "", "UTR774135"),
]


# The payout file states its own net. pay_AX9014's net is deliberately
# inconsistent with gross minus fee and tax — a real payout file that does
# not add up, which is what the arithmetic check exists to catch.
BROKEN_NET = {"pay_AX9014": "6800.00"}


def _net(payment_id: str, amount: str, fee: str, tax: str) -> str:
    if payment_id in BROKEN_NET:
        return BROKEN_NET[payment_id]
    refund = float(GATEWAY_REFUND.get(payment_id, "0") or 0)
    return f"{float(amount) - float(fee) - float(tax) - refund:.2f}"


def _write(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    _write(
        OUT_DIR / "orders.csv",
        ["order_id", "invoice_ref", "amount", "currency", "order_date", "description", "status", "refund_amount"],
        [[oid, ref, amt, ORDER_CURRENCY.get(oid, "INR"), date, desc, status, ORDER_REFUND.get(oid, "")]
         for oid, ref, amt, date, desc, status in ORDERS],
    )

    _write(
        OUT_DIR / "gateway_payouts.csv",
        ["payment_id", "order_id", "amount", "fee", "tax", "net_amount", "currency", "created_at",
         "settlement_date", "status", "description", "refund_amount"],
        [[pid, ref, amt, fee, tax, _net(pid, amt, fee, tax), "INR", date, settled, status, desc,
          GATEWAY_REFUND.get(pid, "")]
         for pid, ref, amt, fee, tax, date, settled, status, desc in GATEWAY],
    )

    _write(
        OUT_DIR / "bank_statement.csv",
        ["Value Date", "Narration", "Withdrawal", "Deposit", "Ref No"],
        [list(row) for row in BANK],
    )

    print(f"Demo data written to {OUT_DIR}")
    print(f"  orders.csv           {len(ORDERS):>3} rows  (ledger side)")
    print(f"  gateway_payouts.csv  {len(GATEWAY):>3} rows  (settlement side)")
    print(f"  bank_statement.csv   {len(BANK):>3} rows  (settlement side, truncated narration)")
    print("\nDemonstrates: deterministic matches, a reformatted reference, a semantic")
    print("bank-narration match, a same-amount collision that is refused, a pending")
    print("settlement, a missing settlement, a fee/tax exception, a currency mismatch,")
    print("and two orders competing for one settlement.")


if __name__ == "__main__":
    main()
