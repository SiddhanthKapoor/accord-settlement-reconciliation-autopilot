"""
The demo workspace: thirteen files, one story, no randomness in the story.

`generate_demo_data.py` writes three files in three shapes and is fine for
a unit-sized demo. This writes the thing a finance team actually hands
over: two order sources, four gateway exports, three bank statements from
two accounts across two months, two accounting exports, a refund report,
and one file that is a byte-identical re-upload of another.

Every row is written by hand. The only use of `random` here is to mint
opaque provider ids (`pay_...`, `setl_...`) from a fixed seed, so the ids
look like real ones and are identical on every run. Nothing about which
scenario a record exercises is random, and there is no `datetime.now()`
anywhere in the output — the observation point is the AS_OF constant
below, so the demo does not decay.

What the set is built to show, in the order a video would show it:

  1. most records reconcile on an exact reference, instantly, with no
     model call at all — the boring majority is the point
  2. two records that only a semantic model can resolve, built so the
     identifier is genuinely absent or in another namespace
  3. an identical-amount trap that must be REFUSED
  4. an aggregated settlement whose arithmetic ties out to the paisa
  5. pending vs missing, pinned to AS_OF rather than to today
  6. a fee/tax exception, a refund offset, a refund/chargeback mismatch
  7. a currency mismatch
  8. the same payment present in two sources
  9. a record even a model should call ambiguous
 10. a truncated reference
 11. a duplicate file

Usage:
    python backend/data/generate_demo_workspace.py
    python backend/data/generate_demo_workspace.py --verify
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import shutil
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT_DIR = Path(__file__).parent / "demo_workspace"

# The observation point. Everything is pinned relative to this, never to
# today, so "pending" stays pending and the demo reproduces in a year.
# It is also, deliberately, the latest settlement date in the whole
# population: batch.process_batch derives `as_of` from exactly that when
# the caller does not pass one, which is what the upload path does.
AS_OF = datetime(2026, 4, 15, tzinfo=timezone.utc)

SEED = 4620
FEE_RATE = 0.02
GST_RATE = 0.18

# T+2 is PolicyConfig.settlement_expected_days. A ledger record dated
# after this cutoff has no settlement because none is due yet.
PENDING_CUTOFF = AS_OF - timedelta(days=2)


# ---------------------------------------------------------------------------
# formatting helpers — each vendor writes money and dates its own way
# ---------------------------------------------------------------------------

def minor(rupees: str | float) -> int:
    return int(round(float(rupees) * 100))


def plain(rupees: str | float) -> str:
    return f"{float(rupees):.2f}"


def grouped(rupees: str | float) -> str:
    """Indian digit grouping: 123456.78 -> '1,23,456.78'."""
    whole, frac = f"{abs(float(rupees)):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        chunks: list[str] = []
        while len(head) > 2:
            chunks.insert(0, head[-2:])
            head = head[:-2]
        if head:
            chunks.insert(0, head)
        whole = ",".join(chunks + [tail])
    sign = "-" if float(rupees) < 0 else ""
    return f"{sign}{whole}.{frac}"


def rupee(rupees: str | float) -> str:
    return f"\u20b9{grouped(rupees)}"


def parens(rupees: str | float) -> str:
    """Accounting-style negative: 2500.00 -> '(2,500.00)'."""
    return f"({grouped(rupees)})"


def iso(day: str) -> str:
    return day


def dmy(day: str) -> str:
    y, m, d = day.split("-")
    return f"{d}-{m}-{y}"


def dmy_slash(day: str, hhmm: str) -> str:
    y, m, d = day.split("-")
    return f"{d}/{m}/{y} {hhmm}"


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def dmonY(day: str) -> str:
    y, m, d = day.split("-")
    return f"{d}-{_MONTHS[int(m) - 1]}-{y}"


def monDY(day: str) -> str:
    y, m, d = day.split("-")
    return f"{_MONTHS[int(m) - 1]} {d}, {y}"


def stamp(day: str, hhmmss: str) -> str:
    return f"{day} {hhmmss}"


def epoch(day: str, hhmm: str = "10:30") -> str:
    """Razorpay reports epoch seconds. parse_date reads a 10-digit run of
    digits as unix seconds, which every date in this era is."""
    h, m = hhmm.split(":")
    when = datetime.fromisoformat(day).replace(hour=int(h), minute=int(m), tzinfo=timezone.utc)
    value = str(int(when.timestamp()))
    assert len(value) == 10, f"epoch for {day} is not 10 digits: {value}"
    return value


def fee_tax(gross_minor: int) -> tuple[int, int]:
    fee = round(gross_minor * FEE_RATE)
    return fee, round(fee * GST_RATE)


# ---------------------------------------------------------------------------
# LEDGER SIDE
# ---------------------------------------------------------------------------
# Fictional counterparties, deliberately plausible and deliberately not
# any real business.

# (order_id, invoice, customer, order_date, amount, currency, status,
#  description, refund, due_date)
INTERNAL_INVOICES = [
    # ---- the boring majority: exact reference on the gateway side ----
    ("ORD-7001", "INV-3001", "Northwind Retail Private Limited", "2026-03-02", "12500.00", "INR", "Paid", "Annual cloud platform subscription", "", "2026-03-17"),
    ("ORD-7002", "INV-3002", "Kalyan Technologies Pvt Ltd", "2026-03-02", "4300.50", "INR", "Paid", "SoundMax Pro headphone bulk order", "", "2026-03-17"),
    ("ORD-7003", "INV-3003", "Sunrise Commerce LLP", "2026-03-03", "899.00", "INR", "Paid", "Starter plan monthly", "", "2026-03-18"),
    ("ORD-7004", "INV-3004", "Meridian Softworks Pvt Ltd", "2026-03-03", "24450.00", "INR", "Paid", "Enterprise onboarding fee", "", "2026-03-18"),
    ("ORD-7005", "INV-3005", "Anand Traders Private Limited", "2026-03-04", "1750.00", "INR", "Paid", "Priority support add-on", "", "2026-03-19"),
    ("ORD-7006", "INV-3006", "Bluepeak Services Pvt Ltd", "2026-03-04", "6300.00", "INR", "Paid", "Team seats x3 quarterly", "", "2026-03-19"),
    ("ORD-7007", "INV-3007", "Vantage Analytics Pvt Ltd", "2026-03-05", "15400.00", "INR", "Paid", "Data export service annual", "", "2026-03-20"),
    ("ORD-7008", "INV-3008", "Harbourline Foods Pvt Ltd", "2026-03-05", "2200.00", "INR", "Paid", "Widget bundle standard", "", "2026-03-20"),
    ("ORD-7009", "INV-3009", "Northwind Retail Private Limited", "2026-03-06", "8750.00", "INR", "Paid", "Consulting session block", "", "2026-03-21"),
    # ---- three payments the gateway settles as one bank credit -------
    ("ORD-7010", "INV-3010", "Kalyan Technologies Pvt Ltd", "2026-03-09", "33150.00", "INR", "Paid", "Enterprise licence renewal", "", "2026-03-24"),
    ("ORD-7011", "INV-3011", "Sunrise Commerce LLP", "2026-03-09", "18400.00", "INR", "Paid", "Data migration service", "", "2026-03-24"),
    ("ORD-7012", "INV-3012", "Meridian Softworks Pvt Ltd", "2026-03-09", "9900.00", "INR", "Paid", "Priority support annual", "", "2026-03-24"),
    # ---- more of the boring majority, settled through a second gateway
    ("ORD-7013", "INV-3013", "Anand Traders Private Limited", "2026-03-10", "3125.00", "INR", "Paid", "Widget bundle standard", "", "2026-03-25"),
    ("ORD-7014", "INV-3014", "Bluepeak Services Pvt Ltd", "2026-03-10", "7480.00", "INR", "Paid", "Team seat upgrade", "", "2026-03-25"),
    ("ORD-7015", "INV-3015", "Vantage Analytics Pvt Ltd", "2026-03-11", "21600.00", "INR", "Paid", "Annual analytics add-on", "", "2026-03-26"),
    ("ORD-7016", "INV-3016", "Harbourline Foods Pvt Ltd", "2026-03-11", "1299.00", "INR", "Paid", "Starter plan monthly", "", "2026-03-26"),
    ("ORD-7017", "INV-3017", "Northwind Retail Private Limited", "2026-03-12", "5675.00", "INR", "Paid", "Express shipping add-on", "", "2026-03-27"),
    ("ORD-7018", "INV-3018", "Kalyan Technologies Pvt Ltd", "2026-03-13", "14250.00", "INR", "Paid", "Consulting session block", "", "2026-03-28"),
    ("ORD-7019", "INV-3019", "Sunrise Commerce LLP", "2026-03-13", "2680.00", "INR", "Paid", "Priority support add-on", "", "2026-03-28"),
    ("ORD-7020", "INV-3020", "Meridian Softworks Pvt Ltd", "2026-03-16", "46300.00", "INR", "Paid", "Enterprise onboarding fee", "", "2026-03-31"),
    # ---- S2  semantic: the only record of this money is a bank line --
    ("ORD-7021", "INV-3055", "Northwind Retail Private Limited", "2026-03-17", "31200.00", "INR", "Paid", "Annual cloud platform renewal", "", "2026-04-01"),
    # ---- S3  the identical-amount trap: no settlement exists for this
    ("ORD-7031", "INV-3042", "Vantage Analytics Pvt Ltd", "2026-03-24", "8650.00", "INR", "Paid", "Gift card bulk purchase corporate", "", "2026-04-08"),
    # ---- S3  the trap's twin: same amount, a day later, real settlement
    ("ORD-7032", "INV-3117", "Harbourline Foods Pvt Ltd", "2026-03-25", "8650.00", "INR", "Paid", "Refrigeration unit deposit", "", "2026-04-09"),
    # ---- S5b genuinely missing, with no lookalike anywhere -----------
    ("ORD-7034", "INV-3061", "Bluepeak Services Pvt Ltd", "2026-03-19", "18650.00", "INR", "Paid", "Custom integration build", "", "2026-04-03"),
    # ---- S7  refund offset, recorded consistently on both sides ------
    ("ORD-7036", "INV-3065", "Anand Traders Private Limited", "2026-03-20", "11000.00", "INR", "Partially Refunded", "Widget bundle premium", "2500.00", "2026-04-04"),
    # ---- S7b chargeback the books never recorded --------------------
    ("ORD-7037", "INV-3072", "Kalyan Technologies Pvt Ltd", "2026-03-23", "16800.00", "INR", "Paid", "Analytics seat pack", "", "2026-04-07"),
    # ---- S9  booked in USD, settled in INR --------------------------
    ("ORD-7038", "INV-3068", "Meridian Softworks Pvt Ltd", "2026-03-26", "1200.00", "USD", "Paid", "Overseas licence fee", "", "2026-04-10"),
    # ---- S11 the gateway truncated the reference --------------------
    ("ORD-7040", "INV-3062", "Sunrise Commerce LLP", "2026-03-27", "13475.00", "INR", "Paid", "Consulting retainer block", "", "2026-04-11"),
    # ---- S8  the same payment is in the gateway file and the bank ----
    ("ORD-7104", "INV-3050", "Northwind Retail Private Limited", "2026-03-28", "24999.00", "INR", "Paid", "Enterprise onboarding fee phase 2", "", "2026-04-12"),
    ("ORD-7042", "INV-3077", "Harbourline Foods Pvt Ltd", "2026-04-02", "9260.00", "INR", "Paid", "Cold chain logistics add-on", "", "2026-04-17"),
]

# Shopify-style export. (name, created_at, financial_status, subtotal,
#  shipping, taxes, total, lineitem_qty, lineitem_name, notes, email, method)
SHOPIFY_ORDERS = [
    ("SH-88201", "2026-03-06 09:14:22", "paid", "2949.15", "120.00", "410.85", "3480.00", "1", "Ceramic pour-over kettle 1.2L", "Web order - ceramic pour-over kettle", "orders@northwind-retail.example", "Cashfree"),
    ("SH-88202", "2026-03-08 11:02:47", "paid", "1067.80", "0.00", "192.20", "1260.00", "2", "Paper filter pack 100s", "Web order - paper filter refill pack", "orders@kalyantech.example", "Cashfree"),
    ("SH-88203", "2026-03-10 15:38:09", "paid", "5033.90", "0.00", "906.10", "5940.00", "1", "Burr grinder compact", "Web order - compact burr grinder", "orders@sunrise-commerce.example", "Cashfree"),
    ("SH-88204", "2026-03-12 08:51:33", "paid", "1817.80", "0.00", "327.20", "2145.00", "3", "Insulated travel tumbler", "Web order - insulated travel tumbler", "orders@meridian-softworks.example", "Cashfree"),
    ("SH-88205", "2026-03-14 17:26:55", "paid", "7050.85", "220.00", "1049.15", "8320.00", "1", "Espresso machine entry tier", "Web order - entry tier espresso machine", "orders@anand-traders.example", "Cashfree"),
    ("SH-88206", "2026-03-16 10:07:12", "paid", "838.98", "0.00", "151.02", "990.00", "1", "Cleaning tablet jar", "Web order - cleaning tablet jar", "orders@bluepeak.example", "Cashfree"),
    ("SH-88207", "2026-03-18 13:45:26", "paid", "3961.86", "0.00", "713.14", "4675.00", "2", "Milk frothing jug set", "Web order - milk frothing jug set", "orders@vantage-analytics.example", "Cashfree"),
    ("SH-88208", "2026-03-20 19:12:40", "paid", "10881.36", "300.00", "1658.64", "12840.00", "1", "Roasting drum accessory", "Web order - roasting drum accessory", "orders@harbourline.example", "Cashfree"),
    ("SH-88209", "2026-03-22 07:33:18", "paid", "2025.42", "0.00", "364.58", "2390.00", "1", "Scale with timer", "Web order - brewing scale with timer", "orders@northwind-retail.example", "Cashfree"),
    ("SH-88210", "2026-03-24 16:20:04", "paid", "5211.86", "0.00", "938.14", "6150.00", "1", "Cold brew tower", "Web order - cold brew tower", "orders@kalyantech.example", "Cashfree"),
    # ---- S10 vague on both sides. Nothing here says what it was for.
    ("SH-88211", "2026-03-25 16:40:11", "paid", "3491.53", "0.00", "628.47", "4120.00", "1", "Assorted items", "Counter sale payment received", "", "Other"),
    ("SH-88212", "2026-03-28 12:04:59", "paid", "6190.68", "0.00", "1114.32", "7305.00", "1", "Bench grinder pro tier", "Web order - pro tier bench grinder", "orders@sunrise-commerce.example", "Cashfree"),
]

# Tally-style sales register. Debit stays empty: a sales register credits
# the sales ledger, and a debit row here would be read as money going out.
# (date, voucher_no, voucher_type, bill_ref, particulars, credit)
TALLY_ROWS = [
    # ---- S4a three invoices one distributor paid with a single NEFT --
    ("2026-03-18", "SAL/2026/0412", "Sales", "BR-4471", "Northwind Retail Private Limited - bulk stationery supply", "42300.00"),
    ("2026-03-19", "SAL/2026/0413", "Sales", "BR-4472", "Northwind Retail Private Limited - office furniture", "27850.00"),
    ("2026-03-19", "SAL/2026/0414", "Sales", "BR-4473", "Northwind Retail Private Limited - installation charges", "9415.00"),
    # ---- S6 the payout file's own arithmetic does not add up ---------
    ("2026-03-21", "SAL/2026/0421", "Sales", "BR-4481", "Kalyan Technologies Pvt Ltd - annual maintenance contract", "7700.00"),
    ("2026-03-21", "SAL/2026/0422", "Sales", "BR-4482", "Sunrise Commerce LLP - spare parts consignment", "5240.00"),
    ("2026-03-24", "SAL/2026/0428", "Sales", "BR-4483", "Anand Traders Private Limited - packaging materials", "3960.00"),
    ("2026-03-26", "SAL/2026/0433", "Sales", "BR-4484", "Bluepeak Services Pvt Ltd - warehouse racking", "12120.00"),
]

# Zoho Books-style invoice export.
# (date, number, status, customer, currency, total, balance, item, notes)
ZOHO_ROWS = [
    ("2026-03-16", "ZB-6101", "Paid", "Vantage Analytics Pvt Ltd", "INR", "6540.00", "0.00", "Analytics tier 2", "Monthly subscription analytics tier 2"),
    ("2026-03-18", "ZB-6102", "Paid", "Bluepeak Services Pvt Ltd", "INR", "2380.00", "0.00", "Support retainer", "Monthly support retainer"),
    # ---- S2b semantic: the marketplace names itself nothing like this
    ("2026-03-20", "ZB-6104", "Paid", "Kartway Seller Services", "INR", "27450.00", "0.00", "Marketplace channel", "Marketplace channel settlement week 12"),
    ("2026-03-31", "ZB-6106", "Paid", "Harbourline Foods Pvt Ltd", "INR", "4990.00", "0.00", "Cold storage audit", "Cold storage compliance audit"),
    # ---- S5a captured too recently for a settlement to be due --------
    ("2026-04-14", "ZB-6107", "Sent", "Harbourline Foods Pvt Ltd", "INR", "15750.00", "15750.00", "Quarterly retainer", "Quarterly retainer April to June"),
]


# ---------------------------------------------------------------------------
# SETTLEMENT SIDE
# ---------------------------------------------------------------------------

# Razorpay-style: paise as integers, epoch-second timestamps.
# (order_ref, gross, captured_day, settled_day, refund, currency, note)
RAZORPAY_ROWS = [
    ("INV-3001", "12500.00", "2026-03-02", "2026-03-04", "0", "INR", "Settlement for INV-3001 Northwind Store cloud platform"),
    ("INV-3002", "4300.50", "2026-03-02", "2026-03-04", "0", "INR", "Settlement for INV-3002 KalyanTech headphone order"),
    ("INV-3003", "899.00", "2026-03-03", "2026-03-05", "0", "INR", "Settlement for INV-3003 Sunrise Shop starter plan"),
    ("INV-3004", "24450.00", "2026-03-03", "2026-03-05", "0", "INR", "Settlement for INV-3004 Meridian Apps onboarding"),
    ("INV-3005", "1750.00", "2026-03-04", "2026-03-06", "0", "INR", "Settlement for INV-3005 Anand Traders priority support"),
    ("INV-3006", "6300.00", "2026-03-04", "2026-03-06", "0", "INR", "Settlement for INV-3006 Bluepeak team seats"),
    ("INV-3007", "15400.00", "2026-03-05", "2026-03-07", "0", "INR", "Settlement for INV-3007 Vantage data export"),
    ("INV-3008", "2200.00", "2026-03-05", "2026-03-07", "0", "INR", "Settlement for INV-3008 Harbourline widget bundle"),
    ("INV-3009", "8750.00", "2026-03-06", "2026-03-09", "0", "INR", "Settlement for INV-3009 Northwind Store consulting"),
    ("INV-3010", "33150.00", "2026-03-09", "2026-03-11", "0", "INR", "Settlement for INV-3010 KalyanTech licence renewal"),
    ("INV-3011", "18400.00", "2026-03-09", "2026-03-11", "0", "INR", "Settlement for INV-3011 Sunrise Shop data migration"),
    ("INV-3012", "9900.00", "2026-03-09", "2026-03-11", "0", "INR", "Settlement for INV-3012 Meridian Apps priority support"),
    ("INV-3018", "14250.00", "2026-03-13", "2026-03-16", "0", "INR", "Settlement for INV-3018 KalyanTech consulting block"),
    ("INV-3019", "2680.00", "2026-03-13", "2026-03-16", "0", "INR", "Settlement for INV-3019 Sunrise Shop priority support"),
    ("INV-3020", "46300.00", "2026-03-16", "2026-03-18", "0", "INR", "Settlement for INV-3020 Meridian Apps onboarding"),
    ("ZB-6101", "6540.00", "2026-03-16", "2026-03-18", "0", "INR", "Settlement for ZB-6101 Vantage analytics tier 2"),
    ("ZB-6102", "2380.00", "2026-03-18", "2026-03-20", "0", "INR", "Settlement for ZB-6102 Bluepeak support retainer"),
    ("INV-3065", "11000.00", "2026-03-20", "2026-03-22", "2500.00", "INR", "Settlement for INV-3065 Anand Traders widget bundle premium"),
    ("INV-3072", "16800.00", "2026-03-23", "2026-03-25", "4200.00", "INR", "Settlement for INV-3072 KalyanTech analytics seat pack"),
    ("INV-3068", "1200.00", "2026-03-26", "2026-03-28", "0", "INR", "Settlement for INV-3068 Meridian Apps licence fee"),
    ("INV-3050", "24999.00", "2026-03-28", "2026-03-30", "0", "INR", "Settlement for INV-3050 Northwind Store onboarding phase 2"),
    ("INV-3077", "9260.00", "2026-04-02", "2026-04-04", "0", "INR", "Settlement for INV-3077 Harbourline cold chain add-on"),
]

# Cashfree-style: rupees with decimals, its own column vocabulary.
# (order_ref, gross, paid_stamp, settled_day, customer, remark)
CASHFREE_ROWS = [
    ("INV-3013", "3125.00", "2026-03-10 11:42:08", "2026-03-12", "Anand Traders Private Limited", "Payment for INV-3013 widget bundle standard"),
    ("INV-3014", "7480.00", "2026-03-10 14:19:55", "2026-03-12", "Bluepeak Services Pvt Ltd", "Payment for INV-3014 team seat upgrade"),
    ("INV-3015", "21600.00", "2026-03-11 09:05:41", "2026-03-13", "Vantage Analytics Pvt Ltd", "Payment for INV-3015 analytics add-on"),
    ("INV-3016", "1299.00", "2026-03-11 18:27:03", "2026-03-13", "Harbourline Foods Pvt Ltd", "Payment for INV-3016 starter plan monthly"),
    ("INV-3017", "5675.00", "2026-03-12 12:58:20", "2026-03-14", "Northwind Retail Private Limited", "Payment for INV-3017 express shipping add-on"),
    ("SH-88201", "3480.00", "2026-03-06 09:15:02", "2026-03-08", "Web customer", "Payment for SH-88201 ceramic pour-over kettle"),
    ("SH-88202", "1260.00", "2026-03-08 11:03:31", "2026-03-10", "Web customer", "Payment for SH-88202 paper filter refill pack"),
    ("SH-88203", "5940.00", "2026-03-10 15:38:52", "2026-03-12", "Web customer", "Payment for SH-88203 compact burr grinder"),
    ("SH-88204", "2145.00", "2026-03-12 08:52:11", "2026-03-14", "Web customer", "Payment for SH-88204 insulated travel tumbler"),
    ("SH-88205", "8320.00", "2026-03-14 17:27:38", "2026-03-16", "Web customer", "Payment for SH-88205 entry tier espresso machine"),
    ("SH-88206", "990.00", "2026-03-16 10:07:49", "2026-03-18", "Web customer", "Payment for SH-88206 cleaning tablet jar"),
    ("SH-88207", "4675.00", "2026-03-18 13:46:07", "2026-03-20", "Web customer", "Payment for SH-88207 milk frothing jug set"),
    ("SH-88208", "12840.00", "2026-03-20 19:13:22", "2026-03-22", "Web customer", "Payment for SH-88208 roasting drum accessory"),
    ("SH-88209", "2390.00", "2026-03-22 07:33:59", "2026-03-24", "Web customer", "Payment for SH-88209 brewing scale with timer"),
    ("SH-88210", "6150.00", "2026-03-24 16:20:47", "2026-03-26", "Web customer", "Payment for SH-88210 cold brew tower"),
    # ---- S3 the trap's real owner. Same amount as INV-3042, a day apart.
    ("INV-3117", "8650.00", "2026-03-25 10:11:34", "2026-03-27", "Harbourline Foods Pvt Ltd", "Payment for INV-3117 refrigeration unit deposit"),
    # ---- S11 the reference is truncated: the real one is INV-3062 -----
    ("INV-306", "13475.00", "2026-03-27 15:44:26", "2026-03-29", "Sunrise Commerce LLP", "Payment for INV-306 consulting retainer block"),
    ("SH-88212", "7305.00", "2026-03-28 12:05:37", "2026-03-30", "Web customer", "Payment for SH-88212 pro tier bench grinder"),
    ("ZB-6106", "4990.00", "2026-03-31 09:48:15", "2026-04-02", "Harbourline Foods Pvt Ltd", "Payment for ZB-6106 cold storage compliance audit"),
]

# PayU-style: rupee symbol, thousands separators, dd/mm/yyyy timestamps.
# (merchant_ref, gross, added_on_day, added_on_time, settled_day,
#  net_override_or_None, mode)
PAYU_ROWS = [
    ("BR-4481", "7700.00", "2026-03-21", "09:15", "2026-03-23", "6800.00", "CC"),
    ("BR-4482", "5240.00", "2026-03-21", "14:02", "2026-03-23", None, "NB"),
    ("BR-4483", "3960.00", "2026-03-24", "10:31", "2026-03-26", None, "UPI"),
    ("BR-4484", "12120.00", "2026-03-26", "16:48", "2026-03-28", None, "NB"),
]

# Marketplace payout, Amazon-report-shaped: hyphenated headers, a week
# window, a separate fee column the detector does not claim.
# (order_id, week_start, week_end, payout_day, product_sales, selling_fees, total, description)
MARKETPLACE_ROWS = [
    ("KWY-88213", "2026-03-16", "2026-03-22", "2026-03-24", "28950.00", "-1500.00", "27450.00", "Seller services payout for week 12"),
    ("KWY-88226", "2026-03-23", "2026-03-29", "2026-03-31", "20230.00", "-1050.00", "19180.00", "Seller services payout for week 13"),
    ("KWY-88231", "2026-03-30", "2026-04-05", "2026-04-07", "23880.00", "-1240.00", "22640.00", "Seller services payout for week 14"),
]

# Bank statements. (txn_day, value_day, narration, ref_no, withdrawal, deposit)
HDFC_MARCH = [
    ("2026-03-04", "2026-03-04", "IMPS INWARD ACME LOGISTICS LLP", "UTR773901", "", "7800.00"),
    ("2026-03-06", "2026-03-06", "NEFT OUTWARD OFFICE RENT MARCH", "UTR773954", "45000.00", ""),
    # ---- S4b one credit for a whole gateway batch, net of fee and tax
    ("2026-03-11", "2026-03-11", "NEFT INWARD RAZORPAY SETL BATCH", "UTR774008", "", "AGG_B"),
    ("2026-03-14", "2026-03-14", "UPI/COLLECT/BRIGHTPATH TRADERS/1180", "UTR774061", "", "2650.00"),
    # ---- S2a the semantic case: no identifier survives the narration -
    ("2026-03-18", "2026-03-18", "NEFT INWARD CLDPLTFRM RENEWAL NORTHWND", "UTR774120", "", "31200.00"),
    ("2026-03-20", "2026-03-20", "BANK CHARGES QTR ENDING MAR", "UTR774188", "590.00", ""),
    # ---- S10 vague on both sides -------------------------------------
    ("2026-03-26", "2026-03-26", "UPI/COLLECT/9911/PAYMENT RECEIVED", "UTR774501", "", "4120.00"),
    # ---- S8 the same payment the Razorpay file already reported ------
    ("2026-03-30", "2026-03-30", "NEFT INWARD RAZORPAY SETL NORTHWND RTL", "INV-3050", "", "24999.00"),
    ("2026-03-31", "2026-03-31", "INTEREST CREDIT QTR", "UTR774610", "", "1240.00"),
]

HDFC_APRIL = [
    ("2026-04-01", "2026-04-01", "NEFT OUTWARD SUPPLIER PAYMENT KRISHNA ENTERPRISES", "UTR774702", "62400.00", ""),
    ("2026-04-03", "2026-04-03", "IMPS INWARD ACME LOGISTICS LLP", "UTR774755", "", "4135.00"),
    ("2026-04-06", "2026-04-06", "NEFT OUTWARD OFFICE RENT APRIL", "UTR774811", "45000.00", ""),
    ("2026-04-09", "2026-04-09", "UPI/COLLECT/BRIGHTPATH TRADERS/2260", "UTR774880", "", "3395.00"),
    ("2026-04-13", "2026-04-13", "NEFT INWARD RAZORPAY SETL BATCH APR", "UTR774935", "", "18742.36"),
    # The latest settlement date in the whole workspace: this is what
    # batch.process_batch derives `as_of` from, and it is why ZB-6107 is
    # PENDING rather than MISSING.
    ("2026-04-15", "2026-04-15", "BANK CHARGES QTR", "UTR775001", "708.00", ""),
]

ICICI_MARCH = [
    ("2026-03-05", "2026-03-05", "NEFT INWARD DISTRIBUTOR ADVANCE HARBOURLINE", "UTR881204", "", "55000.00"),
    ("2026-03-12", "2026-03-12", "NEFT OUTWARD VENDOR SETTLEMENT PRAKASH AND CO", "UTR881260", "22500.00", ""),
    # ---- S4a one NEFT paying three Tally invoices at once ------------
    ("2026-03-23", "2026-03-23", "NEFT INWARD NORTHWND RTL CONSOLIDATED", "UTR881318", "", "AGG_A"),
    ("2026-03-27", "2026-03-27", "UPI/COLLECT/MEDIQUIP SURGICALS/8841", "UTR881377", "", "6720.00"),
    ("2026-03-29", "2026-03-29", "BANK CHARGES ESCROW MAINTENANCE", "UTR881402", "350.00", ""),
    ("2026-03-31", "2026-03-31", "NEFT INWARD DISTRIBUTOR ADVANCE SUNRISE", "UTR881455", "", "31000.00"),
]

# Refund / chargeback report. Amounts are accounting-negative.
# (refund_id, order_ref, day, amount, kind, reason)
REFUND_ROWS = [
    ("RFD-9001", "INV-3065", "2026-03-22", "2500.00", "REFUND", "Partial return - damaged item"),
    ("RFD-9002", "INV-3072", "2026-03-27", "4200.00", "CHARGEBACK", "Cardholder dispute - goods not received"),
    ("RFD-9003", "KWY-88226", "2026-04-02", "1145.00", "CHARGEBACK", "Marketplace A-to-Z claim"),
]

# The aggregation groups, named so the assertions and the docs cannot
# drift from the data.
AGG_A_MEMBERS = ("BR-4471", "BR-4472", "BR-4473")
AGG_B_MEMBERS = ("INV-3010", "INV-3011", "INV-3012")

# Records that can plausibly end a run with no matched settlement. Used
# by the aggregation-uniqueness assertion, which has to reason about the
# same pool `batch.detect_aggregated_settlements` will see.
POSSIBLY_UNMATCHED = (
    "ORD-7021", "ORD-7031", "ORD-7034", "ORD-7040", "ORD-7104",
    "BR-4471", "BR-4472", "BR-4473", "ZB-6104", "ZB-6107", "SH-88211",
)


# ---------------------------------------------------------------------------
# opaque provider ids — the one place randomness is used, from a fixed seed
# ---------------------------------------------------------------------------

class IdMint:
    _ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)

    def token(self, width: int = 14) -> str:
        return "".join(self.rng.choice(self._ALPHABET) for _ in range(width))

    def payment(self) -> str:
        return f"pay_{self.token()}"

    def settlement(self) -> str:
        return f"setl_{self.token()}"

    def cashfree_payment(self) -> str:
        return f"CF-{self.rng.randint(100000000, 999999999)}"

    def payu_txn(self) -> str:
        return f"PU{self.rng.randint(10**11, 10**12 - 1)}"

    def payu_mihpay(self) -> str:
        return str(self.rng.randint(10**17, 10**18 - 1))

    def payu_bankref(self) -> str:
        return str(self.rng.randint(10**11, 10**12 - 1))


# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------

def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write_xlsx(path: Path, sheet_name: str, title_rows: list[str], header: list[str],
               rows: list[list[str]]) -> None:
    """A workbook with byte-identical output on every run.

    openpyxl stamps `datetime.now()` into both the zip entries and
    docProps/core.xml, so a plain `wb.save()` differs on every run. The
    properties are pinned and the archive is then rewritten with a fixed
    entry timestamp, which is what makes `--verify`'s reproducibility
    check meaningful instead of decorative.
    """
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    for line in title_rows:
        sheet.append([line])
    if title_rows:
        sheet.append([])
    sheet.append(header)
    for row in rows:
        sheet.append(row)

    fixed = datetime(2026, 4, 15, 9, 0, 0)
    workbook.properties.creator = "Accord demo workspace generator"
    workbook.properties.lastModifiedBy = "Accord demo workspace generator"
    workbook.properties.created = fixed
    workbook.properties.modified = fixed

    staging = io.BytesIO()
    workbook.save(staging)

    deterministic = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(staging.getvalue())) as source:
        with zipfile.ZipFile(deterministic, "w", zipfile.ZIP_DEFLATED) as target:
            for name in source.namelist():
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                target.writestr(info, source.read(name))
    path.write_bytes(deterministic.getvalue())


def bank_rows(entries: list[tuple], opening: float, resolved: dict[str, str]) -> list[list[str]]:
    """Bank lines with a running balance that actually adds up."""
    balance = opening
    out: list[list[str]] = []
    for txn_day, value_day, narration, ref, withdrawal, deposit in entries:
        withdrawal = resolved.get(withdrawal, withdrawal)
        deposit = resolved.get(deposit, deposit)
        balance += float(deposit or 0) - float(withdrawal or 0)
        out.append([
            dmy(txn_day), dmy(value_day), narration, ref,
            grouped(withdrawal) if withdrawal else "",
            grouped(deposit) if deposit else "",
            grouped(balance),
        ])
    return out


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def build_files(out_dir: Path) -> dict:
    mint = IdMint(SEED)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- ledger: internal invoice export ------------------------------
    write_csv(
        out_dir / "invoices_internal_export.csv",
        ["Invoice No", "Order Id", "Customer Name", "Invoice Date", "Due Date",
         "Amount", "Currency", "Status", "Description", "Refund Amount"],
        [[inv, oid, customer, iso(day), iso(due), plain(amount), currency, status, description,
          plain(refund) if refund else ""]
         for oid, inv, customer, day, amount, currency, status, description, refund, due in INTERNAL_INVOICES],
    )

    # ---- ledger: Shopify-style export ---------------------------------
    write_csv(
        out_dir / "orders_shopify_export.csv",
        ["Name", "Email", "Financial Status", "Paid at", "Fulfillment Status", "Currency",
         "Subtotal", "Shipping", "Taxes", "Total", "Created at",
         "Lineitem quantity", "Lineitem name", "Payment Method", "Notes"],
        [[name, email, status, created, "fulfilled", "INR", subtotal, shipping, taxes, total,
          created, qty, item, method, notes]
         for name, created, status, subtotal, shipping, taxes, total, qty, item, notes, email, method
         in SHOPIFY_ORDERS],
    )

    # ---- ledger: Tally-style sales register ---------------------------
    write_csv(
        out_dir / "tally_sales_register.csv",
        ["Date", "Voucher No", "Voucher Type", "Bill Ref No", "Particulars", "Debit", "Credit"],
        [[dmy(day), voucher, kind, ref, particulars, "", plain(credit)]
         for day, voucher, kind, ref, particulars, credit in TALLY_ROWS],
    )

    # ---- ledger: Zoho Books-style invoice export ----------------------
    write_csv(
        out_dir / "zoho_books_invoices.csv",
        ["Invoice Date", "Invoice Number", "Status", "Customer Name", "Currency",
         "Total", "Balance", "Item Name", "Notes"],
        [[iso(day), number, status, customer, currency, plain(total), plain(balance), item, notes]
         for day, number, status, customer, currency, total, balance, item, notes in ZOHO_ROWS],
    )

    # ---- settlement: Razorpay-style -----------------------------------
    settlement_ids: dict[str, str] = {}
    razorpay_payment_ids: dict[str, str] = {}
    razorpay_out: list[list[str]] = []
    razorpay_net: dict[str, int] = {}
    for ref, gross, captured, settled, refund, currency, note in RAZORPAY_ROWS:
        gross_minor = minor(gross)
        refund_minor = minor(refund) if refund not in ("", "0") else 0
        fee_minor, tax_minor = fee_tax(gross_minor)
        net_minor = gross_minor - fee_minor - tax_minor - refund_minor
        payment_id = mint.payment()
        razorpay_payment_ids[ref] = payment_id
        razorpay_net[ref] = net_minor
        settlement_id = settlement_ids.setdefault(settled, mint.settlement())
        razorpay_out.append([
            payment_id, ref, settlement_id, str(gross_minor), currency,
            str(fee_minor), str(tax_minor), str(net_minor), str(refund_minor),
            "partially_refunded" if refund_minor else "settled",
            "card" if gross_minor % 3 == 0 else "netbanking",
            epoch(captured, "10:30"), epoch(settled, "11:00"), note,
            f"auto settlement {settlement_id}",
        ])
    write_csv(
        out_dir / "razorpay_settlements_mar_apr.csv",
        ["payment_id", "order_id", "settlement_id", "amount", "currency", "fee", "tax",
         "net_amount", "refund_amount", "status", "method", "created_at", "settlement_date",
         "description", "notes"],
        razorpay_out,
    )

    # ---- settlement: Cashfree-style -----------------------------------
    cashfree_out: list[list[str]] = []
    for ref, gross, paid_stamp, settled, customer, remark in CASHFREE_ROWS:
        gross_minor = minor(gross)
        fee_minor, tax_minor = fee_tax(gross_minor)
        cashfree_out.append([
            f"CFSETL{settled.replace('-', '')}", iso(settled), ref, mint.cashfree_payment(),
            paid_stamp, plain(gross), plain(fee_minor / 100), plain(tax_minor / 100),
            plain((gross_minor - fee_minor - tax_minor) / 100), "INR", "SUCCESS", customer, remark,
        ])
    write_csv(
        out_dir / "cashfree_settlements_mar.csv",
        ["Settlement ID", "Settlement Date", "Order ID", "Payment ID", "Payment Time",
         "Payment Amount", "Payment Charges", "GST", "Amount Settled", "Currency",
         "Payment Status", "Customer Name", "Remarks"],
        cashfree_out,
    )

    # ---- settlement: PayU-style ---------------------------------------
    payu_out: list[list[str]] = []
    for ref, gross, day, hhmm, settled, net_override, mode in PAYU_ROWS:
        gross_minor = minor(gross)
        fee_minor, tax_minor = fee_tax(gross_minor)
        net_minor = minor(net_override) if net_override else gross_minor - fee_minor - tax_minor
        payu_out.append([
            mint.payu_mihpay(), mint.payu_txn(), ref, rupee(gross), rupee(fee_minor / 100),
            rupee(tax_minor / 100), rupee(net_minor / 100), "INR", mode, mint.payu_bankref(),
            dmy_slash(day, hhmm), dmy_slash(settled, "18:00"), "success",
        ])
    write_csv(
        out_dir / "payu_settlements_mar.csv",
        ["Mihpayid", "Txnid", "Merchant Ref No", "Amount", "Service Charge", "GST",
         "Net Amount", "Currency", "Mode", "Bank Ref Num", "Added On", "Settlement Date", "Status"],
        payu_out,
    )

    # ---- settlement: marketplace payout -------------------------------
    write_csv(
        out_dir / "kartway_marketplace_payout.csv",
        ["settlement-id", "settlement-start-date", "settlement-end-date", "payout-date",
         "transaction-type", "order-id", "product-sales", "selling-fees", "total",
         "currency", "description"],
        [[f"KWYSETL{payout.replace('-', '')}", dmonY(start), dmonY(end), dmonY(payout),
          "Order Payout", order_id, plain(sales), plain(fees), plain(total), "INR", description]
         for order_id, start, end, payout, sales, fees, total, description in MARKETPLACE_ROWS],
    )

    # ---- settlement: three bank statements ----------------------------
    agg_a_total = sum(minor(credit) for _, _, _, ref, _, credit in TALLY_ROWS if ref in AGG_A_MEMBERS)
    agg_b_total = sum(razorpay_net[ref] for ref in AGG_B_MEMBERS)
    resolved = {"AGG_A": plain(agg_a_total / 100), "AGG_B": plain(agg_b_total / 100)}

    bank_header = ["Txn Date", "Value Date", "Narration", "Ref No", "Withdrawal", "Deposit", "Closing Balance"]

    hdfc_march = bank_rows(HDFC_MARCH, 412650.00, resolved)
    write_csv(out_dir / "bank_hdfc_current_mar2026.csv", bank_header, hdfc_march)

    # April opens where March closed, so the two statements read as one
    # account rather than two files that happen to share a name.
    march_closing = float(hdfc_march[-1][-1].replace(",", ""))
    write_xlsx(
        out_dir / "bank_hdfc_current_apr2026.xlsx", "Statement",
        # Two lines of title block above the header, exactly as a bank
        # export renders it. The header lands on row 4.
        ["HDFC BANK LTD - STATEMENT OF ACCOUNT",
         "Account No: 50200078451236   Branch: Koramangala Bengaluru   Period: 01-04-2026 to 15-04-2026"],
        bank_header, bank_rows(HDFC_APRIL, march_closing, resolved),
    )

    write_xlsx(out_dir / "bank_icici_escrow_mar2026.xlsx", "Account Statement", [],
               bank_header, bank_rows(ICICI_MARCH, 128400.00, resolved))

    # ---- settlement: refunds and chargebacks --------------------------
    write_csv(
        out_dir / "refunds_chargebacks_mar2026.csv",
        ["Refund Id", "Original Payment Id", "Order Ref", "Refund Date", "Amount",
         "Type", "Reason", "Status", "Currency"],
        [[refund_id, razorpay_payment_ids.get(order_ref, f"pay_{order_ref.replace('-', '')}"),
          order_ref, monDY(day), parens(amount), kind, reason, "processed", "INR"]
         for refund_id, order_ref, day, amount, kind, reason in REFUND_ROWS],
    )

    # ---- the duplicate upload -----------------------------------------
    # Copied byte-for-byte rather than regenerated, so it is a genuine
    # duplicate no matter what the XLSX writer does.
    shutil.copyfile(out_dir / "bank_icici_escrow_mar2026.xlsx",
                    out_dir / "bank_icici_escrow_mar2026 (1).xlsx")

    return {
        "agg_a_total_minor": agg_a_total,
        "agg_b_total_minor": agg_b_total,
        "razorpay_net": razorpay_net,
        "razorpay_payment_ids": razorpay_payment_ids,
    }


# ---------------------------------------------------------------------------
# self-verification: the invariants that make the set worth demoing
# ---------------------------------------------------------------------------

class InvariantError(AssertionError):
    pass


def ledger_population() -> list[tuple[str, str | None, int, str, str]]:
    """(record_id, reference, amount_minor, order_day, description) for
    every ledger row across all four ledger files, as the mapper will
    produce them — description includes the counterparty, because
    mapper.map_rows appends it."""
    out: list[tuple[str, str | None, int, str, str]] = []
    for oid, inv, customer, day, amount, _c, _s, description, _r, _d in INTERNAL_INVOICES:
        out.append((oid, inv, minor(amount), day, f"{description} {customer}"))
    for name, created, _st, _sub, _ship, _tax, total, _q, _item, notes, _e, _m in SHOPIFY_ORDERS:
        out.append((name, name, minor(total), created.split(" ")[0], notes))
    for day, _v, _k, ref, particulars, credit in TALLY_ROWS:
        out.append((ref, ref, minor(credit), day, particulars))
    for day, number, _st, customer, _c, total, _b, _i, notes in ZOHO_ROWS:
        out.append((number, number, minor(total), day, f"{notes} {customer}"))
    return out


def settlement_population(built: dict) -> list[tuple[str, str, int, str, str]]:
    """(payment_id, reference, gross_minor, day, description) for every
    settlement row across all seven settlement files."""
    out: list[tuple[str, str, int, str, str]] = []
    for ref, gross, captured, _s, _r, _c, note in RAZORPAY_ROWS:
        out.append((built["razorpay_payment_ids"][ref], ref, minor(gross), captured, note))
    for ref, gross, paid_stamp, _s, customer, remark in CASHFREE_ROWS:
        out.append((f"cf::{ref}", ref, minor(gross), paid_stamp.split(" ")[0], f"{remark} {customer}"))
    for ref, gross, day, _t, _s, _n, _m in PAYU_ROWS:
        out.append((f"payu::{ref}", ref, minor(gross), day, ""))
    for order_id, week_start, _we, _payout, _ps, _f, total, description in MARKETPLACE_ROWS:
        # `settlement-start-date` is what the detector rescues as the
        # transaction date; `payout-date` becomes the settlement date.
        out.append((order_id, order_id, minor(total), week_start, description))
    agg = {"AGG_A": built["agg_a_total_minor"] / 100, "AGG_B": built["agg_b_total_minor"] / 100}
    for entries in (HDFC_MARCH, HDFC_APRIL, ICICI_MARCH):
        for txn_day, _v, narration, ref, withdrawal, deposit in entries:
            value = agg.get(deposit, deposit) or agg.get(withdrawal, withdrawal)
            out.append((ref, ref, minor(value), txn_day, narration))
    for refund_id, order_ref, day, amount, kind, reason in REFUND_ROWS:
        out.append((order_ref, order_ref, minor(amount), day, ""))
    return out


def check_invariants(built: dict) -> list[str]:
    """Every claim the demo script makes about this data, asserted here.

    A dataset bug in this repository once invalidated a whole evaluation
    because a supposedly semantic case leaked its identifier into both
    sides. These checks exist so that cannot happen again silently.
    """
    from app.engine import normalize

    notes: list[str] = []
    ledger = ledger_population()
    settlements = settlement_population(built)
    ledger_by_id = {row[0]: row[1:] for row in ledger}

    # -- 1. the aggregated bank credit is exactly the sum of its parts --
    agg_a_parts = [minor(credit) for _d, _v, _k, ref, _p, credit in TALLY_ROWS if ref in AGG_A_MEMBERS]
    if len(agg_a_parts) != 3 or sum(agg_a_parts) != built["agg_a_total_minor"]:
        raise InvariantError("AGG-A: the ICICI credit is not the exact sum of the three Tally invoices")
    notes.append(f"AGG-A  {' + '.join(str(p) for p in agg_a_parts)} = "
                 f"{built['agg_a_total_minor']} paise (Rs. {grouped(built['agg_a_total_minor'] / 100)})")

    agg_b_parts = []
    for ref in AGG_B_MEMBERS:
        gross = next(minor(g) for r, g, *_ in RAZORPAY_ROWS if r == ref)
        fee, tax = fee_tax(gross)
        agg_b_parts.append((ref, gross, fee, tax, gross - fee - tax))
    if sum(p[4] for p in agg_b_parts) != built["agg_b_total_minor"]:
        raise InvariantError("AGG-B: the HDFC credit is not the exact sum of the three net settlements")
    notes.append("AGG-B  " + " + ".join(f"({g}-{f}-{t})" for _r, g, f, t, _n in agg_b_parts)
                 + f" = {built['agg_b_total_minor']} paise (Rs. {grouped(built['agg_b_total_minor'] / 100)})")

    # -- 2. the trap really is identical in amount, one day apart -------
    trap = ledger_by_id["ORD-7031"]
    twin = ledger_by_id["ORD-7032"]
    lookalike = next(s for s in settlements if s[1] == "INV-3117")
    if not (trap[1] == twin[1] == lookalike[2]):
        raise InvariantError("TRAP: ORD-7031, ORD-7032 and the INV-3117 settlement are not all the same amount")
    if abs((datetime.fromisoformat(trap[2]) - datetime.fromisoformat(lookalike[3])).days) > 2:
        raise InvariantError("TRAP: the lookalike settlement is not close in date")
    trap_cores = normalize.reference_cores(trap[0])
    look_cores = normalize.reference_cores(lookalike[1])
    if trap_cores & look_cores:
        raise InvariantError("TRAP: the trap and its lookalike share a reference core")
    if not normalize.references_comparable(trap_cores, look_cores):
        notes.append("TRAP   note: cores are not width-comparable, so the refusal will need the model")
    else:
        notes.append(f"TRAP   ORD-7031 {sorted(trap_cores)} vs INV-3117 {sorted(look_cores)} — "
                     "same amount, comparable namespaces, disjoint: deterministic refusal")

    # -- 3. the semantic cases really do share no recoverable identifier
    semantic_pairs = [
        ("ORD-7021", "UTR774120", "bank narration"),
        ("ZB-6104", "KWY-88213", "marketplace payout"),
    ]
    for record_id, settlement_ref, label in semantic_pairs:
        led = ledger_by_id[record_id]
        cand = next(s for s in settlements if s[1] == settlement_ref)
        if normalize.normalize_reference(led[0]) == normalize.normalize_reference(cand[1]):
            raise InvariantError(f"SEMANTIC {record_id}: references match exactly — not a semantic case")
        led_cores = normalize.reference_cores(led[0], led[3])
        cand_cores = normalize.reference_cores(cand[1], cand[4])
        if led_cores & cand_cores:
            raise InvariantError(
                f"SEMANTIC {record_id}: shares reference core {sorted(led_cores & cand_cores)} with "
                f"{settlement_ref} — deterministic matching would recover it")
        if normalize.references_comparable(normalize.reference_cores(led[0]),
                                           normalize.reference_cores(cand[1])):
            raise InvariantError(
                f"SEMANTIC {record_id}: identifier namespaces are width-comparable, so the pair would be "
                "refused as contradictory instead of escalated")
        if led[1] != cand[2]:
            raise InvariantError(f"SEMANTIC {record_id}: amounts differ, so the pair is not even admissible")
        notes.append(f"SEMAN  {record_id} {sorted(led_cores)} vs {settlement_ref} {sorted(cand_cores)} "
                     f"— disjoint, incomparable widths ({label})")

    # -- 4. pending vs missing are decided by AS_OF, not by today -------
    pending = ledger_by_id["ZB-6107"]
    missing = ledger_by_id["ORD-7034"]
    if datetime.fromisoformat(pending[2]).replace(tzinfo=timezone.utc) <= PENDING_CUTOFF:
        raise InvariantError("PENDING: ZB-6107 is old enough that a settlement would be due — it would read MISSING")
    if datetime.fromisoformat(missing[2]).replace(tzinfo=timezone.utc) > PENDING_CUTOFF:
        raise InvariantError("MISSING: ORD-7034 is inside the settlement window and would read PENDING")
    latest = max(datetime.fromisoformat(value_day)
                 for entries in (HDFC_MARCH, HDFC_APRIL, ICICI_MARCH)
                 for _txn, value_day, *_rest in entries)
    if latest.replace(tzinfo=timezone.utc) != AS_OF:
        raise InvariantError(f"AS_OF: latest bank value date is {latest.date()}, not {AS_OF.date()}")
    notes.append(f"TIME   as_of {AS_OF.date()} (latest value date), pending cutoff {PENDING_CUTOFF.date()}; "
                 f"ZB-6107 {pending[2]} pending, ORD-7034 {missing[2]} missing")

    # -- 5. the fee/tax exception genuinely does not add up -------------
    broken = next(r for r in PAYU_ROWS if r[5] is not None)
    gross = minor(broken[1])
    fee, tax = fee_tax(gross)
    if minor(broken[5]) == gross - fee - tax:
        raise InvariantError("FEE/TAX: the stated net actually reconciles — the exception would not fire")
    notes.append(f"FEETAX {broken[0]} stated net {minor(broken[5])} vs gross-fee-tax {gross - fee - tax} "
                 f"(difference {minor(broken[5]) - (gross - fee - tax)} paise)")

    # -- 6. exactly one settlement per reference, except where intended -
    by_reference: dict[str, list[str]] = {}
    for payment_id, reference, _amount, _day, _text in settlements:
        by_reference.setdefault(normalize.normalize_reference(reference), []).append(payment_id)
    intended_duplicates = {
        normalize.normalize_reference("INV-3050"): "S8 same payment in the gateway file and the bank",
        normalize.normalize_reference("INV-3065"): "S7 the refund report also carries this reference",
        normalize.normalize_reference("INV-3072"): "S7b the chargeback report also carries this reference",
        normalize.normalize_reference("KWY-88226"): "a chargeback against an unbooked marketplace payout",
    }
    unexpected = {ref: ids for ref, ids in sorted(by_reference.items())
                  if len(ids) > 1 and ref not in intended_duplicates}
    if unexpected:
        raise InvariantError(f"DUPLICATE REFERENCE: unintended collisions {unexpected}")
    for ref, why in sorted(intended_duplicates.items()):
        if len(by_reference.get(ref, [])) < 2:
            raise InvariantError(f"DUPLICATE REFERENCE: {ref} was meant to appear twice ({why}) but does not")
    notes.append(f"REFS   {len(by_reference)} distinct settlement references, "
                 f"{len(intended_duplicates)} deliberately doubled")

    # -- 7. no accidental aggregation ----------------------------------
    # detect_aggregated_settlements reports a group only when it is the
    # unique 2-or-3 subset of unmatched records summing to a settlement.
    # Anything else that sums would either steal the finding or bury it.
    from itertools import combinations
    pool = [(rid, amount, day) for rid, _ref, amount, day, _text in ledger
            if rid in POSSIBLY_UNMATCHED]
    if len(pool) != len(POSSIBLY_UNMATCHED):
        raise InvariantError("AGGREGATION: POSSIBLY_UNMATCHED names a record that is not in the ledger")
    hits: list[tuple[str, tuple[str, ...]]] = []
    for payment_id, _reference, gross, day, _text in settlements:
        when = datetime.fromisoformat(day)
        eligible = [p for p in pool if p[1] < gross
                    and abs((datetime.fromisoformat(p[2]) - when).days) <= 21]
        for size in (2, 3):
            for group in combinations(eligible, size):
                if abs(sum(g[1] for g in group) - gross) <= 2:
                    hits.append((payment_id, tuple(g[0] for g in group)))
    expected = [("UTR881318", AGG_A_MEMBERS)]
    if sorted(hits) != sorted(expected):
        raise InvariantError(f"AGGREGATION: expected exactly {expected}, found {sorted(hits)}")
    notes.append(f"AGGR   the only 2-3 record combination summing to any settlement is "
                 f"{' + '.join(AGG_A_MEMBERS)} -> UTR881318")

    # -- 8. ledger amount collisions are only the ones the demo names ---
    amounts: dict[int, list[str]] = {}
    for rid, _ref, amount, _day, _text in ledger:
        amounts.setdefault(amount, []).append(rid)
    collisions = {amount: ids for amount, ids in sorted(amounts.items()) if len(ids) > 1}
    if collisions != {minor("8650.00"): ["ORD-7031", "ORD-7032"]}:
        raise InvariantError(f"AMOUNTS: unintended ledger amount collisions {collisions}")
    notes.append("AMTS   the only two ledger records sharing an amount are the trap pair")

    return notes


# ---------------------------------------------------------------------------
# ingestion verification — the current detector, on the real bytes
# ---------------------------------------------------------------------------

# What each file is, and the one mapping a human is expected to confirm.
FILE_PLAN: list[tuple[str, str, dict[str, str]]] = [
    ("invoices_internal_export.csv", "ORDERS", {}),
    # `Name` is Shopify's order number, but the detector reads a bare
    # "Name" column as a counterparty, which is the more common reading.
    # This is the one confirmation the demo asks for, on purpose.
    ("orders_shopify_export.csv", "ORDERS", {"reference": "Name"}),
    ("tally_sales_register.csv", "ACCOUNTING", {}),
    ("zoho_books_invoices.csv", "ACCOUNTING", {}),
    ("razorpay_settlements_mar_apr.csv", "PAYMENT_GATEWAY", {}),
    ("cashfree_settlements_mar.csv", "PAYMENT_GATEWAY", {}),
    ("payu_settlements_mar.csv", "PAYMENT_GATEWAY", {}),
    ("kartway_marketplace_payout.csv", "PAYMENT_GATEWAY", {}),
    ("refunds_chargebacks_mar2026.csv", "PAYMENT_GATEWAY", {}),
    ("bank_hdfc_current_mar2026.csv", "BANK_STATEMENT", {}),
    ("bank_hdfc_current_apr2026.xlsx", "BANK_STATEMENT", {}),
    ("bank_icici_escrow_mar2026.xlsx", "BANK_STATEMENT", {}),
]

# record_id -> (scenario tag, what the demo script claims should happen)
SCENARIO_INDEX: dict[str, tuple[str, str]] = {
    "ORD-7021": ("S2a semantic bank narration", "RECONCILED via the model, or HUMAN_REVIEW below threshold"),
    "ZB-6104": ("S2b semantic marketplace payout", "RECONCILED via the model, or HUMAN_REVIEW below threshold"),
    "ORD-7031": ("S3 identical-amount trap", "EXCEPTION — refused, never auto-matched"),
    "ORD-7032": ("S3 the trap's twin", "RECONCILED on its own exact reference"),
    "BR-4471": ("S4a aggregated settlement", "HUMAN_REVIEW / AGGREGATED_SETTLEMENT"),
    "BR-4472": ("S4a aggregated settlement", "HUMAN_REVIEW / AGGREGATED_SETTLEMENT"),
    "BR-4473": ("S4a aggregated settlement", "HUMAN_REVIEW / AGGREGATED_SETTLEMENT"),
    "ZB-6107": ("S5a pending", "EXCEPTION / PENDING_SETTLEMENT"),
    "ORD-7034": ("S5b missing", "EXCEPTION / MISSING_SETTLEMENT"),
    "BR-4481": ("S6 fee/tax arithmetic", "EXCEPTION / FEE_TAX_INCONSISTENT"),
    "ORD-7036": ("S7 refund offset", "RECONCILED"),
    "ORD-7037": ("S7b chargeback not booked", "EXCEPTION / REFUND_MISMATCH"),
    "ORD-7104": ("S8 same payment in two sources", "HUMAN_REVIEW / DUPLICATE_REFERENCE"),
    "ORD-7038": ("S9 currency mismatch", "EXCEPTION / CURRENCY_MISMATCH"),
    "SH-88211": ("S10 ambiguous for the model too", "HUMAN_REVIEW"),
    "ORD-7040": ("S11 truncated reference", "RECONCILED via the model, or HUMAN_REVIEW"),
}


def read_file(path: Path) -> tuple[list[str], list[dict], str, int | None, list[str]]:
    """Columns and rows for a CSV or an XLSX, through the app's own reader
    when it exists and a CSV-only fallback when it does not."""
    raw = path.read_bytes()
    try:
        from app.ingest.reader import read_table
    except Exception:                                     # noqa: BLE001
        if path.suffix.lower() == ".xlsx":
            return [], [], "xlsx", None, ["reader.py not available: XLSX verification skipped"]
        from app.ingest.schema import parse_csv
        columns, rows = parse_csv(raw.decode("utf-8-sig"))
        return columns, rows, "csv", 1, ["read with schema.parse_csv (reader.py not available)"]
    result = read_table(path.name, raw)
    return result.columns, result.rows, result.fmt, result.header_row, list(result.notes)


def verify(out_dir: Path, built: dict) -> int:
    import os

    # The offline heuristic verifier, forced: no key is read, no network
    # call is made, and the run is reproducible. It means the two
    # genuinely semantic records cannot resolve here — that is the point
    # of them, and it is reported rather than hidden.
    os.environ["ACCORD_AI_DISABLED"] = "1"

    from app.domain.models import PolicyConfig, ReconciliationRecord
    from app.engine.batch import process_batch
    from app.ingest.mapper import combine, map_rows
    from app.ingest.schema import SourceType, detect_schema

    failures: list[str] = []

    print("=" * 78)
    print("INVARIANTS")
    print("=" * 78)
    for line in check_invariants(built):
        print("  " + line)

    print()
    print("=" * 78)
    print("SCHEMA DETECTION  (app.ingest.schema.detect_schema on the real bytes)")
    print("=" * 78)

    mapped_sources = []
    for filename, source_type, overrides in FILE_PLAN:
        path = out_dir / filename
        columns, rows, fmt, header_row, notes = read_file(path)
        if not columns:
            failures.append(f"{filename}: could not be read ({'; '.join(notes) or 'no columns'})")
            print(f"\n  {filename}\n    UNREADABLE: {'; '.join(notes)}")
            continue
        detected = detect_schema(columns, rows)
        mapping = dict(detected.mapping)
        for canonical, column in overrides.items():
            for existing, taken in list(mapping.items()):
                if taken == column:
                    del mapping[existing]
            mapping[canonical] = column

        unmapped = [g.column for g in detected.guesses if g.canonical is None]
        print(f"\n  {filename}   [{fmt}"
              + (f", header row {header_row}" if fmt == "xlsx" else "")
              + f", {detected.row_count} rows, amounts={detected.amount_scale}]")
        print(f"    mapping   " + ", ".join(f"{k}={v}" for k, v in sorted(detected.mapping.items())))
        if detected.debit_column:
            print(f"    paired    debit={detected.debit_column} credit={detected.credit_column}")
        if unmapped:
            print(f"    unmapped  {', '.join(unmapped)}")
        if detected.unmapped_required:
            print(f"    BLOCKED   required field(s) unresolved: {', '.join(detected.unmapped_required)}")
            failures.append(f"{filename}: required field(s) unresolved: {detected.unmapped_required}")
        if overrides:
            print(f"    CONFIRM   user maps " + ", ".join(f"{k} -> {v}" for k, v in overrides.items()))
        for note in notes:
            print(f"    note      {note}")

        mapped_sources.append(map_rows(
            rows, mapping, SourceType(source_type), filename,
            detected.amount_scale, detected.debit_column, detected.credit_column,
            filename=filename,
        ))
        rejected = mapped_sources[-1].rejected
        if rejected:
            failures.append(f"{filename}: {len(rejected)} row(s) rejected: {rejected[:2]}")
            print(f"    REJECTED  {len(rejected)} row(s): {rejected[:2]}")

    ledger, settlements, rejected = combine(mapped_sources)
    print()
    print("=" * 78)
    print("RECONCILIATION  (deterministic tiers only; ACCORD_AI_DISABLED=1)")
    print("=" * 78)
    print(f"  {len(ledger)} ledger records, {len(settlements)} settlement records, "
          f"{len(rejected)} rejected rows")

    records = [ReconciliationRecord(record_id=r.order_id, merchant=r) for r in ledger]
    results = process_batch(records, settlements, policy=PolicyConfig())
    derived_as_of = max(s.settlement_date for s in settlements)
    print(f"  derived as_of = {derived_as_of.isoformat()}  (expected {AS_OF.isoformat()})")
    if derived_as_of != AS_OF:
        failures.append(f"derived as_of {derived_as_of} != AS_OF {AS_OF}")

    counts: dict[str, int] = {}
    for result in results:
        counts[result.outcome.value] = counts.get(result.outcome.value, 0) + 1
    print("  outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    no_ai = sum(1 for r in results if not r.ai_invoked)
    print(f"  {no_ai}/{len(results)} records decided without any model call")

    print()
    print("  scenario records")
    print(f"  {'record':<10} {'outcome':<14} {'exception':<24} {'classification':<28} scenario")
    for record_id, (tag, _expected) in sorted(SCENARIO_INDEX.items()):
        result = next((r for r in results if r.record_id == record_id), None)
        if result is None:
            failures.append(f"scenario record {record_id} is missing from the run")
            print(f"  {record_id:<10} MISSING")
            continue
        print(f"  {record_id:<10} {result.outcome.value:<14} "
              f"{(result.exception_type.value if result.exception_type else '-'):<24} "
              f"{result.classification.value:<28} {tag}")

    print()
    print("  reproducibility")
    digests = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(out_dir.iterdir()) if p.name != "_manifest.json"}
    for name, digest in digests.items():
        print(f"    {digest[:16]}  {name}")

    print()
    if failures:
        print("VERIFICATION FAILURES")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("Verification passed: every file read, every required field mapped, "
          "every invariant held.")
    return 0


def write_manifest(out_dir: Path, built: dict) -> None:
    files = []
    for path in sorted(out_dir.iterdir()):
        if path.name == "_manifest.json":
            continue
        files.append({
            "filename": path.name,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    manifest = {
        "generator": "generate_demo_workspace.py",
        "seed": SEED,
        "as_of": AS_OF.isoformat(),
        "pending_cutoff": PENDING_CUTOFF.isoformat(),
        "ledger_record_count": len(ledger_population()),
        "settlement_record_count": len(settlement_population(built)),
        "aggregation": {
            "AGG_A": {"members": list(AGG_A_MEMBERS), "bank_ref": "UTR881318",
                      "total_minor": built["agg_a_total_minor"]},
            "AGG_B": {"members": list(AGG_B_MEMBERS), "bank_ref": "UTR774008",
                      "total_minor": built["agg_b_total_minor"]},
        },
        "scenarios": {k: {"tag": v[0], "expected": v[1]} for k, v in sorted(SCENARIO_INDEX.items())},
        "files": files,
    }
    (out_dir / "_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--verify", action="store_true",
                        help="also feed every generated file back through the real detector and engine")
    args = parser.parse_args()

    built = build_files(args.out_dir)
    check_invariants(built)
    write_manifest(args.out_dir, built)

    ledger = ledger_population()
    settlements = settlement_population(built)
    print(f"Demo workspace written to {args.out_dir}")
    print(f"  {len(list(args.out_dir.iterdir())) - 1} data files, "
          f"{len(ledger)} ledger rows, {len(settlements)} settlement rows")
    print(f"  observation point pinned at {AS_OF.date()}")

    if args.verify:
        print()
        return verify(args.out_dir, built)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
