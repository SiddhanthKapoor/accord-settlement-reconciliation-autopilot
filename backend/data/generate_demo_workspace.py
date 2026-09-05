"""
The demo workspace: twenty sources, one story, no randomness in the story.

`generate_demo_data.py` writes three files in three shapes and is fine for
a unit-sized demo. This writes the thing a finance team actually hands
over, and it is deliberately one month of one company: March 2026 at
Sahyadri Coffee Works Private Limited, a Bengaluru specialty coffee
equipment retailer and distributor that sells through its own storefront,
a retail counter, a marketplace channel and a B2B distributor book.

Seven ledger sources (an internal invoice book, a storefront order
export, a retail counter register, a Shopify export, a Tally sales
register, an ERP general ledger, a Zoho Books export) and thirteen
settlement sources (two Razorpay exports at different points in the money
flow, a card acquirer's POS settlement report, a nodal-account payout
advice, a UPI collections report, a collections-account sweep advice, a
marketplace payout, a gateway fee-and-adjustment register, a
refund/chargeback report, and four bank statements across three accounts)
— plus one file that is a byte-identical re-upload of another.

Only some of the settlement sources name a provider at all. Four of them
carry no vendor identity anywhere in the bytes — no branded column, no
branded filename — and are classified purely on what their columns mean.
That is deliberate: the detector reads semantics, not logos.

Roughly seven thousand records, in two halves that are built very
differently on purpose:

  * every record that carries a *claim* — every case the demo script
    names, and every record that can end a run unmatched — is written by
    hand, one row at a time, and individually asserted below.
  * the ordinary majority is generated from a second fixed seed under
    constraints strict enough that it cannot accidentally become
    interesting: amounts unique to well outside the matcher's tolerance,
    never equal to any 2- or 3-way sum of the unresolved population, and
    drawn from wording that shares nothing distinctive with the
    hand-written cases.

There is no `datetime.now()` anywhere in the output — the observation
point is the AS_OF constant below, so the demo does not decay — and two
runs produce byte-identical files, XLSX included.

What the set is built to show, in the order a video would show it:

  1. nearly all of it reconciles on an exact reference, instantly, with
     no model call at all — the boring majority is the point
  2. records that only a semantic model can resolve, built so the
     identifier is genuinely absent or in another namespace
  3. an identical-amount trap that must be REFUSED
  4. two aggregated settlements whose arithmetic ties out to the paisa,
     proposed and never auto-booked
  5. pending vs missing, pinned to AS_OF rather than to today
  6. exceptions that are known-wrong money: amounts that disagree, payout
     arithmetic that does not hold, refunds one side never booked, a
     cross-currency booking, a settlement three weeks late
  7. the same payment present in two sources, and one reference reported
     twice by the same provider
  8. a merchant alias, a gateway-reformatted reference, a truncated one
  9. a record even a model should call ambiguous
 10. a duplicate file, and a source the classifier is not confident
     enough about to run without asking

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
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
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


def swept(rupees: str | float, marker: str = "Cr") -> str:
    """How a collection-account advice writes money: Indian digit
    grouping with the direction marked inside the cell rather than as a
    sign, a bracket or a second column. 21600 -> '21,600.00 Cr'."""
    return f"{grouped(rupees)} {marker}"


def ist(day: str, hhmmss: str = "18:30:00") -> str:
    """An ISO-8601 instant carrying a real +05:30 offset.

    The storefront export writes the same shape in UTC with a trailing Z.
    This is the other half of that convention and the only place in the
    workspace where a timestamp is not already UTC, which is worth having
    in the set: `schema.parse_date` reads both through
    `datetime.fromisoformat`, and everything downstream is offset-aware."""
    return f"{day}T{hhmmss}+05:30"


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
    ("SH-88201", "2026-03-06 09:14:22", "paid", "2949.15", "120.00", "410.85", "3480.00", "1", "Ceramic pour-over kettle 1.2L", "Web order - ceramic pour-over kettle", "orders@northwind-retail.example", "Payment Link"),
    ("SH-88202", "2026-03-08 11:02:47", "paid", "1067.80", "0.00", "192.20", "1260.00", "2", "Paper filter pack 100s", "Web order - paper filter refill pack", "orders@kalyantech.example", "Payment Link"),
    ("SH-88203", "2026-03-10 15:38:09", "paid", "5033.90", "0.00", "906.10", "5940.00", "1", "Burr grinder compact", "Web order - compact burr grinder", "orders@sunrise-commerce.example", "Payment Link"),
    ("SH-88204", "2026-03-12 08:51:33", "paid", "1817.80", "0.00", "327.20", "2145.00", "3", "Insulated travel tumbler", "Web order - insulated travel tumbler", "orders@meridian-softworks.example", "Payment Link"),
    ("SH-88205", "2026-03-14 17:26:55", "paid", "7050.85", "220.00", "1049.15", "8320.00", "1", "Espresso machine entry tier", "Web order - entry tier espresso machine", "orders@anand-traders.example", "Payment Link"),
    ("SH-88206", "2026-03-16 10:07:12", "paid", "838.98", "0.00", "151.02", "990.00", "1", "Cleaning tablet jar", "Web order - cleaning tablet jar", "orders@bluepeak.example", "Payment Link"),
    ("SH-88207", "2026-03-18 13:45:26", "paid", "3961.86", "0.00", "713.14", "4675.00", "2", "Milk frothing jug set", "Web order - milk frothing jug set", "orders@vantage-analytics.example", "Payment Link"),
    ("SH-88208", "2026-03-20 19:12:40", "paid", "10881.36", "300.00", "1658.64", "12840.00", "1", "Roasting drum accessory", "Web order - roasting drum accessory", "orders@harbourline.example", "Payment Link"),
    ("SH-88209", "2026-03-22 07:33:18", "paid", "2025.42", "0.00", "364.58", "2390.00", "1", "Scale with timer", "Web order - brewing scale with timer", "orders@northwind-retail.example", "Payment Link"),
    ("SH-88210", "2026-03-24 16:20:04", "paid", "5211.86", "0.00", "938.14", "6150.00", "1", "Cold brew tower", "Web order - cold brew tower", "orders@kalyantech.example", "Payment Link"),
    # ---- S10 vague on both sides. Nothing here says what it was for.
    ("SH-88211", "2026-03-25 16:40:11", "paid", "3491.53", "0.00", "628.47", "4120.00", "1", "Assorted items", "Counter sale payment received", "", "Other"),
    ("SH-88212", "2026-03-28 12:04:59", "paid", "6190.68", "0.00", "1114.32", "7305.00", "1", "Bench grinder pro tier", "Web order - pro tier bench grinder", "orders@sunrise-commerce.example", "Payment Link"),
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

# The collections-account sweep advice. Not a gateway export and not a
# bank statement: the advice the business's collection account operator
# issues when it sweeps the day's net-banking, payment-link and card
# collections into the current account, one line per collection with its
# own charge and tax breakdown.
#
# It is the one settlement source in the workspace with no provider
# branding anywhere in it — no vendor column prefix, no vendor id shape,
# no vendor token in the filename — which is deliberate. The classifier's
# provider table is a label, not a gate: this file has to reach
# PAYMENT_GATEWAY confidently on its column semantics alone and report
# `provider=None`, and `--verify` would notice if it stopped doing so.
#
# Its own conventions, shared with nothing else in the set: money as
# Indian-grouped rupees with a Cr/Dr marker inside the cell
# ("21,600.00 Cr", "432.00 Dr"), and dates as ISO-8601 instants carrying
# a real +05:30 offset rather than UTC.
#
# (order_ref, gross, paid_stamp, swept_day, mode, counterparty, detail)
COLLECTION_SWEEP_ROWS = [
    ("INV-3013", "3125.00", "2026-03-10 11:42:08", "2026-03-12", "NET BANKING", "Anand Traders Private Limited", "Payment for INV-3013 widget bundle standard"),
    ("INV-3014", "7480.00", "2026-03-10 14:19:55", "2026-03-12", "NET BANKING", "Bluepeak Services Pvt Ltd", "Payment for INV-3014 team seat upgrade"),
    ("INV-3015", "21600.00", "2026-03-11 09:05:41", "2026-03-13", "NET BANKING", "Vantage Analytics Pvt Ltd", "Payment for INV-3015 analytics add-on"),
    ("INV-3016", "1299.00", "2026-03-11 18:27:03", "2026-03-13", "PAYMENT LINK", "Harbourline Foods Pvt Ltd", "Payment for INV-3016 starter plan monthly"),
    ("INV-3017", "5675.00", "2026-03-12 12:58:20", "2026-03-14", "NET BANKING", "Northwind Retail Private Limited", "Payment for INV-3017 express shipping add-on"),
    ("SH-88201", "3480.00", "2026-03-06 09:15:02", "2026-03-08", "PAYMENT LINK", "Web customer", "Payment for SH-88201 ceramic pour-over kettle"),
    ("SH-88202", "1260.00", "2026-03-08 11:03:31", "2026-03-10", "PAYMENT LINK", "Web customer", "Payment for SH-88202 paper filter refill pack"),
    ("SH-88203", "5940.00", "2026-03-10 15:38:52", "2026-03-12", "CARD", "Web customer", "Payment for SH-88203 compact burr grinder"),
    ("SH-88204", "2145.00", "2026-03-12 08:52:11", "2026-03-14", "PAYMENT LINK", "Web customer", "Payment for SH-88204 insulated travel tumbler"),
    ("SH-88205", "8320.00", "2026-03-14 17:27:38", "2026-03-16", "CARD", "Web customer", "Payment for SH-88205 entry tier espresso machine"),
    ("SH-88206", "990.00", "2026-03-16 10:07:49", "2026-03-18", "PAYMENT LINK", "Web customer", "Payment for SH-88206 cleaning tablet jar"),
    ("SH-88207", "4675.00", "2026-03-18 13:46:07", "2026-03-20", "PAYMENT LINK", "Web customer", "Payment for SH-88207 milk frothing jug set"),
    ("SH-88208", "12840.00", "2026-03-20 19:13:22", "2026-03-22", "CARD", "Web customer", "Payment for SH-88208 roasting drum accessory"),
    ("SH-88209", "2390.00", "2026-03-22 07:33:59", "2026-03-24", "PAYMENT LINK", "Web customer", "Payment for SH-88209 brewing scale with timer"),
    ("SH-88210", "6150.00", "2026-03-24 16:20:47", "2026-03-26", "PAYMENT LINK", "Web customer", "Payment for SH-88210 cold brew tower"),
    # ---- S3 the trap's real owner. Same amount as INV-3042, a day apart.
    ("INV-3117", "8650.00", "2026-03-25 10:11:34", "2026-03-27", "NET BANKING", "Harbourline Foods Pvt Ltd", "Payment for INV-3117 refrigeration unit deposit"),
    # ---- S11 the reference is truncated: the real one is INV-3062 -----
    ("INV-306", "13475.00", "2026-03-27 15:44:26", "2026-03-29", "NET BANKING", "Sunrise Commerce LLP", "Payment for INV-306 consulting retainer block"),
    ("SH-88212", "7305.00", "2026-03-28 12:05:37", "2026-03-30", "PAYMENT LINK", "Web customer", "Payment for SH-88212 pro tier bench grinder"),
    ("ZB-6106", "4990.00", "2026-03-31 09:48:15", "2026-04-02", "NET BANKING", "Harbourline Foods Pvt Ltd", "Payment for ZB-6106 cold storage compliance audit"),
]

# The payment aggregator's nodal-account payout advice: rupee symbol,
# thousands separators, dd/mm/yyyy timestamps, and no vendor identity
# anywhere in it — the file is classified on its columns alone.
# (merchant_ref, gross, paid_day, paid_time, payout_day,
#  net_override_or_None, mode)
NODAL_ROWS = [
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

# ---------------------------------------------------------------------------
# SCALE — the six large sources, and the hand-written rows inside them
# ---------------------------------------------------------------------------
# Everything above is written one row at a time because every one of
# those rows is a specific claim the demo script makes. What follows is
# the opposite: several thousand records that are deliberately
# unremarkable. "Nearly all of it reconciles on arithmetic, with no model
# call" is the product's actual claim, and a fifty-four-record workspace
# is far too small to demonstrate it.
#
# The bulk is generated from its own seed — deliberately a *separate*
# seed from IdMint's, so that adding it does not perturb a single byte of
# the thirteen original files — and it is generated under constraints
# rather than freely:
#
#   * every generated amount is unique to well outside the engine's
#     2-paise tolerance, never lands on an amount that a hand-written
#     unresolved record carries, and never lands on any 2- or 3-way sum
#     of those amounts. Either would manufacture a coincidence the engine
#     then has to reason about, and a coincidence nobody designed is a
#     coincidence nobody asserted.
#   * counterparties and product wording are disjoint from the
#     hand-written scenarios, so a bulk description cannot accidentally
#     corroborate a scenario record.
#   * every record that ends a run *unmatched* is hand-written and named
#     below. That is not tidiness: `batch.detect_aggregated_settlements`
#     skips itself entirely once the unmatched population passes
#     PolicyConfig.max_aggregation_candidates (40), so an unbounded
#     missing-settlement rate at this scale would silently take the
#     aggregation finding with it.
#
# Anything that *is* matched and then fails a financial check costs the
# unmatched budget nothing, so the ordinary exception traffic — amounts
# that disagree, payout files whose arithmetic does not hold, refunds one
# side never booked, a cross-currency booking, a settlement that arrived
# far too late — is generated at a realistic rate.

BULK_SEED = 91744

BULK_START = "2026-03-01"
# T+2 settlement on the last generated day still lands before AS_OF, so
# the observation point stays exactly where the bank statements put it.
BULK_END = "2026-04-10"

# Counts, split so the totals in the docs cannot drift from the data.
WEB_TOTAL = 1600
ERP_TOTAL = 1050
POS_TOTAL = 800

# Fictional. Deliberately not any real business, and deliberately sharing
# no distinctive word with the eight counterparties the hand-written
# scenarios use, so bulk wording can never corroborate a scenario record.
BULK_PARTIES = (
    "Vaayu Organics Private Limited",
    "Meghdoot Packaging LLP",
    "Kesari Handlooms Pvt Ltd",
    "Palashwood Interiors Pvt Ltd",
    "Chandravat Metals Private Limited",
    "Nirvaan Agro Mills Pvt Ltd",
    "Suvarna Weaves LLP",
    "Girnar Pipes and Fittings Pvt Ltd",
    "Barkat Stationers Private Limited",
    "Neelkanth Ceramics Pvt Ltd",
    "Tarangini Prints LLP",
    "Ambar Cold Rooms Pvt Ltd",
    "Rukmini Provisions Private Limited",
    "Sanchit Hardware Depot Pvt Ltd",
    "Devgiri Paints LLP",
    "Kaveri Rubber Works Pvt Ltd",
    "Prithvi Seeds and Feeds Pvt Ltd",
    "Ojas Lighting Private Limited",
    "Marudhar Furnishings LLP",
    "Anokhi Kitchenware Pvt Ltd",
    "Bhavani Steel Trading Pvt Ltd",
    "Shubhankar Electricals LLP",
    "Naaz Fabrics Private Limited",
    "Ranjeet Timber Depot Pvt Ltd",
    "Utsav Bakers LLP",
    "Kalanjali Crafts Pvt Ltd",
    "Vindhya Plastics Private Limited",
    "Jashan Sports Goods Pvt Ltd",
    "Sohrab Auto Spares LLP",
    "Madhuban Nurseries Pvt Ltd",
    "Ekansh Glassworks Private Limited",
    "Yamini Home Needs Pvt Ltd",
    "Bhoomi Tiles and Sanitary LLP",
    "Chirag Battery House Pvt Ltd",
    "Tanmay Toolroom Private Limited",
    "Sitara Packaging Solutions Pvt Ltd",
)

WEB_ITEMS = (
    "Cotton bedsheet double", "Steel tiffin carrier", "LED desk lamp", "Yoga mat 6mm",
    "Wall clock teak", "Backpack 32L", "Copper water bottle", "Ruled notebook A5",
    "Wireless mouse", "Aluminium phone stand", "Cushion cover pair", "Coir door mat",
    "Steel spice box", "Bath towel set", "Sandalwood soap box", "Floating wall shelf",
    "Trolley bag 24 inch", "Linen table runner", "Photo frame set", "Hand blender 300W",
    "Pressure cooker 3L", "Mixer jar spare", "Rice storage bin", "Folding umbrella",
    "Laptop sleeve 14 inch", "Cable organiser box", "Rubber foot mat", "Curtain pair 7ft",
    "Wooden chopping board", "Stainless lunch flask",
)

ERP_NARRATIVES = (
    "Consignment despatch", "Contract manufacturing charges", "Freight recovery billing",
    "Job work conversion charges", "Scrap sale realisation", "Warehouse handling recovery",
    "Packaging material supply", "Machinery hire charges", "Tooling amortisation billing",
    "Secondary transport recovery", "Rework charges billed", "Sample despatch billing",
    "Bulk despatch schedule A", "Bulk despatch schedule B", "Annual maintenance billing",
    "Site erection charges", "Testing and calibration charges", "Spare kit despatch",
    "Design retainer billing", "Mould development charges", "Trial batch supply",
    "Export documentation recovery", "Labelling job billing", "Palletisation charges",
)

POS_ITEMS = (
    "Grocery basket", "Stationery pack", "Hardware fittings", "Paint tin 4L",
    "Plumbing fittings", "Electrical wire roll", "Garden tool set", "Cleaning supplies",
    "Crockery half set", "Bedding roll", "Sports kit", "Battery pack AA",
    "Torch and cells", "Adhesive carton", "Fastener assortment", "Measuring tape 5m",
    "Extension board", "Bulb pack of six", "Mop and bucket", "Sponge pack",
    "Storage crate", "Ladder step 4ft", "Tarpaulin sheet", "Cable tie pack",
)

WEB_CHANNELS = ("Web", "Mobile app", "Phone order", "Marketplace")
POS_STORES = ("BLR-01", "BLR-02", "MYS-01", "HYD-01", "PNQ-01")
POS_MODES = ("CARD", "UPI", "WALLET", "NETBANKING")
UPI_BANKS = ("okhdfcbank", "okicici", "okaxis", "okkotak", "ybl", "oksbi")


# ---- the hand-written records inside the large sources -------------------
# Every record that ends a run unmatched is here, written by hand and
# individually asserted, exactly like the original thirteen files. Only
# the records that reconcile (or that are matched and then fail a
# financial check) are generated.
#
# (record_id, stream, day, amount, counterparty, item, note, scenario)
NEW_LEDGER_SCENARIOS = (
    # ---- C  the only trace of this money is a bank narration ----------
    ("WB-104217", "WEB", "2026-03-19", "46185.00", "Vaayu Organics Private Limited",
     "Cold pressed oil case pack", "Cold pressed oil case pack bulk despatch", "N1"),
    # ---- D  merchant alias: trading name on one side, legal name the other
    ("WB-104931", "WEB", "2026-03-26", "58940.00", "Trisool Online Pvt Ltd",
     "Quarterly platform licence", "Quarterly platform licence", "N2"),
    # ---- A  the gateway reformatted the reference; still deterministic
    ("WB-105204", "WEB", "2026-03-27", "19430.00", "Kesari Handlooms Pvt Ltd",
     "Handloom fabric bulk consignment", "Handloom fabric bulk consignment", "N3"),
    # ---- F  one reference, two settlements, both matching on amount ----
    ("WB-105633", "WEB", "2026-03-23", "27180.00", "Neelkanth Ceramics Pvt Ltd",
     "Glazed tile carton lot", "Glazed tile carton lot", "N5"),
    # ---- E  genuinely missing, no lookalike anywhere -------------------
    ("WB-106402", "WEB", "2026-03-13", "22900.00", "Barkat Stationers Private Limited",
     "Office paper quarterly supply", "Office paper quarterly supply", "N7"),
    # ---- pending: captured after the T+2 cutoff ------------------------
    ("WB-106988", "WEB", "2026-04-14", "9875.00", "Ojas Lighting Private Limited",
     "Track light spares", "Track light spares", "N8"),
    ("WB-107455", "WEB", "2026-04-15", "14260.00", "Yamini Home Needs Pvt Ltd",
     "Storage rack flat pack", "Storage rack flat pack", "N8"),
    # ---- F  two same-amount credits from the same counterparty ---------
    ("GLX-204880", "ERP", "2026-03-30", "33750.00", "Meghdoot Packaging LLP",
     "Consignment despatch", "Consignment despatch schedule 14", "N6"),
    ("GLX-206115", "ERP", "2026-03-24", "41320.00", "Kaveri Rubber Works Pvt Ltd",
     "Job work conversion charges", "Job work conversion charges batch 71", "N7"),
    ("GLX-207209", "ERP", "2026-04-01", "36415.00", "Girnar Pipes and Fittings Pvt Ltd",
     "Freight recovery billing", "Freight recovery billing April cycle", "N5"),
    # ---- E  amount mismatch nothing explains --------------------------
    ("POS-300412", "POS", "2026-03-23", "27460.00", "Sanchit Hardware Depot Pvt Ltd",
     "Hardware fittings", "Counter bill hardware fittings", "N4"),
    # ---- B  two bills, one consolidated bank credit --------------------
    ("POS-300771", "POS", "2026-04-02", "12340.00", "Devgiri Paints LLP",
     "Paint tin 4L", "Counter bill paint tin lot", "N9"),
    ("POS-300779", "POS", "2026-04-03", "8915.00", "Bhoomi Tiles and Sanitary LLP",
     "Plumbing fittings", "Counter bill plumbing fittings", "N9"),
    ("POS-302640", "POS", "2026-03-17", "5680.00", "Chirag Battery House Pvt Ltd",
     "Battery pack AA", "Counter bill battery pack lot", "N7"),
    ("POS-303551", "POS", "2026-04-14", "18435.00", "Utsav Bakers LLP",
     "Crockery half set", "Counter bill crockery half set", "N8"),
)

# Which of those never acquire a settlement of their own, and therefore
# make up the entire unmatched population of the large sources.
NEW_UNMATCHED = (
    "WB-104217", "WB-104931", "WB-105633", "WB-106402", "WB-106988", "WB-107455",
    "GLX-204880", "GLX-206115", "GLX-207209",
    "POS-300771", "POS-300779", "POS-302640", "POS-303551",
)

# The second aggregation: two POS bills, one Axis credit.
AGG_C_MEMBERS = ("POS-300771", "POS-300779")

# How many generated records carry each kind of matched-but-wrong
# finding, per stream. Matched records never enter the aggregation pool,
# so these are free of the unmatched budget above.
BULK_ANOMALY_PLAN = {
    "WEB": {"amount_mismatch": 11, "currency_mismatch": 4},
    "ERP": {"amount_mismatch": 7, "fee_tax": 7, "refund_mismatch": 5, "delayed": 4},
    "POS": {"amount_mismatch": 5, "fee_tax": 5, "refund_mismatch": 3, "delayed": 2},
}


@dataclass(frozen=True)
class BulkLedger:
    stream: str
    record_id: str
    day: str
    party: str
    item: str
    note: str
    amount_minor: int
    currency: str
    qty: int
    channel: str
    kind: str


@dataclass(frozen=True)
class BulkSettlement:
    stream: str
    txn_id: str
    reference: str
    day: str
    settled_day: str
    gross_minor: int
    fee_minor: int
    tax_minor: int
    net_minor: int
    refund_minor: int
    currency: str
    party: str
    note: str
    method: str


@dataclass(frozen=True)
class BulkPlan:
    web: tuple[BulkLedger, ...]
    erp: tuple[BulkLedger, ...]
    pos: tuple[BulkLedger, ...]
    razorpay_payments: tuple[BulkSettlement, ...]
    upi: tuple[BulkSettlement, ...]
    acquirer: tuple[BulkSettlement, ...]

    @property
    def ledger(self) -> tuple[BulkLedger, ...]:
        return self.web + self.erp + self.pos

    @property
    def settlements(self) -> tuple[BulkSettlement, ...]:
        return self.razorpay_payments + self.upi + self.acquirer


def day_range(start: str, end: str) -> list[str]:
    first = datetime.fromisoformat(start)
    last = datetime.fromisoformat(end)
    return [(first + timedelta(days=i)).date().isoformat() for i in range((last - first).days + 1)]


def shift(day: str, days: int) -> str:
    return (datetime.fromisoformat(day) + timedelta(days=days)).date().isoformat()


def handwritten_amounts() -> set[int]:
    """Every amount the hand-written half of the workspace already uses.

    Collected from the constants rather than from the mapped population,
    because the bulk generator needs it before anything has been built.
    """
    used: set[int] = set()
    for _oid, _inv, _cust, _day, amount, *_rest in INTERNAL_INVOICES:
        used.add(minor(amount))
    for _name, _created, _st, _sub, _ship, _tax, total, *_rest in SHOPIFY_ORDERS:
        used.add(minor(total))
    for _day, _v, _k, _ref, _p, credit in TALLY_ROWS:
        used.add(minor(credit))
    for _day, _n, _st, _c, _cur, total, *_rest in ZOHO_ROWS:
        used.add(minor(total))
    for _ref, gross, *_rest in RAZORPAY_ROWS:
        used.add(minor(gross))
    for _ref, gross, *_rest in COLLECTION_SWEEP_ROWS:
        used.add(minor(gross))
    for _ref, gross, *_rest in NODAL_ROWS:
        used.add(minor(gross))
    for _oid, _ws, _we, _p, sales, fees, total, _d in MARKETPLACE_ROWS:
        used.update({minor(sales), minor(abs(float(fees))), minor(total)})
    for entries in (HDFC_MARCH, HDFC_APRIL, ICICI_MARCH):
        for _txn, _value, _narration, _ref, withdrawal, deposit in entries:
            for value in (withdrawal, deposit):
                if value and not value.startswith("AGG_"):
                    used.add(minor(value))
    for _rid, _ref, _day, amount, *_rest in REFUND_ROWS:
        used.add(minor(amount))
    for _rid, _stream, _day, amount, *_rest in NEW_LEDGER_SCENARIOS:
        used.add(minor(amount))
    for _rid, _ref, amount, *_rest in NEW_SETTLEMENT_AMOUNTS:
        used.add(amount)
    # The two bank credits written as AGG_A / AGG_B placeholders above.
    used.add(sum(minor(c) for _d, _v, _k, ref, _p, c in TALLY_ROWS if ref in AGG_A_MEMBERS))
    used.add(sum(minor(g) - sum(fee_tax(minor(g)))
                 for r, g, *_rest in RAZORPAY_ROWS if r in AGG_B_MEMBERS))
    return used


def handwritten_ledger_amounts() -> dict[str, int]:
    """record_id -> amount in paise, for every hand-written ledger row in
    the workspace: the original four files and the rows placed by hand
    inside the three large ones."""
    out: dict[str, int] = {}
    for oid, _inv, _cust, _day, amount, *_rest in INTERNAL_INVOICES:
        out[oid] = minor(amount)
    for name, _created, _st, _sub, _sh, _tx, total, *_rest in SHOPIFY_ORDERS:
        out[name] = minor(total)
    for _day, _v, _k, ref, _p, credit in TALLY_ROWS:
        out[ref] = minor(credit)
    for _day, number, _st, _c, _cur, total, *_rest in ZOHO_ROWS:
        out[number] = minor(total)
    for rid, _stream, _day, amount, *_rest in NEW_LEDGER_SCENARIOS:
        out[rid] = minor(amount)
    return out


def _bulk_amount(rng: random.Random, deny: set[int], low: int, high: int) -> int:
    """A fresh amount in paise that collides with nothing.

    `deny` holds a +/-2 paise band around every amount already spoken
    for, which is the engine's own `amount_tolerance_minor`. Blocking the
    band rather than the point is what makes "unique" mean unique to the
    matcher and not merely unique as an integer.
    """
    for _ in range(4000):
        if rng.random() < 0.55:
            value = rng.randrange(low // 100, high // 100) * 100      # whole rupees
        else:
            value = rng.randrange(low, high)
        if value not in deny:
            deny.update(range(value - 2, value + 3))
            return value
    raise InvariantError("bulk amount space exhausted — widen the range or shrink the population")


_BULK_CACHE: BulkPlan | None = None


def bulk_plan() -> BulkPlan:
    global _BULK_CACHE
    if _BULK_CACHE is None:
        _BULK_CACHE = _build_bulk()
    return _BULK_CACHE


def _build_bulk() -> BulkPlan:
    rng = random.Random(BULK_SEED)

    deny: set[int] = set()
    for value in sorted(handwritten_amounts()):
        deny.update(range(value - 2, value + 3))

    # No generated amount may equal any 2- or 3-way sum of the unmatched
    # population, or the aggregation pass would report a decomposition
    # nobody designed and the one that *was* designed would stop being
    # unique.
    by_record = handwritten_ledger_amounts()
    missing = [r for r in POSSIBLY_UNMATCHED if r not in by_record]
    if missing:
        raise InvariantError(f"POSSIBLY_UNMATCHED names records that do not exist: {missing}")
    pool_amounts = sorted(by_record[r] for r in POSSIBLY_UNMATCHED)
    from itertools import combinations
    for size in (2, 3):
        for group in combinations(pool_amounts, size):
            total = sum(group)
            deny.update(range(total - 2, total + 3))

    parties = BULK_PARTIES

    def scenario_rows(stream: str) -> dict[str, BulkLedger]:
        out: dict[str, BulkLedger] = {}
        for rid, row_stream, day, amount, party, item, note, _tag in NEW_LEDGER_SCENARIOS:
            if row_stream != stream:
                continue
            out[rid] = BulkLedger(
                stream=stream, record_id=rid, day=day, party=party, item=item, note=note,
                amount_minor=minor(amount), currency="INR", qty=1,
                channel=WEB_CHANNELS[len(out) % len(WEB_CHANNELS)], kind="scenario",
            )
        return out

    def build_stream(stream: str, total: int, prefix: str, low: int, high: int,
                     items: tuple[str, ...], span: tuple[str, str]) -> list[BulkLedger]:
        """`total` ledger rows for one stream, date-ordered, references
        drawn without replacement from a fixed numbering block."""
        fixed = scenario_rows(stream)
        generated = total - len(fixed)
        days = day_range(*span)

        block_lo, block_hi = ID_BLOCKS[stream]
        available = [n for n in range(block_lo, block_hi)
                     if f"{prefix}{n:05d}" not in fixed]
        rng.shuffle(available)
        numbers = available[:generated]

        plan = dict(BULK_ANOMALY_PLAN[stream])
        kinds: list[str] = []
        for kind, count in sorted(plan.items()):
            kinds.extend([kind] * count)
        kinds.extend(["clean"] * (generated - len(kinds)))
        rng.shuffle(kinds)

        rows: list[BulkLedger] = []
        for i, number in enumerate(numbers):
            day = days[(i * len(days)) // generated]
            kind = kinds[i]
            if kind == "delayed":
                # A settlement that arrives four weeks late still has to
                # arrive inside the observation window, so these are only
                # drawn from the first fortnight.
                day = days[i % 14]
            party = parties[(i * 7 + 3) % len(parties)]
            item = items[(i * 5 + 1) % len(items)]
            currency = "USD" if kind == "currency_mismatch" else "INR"
            amount = (_bulk_amount(rng, deny, 60000, 380000) if currency == "USD"
                      else _bulk_amount(rng, deny, low, high))
            rows.append(BulkLedger(
                stream=stream, record_id=f"{prefix}{number:05d}", day=day, party=party, item=item,
                note=NOTE_TEMPLATES[stream].format(item=item),
                amount_minor=amount, currency=currency,
                qty=1 + (i % 4), channel=WEB_CHANNELS[i % len(WEB_CHANNELS)], kind=kind,
            ))
        rows.extend(fixed.values())
        rows.sort(key=lambda r: (r.day, r.record_id))
        return rows

    web = build_stream("WEB", WEB_TOTAL, "WB-1", 25000, 9200000, WEB_ITEMS, (BULK_START, BULK_END))
    erp = build_stream("ERP", ERP_TOTAL, "GLX-2", 80000, 9600000, ERP_NARRATIVES, (BULK_START, BULK_END))
    pos = build_stream("POS", POS_TOTAL, "POS-3", 19900, 4800000, POS_ITEMS, (BULK_START, BULK_END))

    mint = IdMint(BULK_SEED + 1)

    def settle(row: BulkLedger, stream: str, lag: int) -> BulkSettlement:
        """The settlement counterpart of one generated ledger row."""
        gross = row.amount_minor
        settled_day = shift(row.day, lag)
        if row.kind == "amount_mismatch":
            # A keying error, not a fee: an amount nothing in the file
            # explains. Blocked out of the shared amount space like every
            # other value, so it cannot collide with a real record.
            gross = _bulk_amount(rng, deny, max(20000, row.amount_minor - 90000),
                                 row.amount_minor + 90000)
        if row.kind == "delayed":
            settled_day = shift(row.day, 24 + (gross % 5))
        fee, tax = fee_tax(gross)
        refund = 0
        if row.kind == "refund_mismatch":
            refund = round(gross * 0.25 / 100) * 100
        net = gross - fee - tax - refund
        if row.kind == "fee_tax":
            # The payout file's own arithmetic does not hold. Nothing else
            # about the pair is wrong.
            net = net - (7300 + (gross % 4300))
        return BulkSettlement(
            stream=stream,
            txn_id=mint.payment() if stream == "RZP" else (
                f"UPI{mint.rng.randrange(10 ** 11, 10 ** 12)}" if stream == "UPI"
                else f"ACQ{mint.rng.randrange(10 ** 13, 10 ** 14)}"),
            reference=row.record_id, day=row.day, settled_day=settled_day,
            gross_minor=gross, fee_minor=fee, tax_minor=tax, net_minor=net,
            refund_minor=refund, currency="INR", party=row.party,
            note=SETTLEMENT_NOTES[stream].format(ref=row.record_id, item=row.item),
            method=POS_MODES[(gross // 100) % len(POS_MODES)],
        )

    razorpay_payments = [settle(r, "RZP", 0) for r in web if r.kind != "scenario"]
    upi = [settle(r, "UPI", 1 + (r.amount_minor % 2)) for r in erp if r.kind != "scenario"]
    acquirer = [settle(r, "ACQ", 1 + (r.amount_minor % 3)) for r in pos if r.kind != "scenario"]

    # ---- the hand-written settlement rows inside the large sources -----
    by_id = {r.record_id: r for r in web + erp + pos}

    def hand(stream: str, record_id: str, reference: str, day: str, lag: int,
             gross_minor: int | None = None, note: str | None = None) -> BulkSettlement:
        row = by_id[record_id]
        gross = row.amount_minor if gross_minor is None else gross_minor
        fee, tax = fee_tax(gross)
        return BulkSettlement(
            stream=stream,
            txn_id=mint.payment() if stream == "RZP" else (
                f"UPI{mint.rng.randrange(10 ** 11, 10 ** 12)}" if stream == "UPI"
                else f"ACQ{mint.rng.randrange(10 ** 13, 10 ** 14)}"),
            reference=reference, day=day, settled_day=shift(day, lag),
            gross_minor=gross, fee_minor=fee, tax_minor=tax,
            net_minor=gross - fee - tax, refund_minor=0, currency="INR",
            party=row.party, note=note or SETTLEMENT_NOTES[stream].format(
                ref=reference, item=row.item),
            method="UPI",
        )

    # N3 — the gateway wrapped the merchant reference in its own string.
    # Exact matching misses; the shared identifier core does not.
    upi.append(hand("UPI", "WB-105204", "UPI/WB105204/COLL", "2026-03-27", 2,
                    note="Handloom fabric bulk consignment collect"))
    # N5 — one reference, reported twice at the same amount.
    razorpay_payments.append(hand("RZP", "WB-105633", "WB-105633", "2026-03-23", 0))
    razorpay_payments.append(hand("RZP", "WB-105633", "WB-105633", "2026-03-23", 0))
    upi.append(hand("UPI", "GLX-207209", "GLX-207209", "2026-04-01", 2))
    upi.append(hand("UPI", "GLX-207209", "GLX-207209", "2026-04-01", 2))
    # N4 — the settlement is ~Rs 414 short and nothing in the file says why.
    acquirer.append(hand("ACQ", "POS-300412", "POS-300412", "2026-03-23", 2,
                         gross_minor=minor("27046.00")))

    for group in (razorpay_payments, upi, acquirer):
        group.sort(key=lambda s: (s.day, s.reference, s.txn_id))

    return BulkPlan(
        web=tuple(web), erp=tuple(erp), pos=tuple(pos),
        razorpay_payments=tuple(razorpay_payments), upi=tuple(upi), acquirer=tuple(acquirer),
    )


# Reference numbering blocks, one per stream. Wide enough that the drawn
# subset looks like ids rather than a counter.
ID_BLOCKS = {"WEB": (4000, 9999), "ERP": (4000, 9999), "POS": (100, 5999)}

NOTE_TEMPLATES = {
    "WEB": "Web order - {item}",
    "ERP": "{item}",
    "POS": "Counter bill {item}",
}

SETTLEMENT_NOTES = {
    "RZP": "Payment for {ref} {item}",
    "UPI": "UPI collect for {ref} {item}",
    "ACQ": "Settlement for {ref} {item}",
}


# ---------------------------------------------------------------------------
# The two further settlement sources: a third bank account, and the
# gateway's own fee / adjustment register.
# ---------------------------------------------------------------------------

# (txn_day, value_day, description, utr, amount, direction)
# Amounts are written with parenthesised negatives for money out, in one
# signed column — a fourth money convention, and one the reader has to
# get right or every debit reads as an inflow.
AXIS_ROWS = (
    ("2026-03-05", "2026-03-05", "NEFT OUTWARD VENDOR PAYOUT GIRNAR PIPES", "UTR9930041", "118450.00", "DR"),
    ("2026-03-09", "2026-03-09", "IMPS INWARD COUNTER COLLECTION SWEEP", "UTR9930063", "27310.00", "CR"),
    ("2026-03-12", "2026-03-12", "NEFT OUTWARD VENDOR PAYOUT NAAZ FABRICS", "UTR9930088", "74620.00", "DR"),
    ("2026-03-16", "2026-03-16", "GATEWAY MDR DEBIT MARCH CYCLE ONE", "UTR9930102", "8140.00", "DR"),
    # ---- N1 the semantic case: no identifier survives the narration ----
    ("2026-03-21", "2026-03-21", "NEFT INWARD COLD PRESS OIL CASE VAAYU ORGNC", "UTR9930114", "46185.00", "CR"),
    ("2026-03-24", "2026-03-24", "NEFT OUTWARD VENDOR PAYOUT RANJEET TIMBER", "UTR9930167", "53900.00", "DR"),
    # ---- N2 the alias case: trading name on the bank side --------------
    ("2026-03-28", "2026-03-28", "RTGS INWARD TRISOOL ONLINE SERVICES", "UTR9930287", "58940.00", "CR"),
    ("2026-03-29", "2026-03-29", "UPI/COLLECT/MADHUBAN NURSERIES/4417", "UTR9930344", "12760.00", "CR"),
    # ---- N6 two credits, same counterparty, same amount, days apart ----
    ("2026-03-31", "2026-03-31", "NEFT INWARD MEGHDOOT PACKAGING CONSIGNMENT", "UTR9930411", "33750.00", "CR"),
    ("2026-04-01", "2026-04-01", "NEFT INWARD MEGHDOOT PACKAGING LLP DESPATCH", "UTR9930423", "33750.00", "CR"),
    ("2026-04-03", "2026-04-03", "SETTLEMENT ADJUSTMENT REVERSAL GATEWAY", "UTR9930470", "3215.00", "DR"),
    # ---- N9 two counter bills, one consolidated credit -----------------
    ("2026-04-06", "2026-04-06", "NEFT INWARD POS CONSOLIDATED PAYOUT", "UTR9930518", "21255.00", "CR"),
    ("2026-04-08", "2026-04-08", "NEFT OUTWARD VENDOR PAYOUT SOHRAB AUTO", "UTR9930561", "96300.00", "DR"),
    ("2026-04-09", "2026-04-09", "REFUND ADJUSTMENT UPI COLLECT REVERSAL", "UTR9930588", "4470.00", "DR"),
    ("2026-04-10", "2026-04-10", "IMPS INWARD COUNTER COLLECTION SWEEP", "UTR9930604", "31940.00", "CR"),
    ("2026-04-12", "2026-04-12", "BANK CHARGES ESCROW MAINTENANCE APR", "UTR9930651", "826.00", "DR"),
)

# (entry_id_suffix, posted_day, merchant_ref, kind, debit, credit, description)
ADJUSTMENT_ROWS = (
    ("2026/03/06", "ADJ-51004", "MDR", "6420.00", "", "Gateway MDR debit for settlement cycle 2026-03-04"),
    ("2026/03/06", "ADJ-51005", "TAX", "1155.60", "", "GST on gateway MDR for cycle 2026-03-04"),
    ("2026/03/11", "ADJ-51012", "MDR", "9840.00", "", "Gateway MDR debit for settlement cycle 2026-03-09"),
    ("2026/03/11", "ADJ-51013", "TAX", "1771.20", "", "GST on gateway MDR for cycle 2026-03-09"),
    ("2026/03/13", "ADJ-51019", "CHARGEBACK_FEE", "1500.00", "", "Chargeback handling fee raised by acquirer"),
    ("2026/03/16", "ADJ-51026", "MDR", "7310.00", "", "Gateway MDR debit for settlement cycle 2026-03-14"),
    ("2026/03/16", "ADJ-51027", "TAX", "1315.80", "", "GST on gateway MDR for cycle 2026-03-14"),
    ("2026/03/18", "ADJ-51033", "REVERSAL", "", "2480.00", "Reversal of MDR overcharge cycle 2026-03-04"),
    ("2026/03/20", "ADJ-51041", "MDR", "8265.00", "", "Gateway MDR debit for settlement cycle 2026-03-18"),
    ("2026/03/20", "ADJ-51042", "TAX", "1487.70", "", "GST on gateway MDR for cycle 2026-03-18"),
    ("2026/03/23", "ADJ-51048", "PENALTY", "2100.00", "", "Late settlement penalty raised by acquirer"),
    ("2026/03/25", "ADJ-51055", "MDR", "10420.00", "", "Gateway MDR debit for settlement cycle 2026-03-23"),
    ("2026/03/25", "ADJ-51056", "TAX", "1875.60", "", "GST on gateway MDR for cycle 2026-03-23"),
    ("2026/03/27", "ADJ-51062", "REVERSAL", "", "3940.00", "Reversal of duplicate MDR debit cycle 2026-03-14"),
    ("2026/03/30", "ADJ-51070", "MDR", "11260.00", "", "Gateway MDR debit for settlement cycle 2026-03-28"),
    ("2026/03/30", "ADJ-51071", "TAX", "2026.80", "", "GST on gateway MDR for cycle 2026-03-28"),
    ("2026/04/02", "ADJ-51079", "ROLLING_RESERVE", "24500.00", "", "Rolling reserve withheld for April cycle"),
    ("2026/04/04", "ADJ-51086", "MDR", "9075.00", "", "Gateway MDR debit for settlement cycle 2026-04-02"),
    ("2026/04/04", "ADJ-51087", "TAX", "1633.50", "", "GST on gateway MDR for cycle 2026-04-02"),
    ("2026/04/07", "ADJ-51094", "REVERSAL", "", "18300.00", "Rolling reserve released for March cycle"),
    ("2026/04/09", "ADJ-51101", "MDR", "8690.00", "", "Gateway MDR debit for settlement cycle 2026-04-07"),
    ("2026/04/09", "ADJ-51102", "TAX", "1564.20", "", "GST on gateway MDR for cycle 2026-04-07"),
    ("2026/04/11", "ADJ-51109", "CHARGEBACK_FEE", "1500.00", "", "Chargeback handling fee raised by acquirer"),
    ("2026/04/13", "ADJ-51116", "REVERSAL", "", "1500.00", "Chargeback fee reversed after representment"),
)

# Every amount those two files introduce, so the bulk generator can keep
# clear of them exactly as it keeps clear of the original thirteen.
NEW_SETTLEMENT_AMOUNTS = tuple(
    ("axis", utr, minor(amount)) for _t, _v, _d, utr, amount, _dir in AXIS_ROWS
) + tuple(
    ("adjustment", ref, minor(debit or credit))
    for _day, ref, _kind, debit, credit, _desc in ADJUSTMENT_ROWS
)


# The aggregation groups, named so the assertions and the docs cannot
# drift from the data.
AGG_A_MEMBERS = ("BR-4471", "BR-4472", "BR-4473")
AGG_B_MEMBERS = ("INV-3010", "INV-3011", "INV-3012")

# Records from the original thirteen files that can plausibly end a run
# with no matched settlement.
HANDWRITTEN_UNMATCHED = (
    "ORD-7021", "ORD-7031", "ORD-7034", "ORD-7040", "ORD-7104",
    "BR-4471", "BR-4472", "BR-4473", "ZB-6104", "ZB-6107", "SH-88211",
)

# The pool `batch.detect_aggregated_settlements` will actually see, plus
# two records that are matched today but would join it if a match were
# ever lost. Deliberately a superset: the aggregation-uniqueness
# assertion is stronger for including them.
POSSIBLY_UNMATCHED = HANDWRITTEN_UNMATCHED + NEW_UNMATCHED + ("WB-105204", "POS-300412")

AGG_C_BANK_REF = "UTR9930518"

# Pairs whose link no deterministic tier can recover: the two sides share
# no digit run, and their identifier widths are not even comparable, so
# the engine cannot treat the disagreement as a contradiction either.
SEMANTIC_PAIRS = (
    ("ORD-7021", "UTR774120", "HDFC narration"),
    ("ZB-6104", "KWY-88213", "marketplace payout"),
    ("WB-104217", "UTR9930114", "Axis narration"),
    ("WB-104931", "UTR9930287", "merchant alias, trading name vs legal name"),
)

# The opposite case: a reference the gateway wrapped in its own string,
# which exact matching misses and the shared identifier core recovers.
REFORMATTED_SETTLEMENT_REF = "UPI/WB105204/COLL"

PENDING_RECORDS = ("ZB-6107", "WB-106988", "WB-107455", "POS-303551")
MISSING_RECORDS = ("ORD-7034", "WB-106402", "GLX-206115", "POS-302640")

INTENDED_DUPLICATE_REFERENCES = {
    "INV-3050": "S8 the same payment in the gateway file and the bank",
    "INV-3065": "S7 the refund report also carries this reference",
    "INV-3072": "S7b the chargeback report also carries this reference",
    "KWY-88226": "a chargeback against an unbooked marketplace payout",
    "WB-105633": "N5 the gateway reported one payment twice",
    "GLX-207209": "N5 the UPI report carries one collection twice",
}

# For every record that can end a run unmatched: which settlements agree
# with it on amount to the paisa. Anything else appearing here means the
# generated population has manufactured a coincidence.
EXPECTED_AMOUNT_TWINS: dict[str, tuple[str, ...]] = {
    "ORD-7021": ("UTR774120",),
    "ORD-7031": ("INV-3117",),
    "ORD-7034": (),
    "ORD-7040": ("INV-306",),
    "ORD-7104": ("INV-3050", "INV-3050"),
    "BR-4471": (),
    "BR-4472": (),
    "BR-4473": (),
    "ZB-6104": ("KWY-88213",),
    "ZB-6107": (),
    "SH-88211": ("UTR774501",),
    "WB-104217": ("UTR9930114",),
    "WB-104931": ("UTR9930287",),
    "WB-105204": (REFORMATTED_SETTLEMENT_REF,),
    "WB-105633": ("WB-105633", "WB-105633"),
    "WB-106402": (),
    "WB-106988": (),
    "WB-107455": (),
    "GLX-204880": ("UTR9930411", "UTR9930423"),
    "GLX-206115": (),
    "GLX-207209": ("GLX-207209", "GLX-207209"),
    "POS-300412": (),
    "POS-300771": (),
    "POS-300779": (),
    "POS-302640": (),
    "POS-303551": (),
}


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

    def sweep_payout(self) -> str:
        return f"SWP-{self.rng.randint(100000000, 999999999)}"

    def nodal_payout(self) -> str:
        return f"NDL{self.rng.randint(10**11, 10**12 - 1)}"

    def advice_serial(self) -> str:
        return str(self.rng.randint(10**17, 10**18 - 1))

    def nodal_bankref(self) -> str:
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

    Pinning `workbook.properties.modified` is not enough on its own:
    openpyxl's writer overwrites that field with the current time as it
    serialises, so the value in the archive is whatever second the run
    happened in. Two generations of identical data therefore produced
    workbooks that differed by one byte and hashed differently, which is
    exactly the kind of "reproducible" that is only reproducible until
    somebody checks. `docProps/core.xml` is rewritten below.
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

    pinned = fixed.strftime("%Y-%m-%dT%H:%M:%SZ").encode()
    modified = re.compile(rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)")

    deterministic = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(staging.getvalue())) as source:
        with zipfile.ZipFile(deterministic, "w", zipfile.ZIP_DEFLATED) as target:
            for name in source.namelist():
                payload = source.read(name)
                if name == "docProps/core.xml":
                    payload, count = modified.subn(rb"\g<1>" + pinned + rb"\g<2>", payload)
                    if count != 1:
                        raise InvariantError(
                            "XLSX: docProps/core.xml no longer carries exactly one dcterms:modified — "
                            "the reproducibility guarantee cannot be enforced")
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                target.writestr(info, payload)
    path.write_bytes(deterministic.getvalue())


def dmy_slash_date(day: str) -> str:
    y, m, d = day.split("-")
    return f"{d}/{m}/{y}"


def ymd_slash(day: str) -> str:
    return day.replace("-", "/")


def iso_stamp(day: str, hhmm: str, seconds: int = 0) -> str:
    """A UTC ISO-8601 instant, the shape a modern storefront exports."""
    h, m = hhmm.split(":")
    return f"{day}T{int(h):02d}:{int(m):02d}:{seconds:02d}Z"


RZP_METHODS = ("card", "netbanking", "upi", "wallet", "emi")


def vpa_for(party: str, seed_value: int) -> str:
    """A payer VPA built from the counterparty name, never from a real one."""
    handle = "".join(ch for ch in party.lower().split(" ")[0] if ch.isalpha())
    return f"{handle}{seed_value % 97:02d}@{UPI_BANKS[seed_value % len(UPI_BANKS)]}"


def clock(seed_value: int, first_hour: int = 8, span_hours: int = 13) -> tuple[str, int]:
    """A deterministic time of day derived from a record's own amount, so
    timestamps vary across a file without a second RNG."""
    hour = first_hour + (seed_value // 7) % span_hours
    minute = (seed_value // 13) % 60
    second = (seed_value // 3) % 60
    return f"{hour:02d}:{minute:02d}", second


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
        out_dir / "sahyadri_invoices_receivable_mar2026.csv",
        ["Invoice No", "Order Id", "Customer Name", "Invoice Date", "Due Date",
         "Amount", "Currency", "Status", "Description", "Refund Amount"],
        [[inv, oid, customer, iso(day), iso(due), plain(amount), currency, status, description,
          plain(refund) if refund else ""]
         for oid, inv, customer, day, amount, currency, status, description, refund, due in INTERNAL_INVOICES],
    )

    # ---- ledger: Shopify-style export ---------------------------------
    write_csv(
        out_dir / "sahyadri_shopify_orders_mar2026.csv",
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
        out_dir / "sahyadri_tally_sales_register_mar2026.csv",
        ["Date", "Voucher No", "Voucher Type", "Bill Ref No", "Particulars", "Debit", "Credit"],
        [[dmy(day), voucher, kind, ref, particulars, "", plain(credit)]
         for day, voucher, kind, ref, particulars, credit in TALLY_ROWS],
    )

    # ---- ledger: Zoho Books-style invoice export ----------------------
    write_csv(
        out_dir / "sahyadri_zoho_books_invoices_mar2026.csv",
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
        out_dir / "razorpay_settlements_mar_apr2026.csv",
        ["payment_id", "order_id", "settlement_id", "amount", "currency", "fee", "tax",
         "net_amount", "refund_amount", "status", "method", "created_at", "settlement_date",
         "description", "notes"],
        razorpay_out,
    )

    # ---- settlement: the collections-account sweep advice --------------
    # Indian-grouped rupees with the direction inside the cell, and
    # +05:30 instants. The day's collections are swept at a fixed 18:30
    # IST cut-off, so every line credited on the same day carries the
    # same advice number and the same credit instant.
    sweep_out: list[list[str]] = []
    for ref, gross, paid_stamp, settled, mode, counterparty, detail in COLLECTION_SWEEP_ROWS:
        gross_minor = minor(gross)
        fee_minor, tax_minor = fee_tax(gross_minor)
        paid_day, paid_time = paid_stamp.split(" ")
        sweep_out.append([
            f"ADV-{settled.replace('-', '')}", ist(settled), mode, ref, mint.sweep_payout(),
            ist(paid_day, paid_time), swept(gross), swept(fee_minor / 100, "Dr"),
            swept(tax_minor / 100, "Dr"), swept((gross_minor - fee_minor - tax_minor) / 100),
            "INR", "credited", counterparty, detail,
        ])
    write_csv(
        out_dir / "collections_settlement_advice_mar2026.csv",
        ["Advice No", "Sweep Credit Date", "Collection Mode", "Invoice Ref", "Payout Id",
         "Payment Date", "Gross Amount", "Collection Charges", "Tax", "Payout Amount",
         "Currency", "Status", "Counterparty Name", "Details"],
        sweep_out,
    )

    # ---- settlement: the nodal-account payout advice -------------------
    # Second unbranded settlement source. Nothing in the bytes says who
    # produced it: no vendor column, no vendor filename. It classifies on
    # `Payout Id` / `Payout Amount` / `Payout Date` / `Gross Amount` and
    # the fee-and-net breakdown alongside them, which is what a payout
    # file *is* rather than who wrote it.
    nodal_out: list[list[str]] = []
    for ref, gross, day, hhmm, settled, net_override, mode in NODAL_ROWS:
        gross_minor = minor(gross)
        fee_minor, tax_minor = fee_tax(gross_minor)
        net_minor = minor(net_override) if net_override else gross_minor - fee_minor - tax_minor
        nodal_out.append([
            mint.advice_serial(), mint.nodal_payout(), ref, rupee(gross), rupee(fee_minor / 100),
            rupee(tax_minor / 100), rupee(net_minor / 100), "INR", mode, mint.nodal_bankref(),
            dmy_slash(day, hhmm), dmy_slash(settled, "18:00"), "success",
        ])
    write_csv(
        out_dir / "nodal_payout_advice_mar2026.csv",
        ["Advice Serial No", "Payout Id", "Merchant Ref No", "Gross Amount", "Nodal Charges",
         "GST", "Payout Amount", "Currency", "Payment Mode", "Bank Ref Num", "Payment Date",
         "Payout Date", "Status"],
        nodal_out,
    )

    # ---- settlement: marketplace payout -------------------------------
    write_csv(
        out_dir / "kartway_marketplace_payout_mar2026.csv",
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
    write_csv(out_dir / "bank_hdfc_current_5521_mar2026.csv", bank_header, hdfc_march)

    # April opens where March closed, so the two statements read as one
    # account rather than two files that happen to share a name.
    march_closing = float(hdfc_march[-1][-1].replace(",", ""))
    write_xlsx(
        out_dir / "bank_hdfc_current_5521_apr2026.xlsx", "Statement",
        # Two lines of title block above the header, exactly as a bank
        # export renders it. The header lands on row 4.
        ["HDFC BANK LTD - STATEMENT OF ACCOUNT",
         "Account No: 50200078451236   Branch: Koramangala Bengaluru   Period: 01-04-2026 to 15-04-2026"],
        bank_header, bank_rows(HDFC_APRIL, march_closing, resolved),
    )

    write_xlsx(out_dir / "bank_icici_escrow_8347_mar2026.xlsx", "Account Statement", [],
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

    # ---- the six large sources ----------------------------------------
    plan = bulk_plan()

    # ledger: storefront order book. ISO-8601 instants, plain decimals.
    write_csv(
        out_dir / "sahyadri_webstore_orders_mar2026.csv",
        ["Order Reference", "Placed On", "Customer Name", "Status", "Currency", "Item Name",
         "Qty", "Total Amount", "GST", "Sales Channel", "Notes"],
        [[r.record_id, iso_stamp(r.day, clock(r.amount_minor)[0], clock(r.amount_minor)[1]),
          r.party, "PAID", r.currency, r.item, str(r.qty),
          plain(r.amount_minor / 100), plain(round(r.amount_minor * 9 / 118) / 100),
          r.channel, r.note]
         for r in plan.web],
    )

    # ledger: ERP general-ledger extract. Split Debit/Credit, dd-Mon-yyyy.
    write_csv(
        out_dir / "sahyadri_erp_gl_export_fy2026.csv",
        ["Posting Date", "Document No", "Journal", "GL Account Code", "Cost Centre",
         "External Ref", "Customer", "Narrative", "Debit", "Credit", "Currency"],
        [[dmonY(r.day), f"DOC/2026/{r.record_id.split('-')[1]}", "SALES",
          f"41{10 + (r.amount_minor // 100) % 6 * 10}-REV-DOM",
          f"CC-{POS_STORES[(r.amount_minor // 100) % len(POS_STORES)]}",
          r.record_id, r.party, r.note, "", plain(r.amount_minor / 100), r.currency]
         for r in plan.erp],
    )

    # ledger: retail counter register. dd/mm/yyyy, Indian digit grouping.
    write_csv(
        out_dir / "sahyadri_pos_counter_sales_mar2026.csv",
        ["Bill No", "Bill Date", "Store Code", "Terminal Id", "Customer Name", "Item Name",
         "Qty", "Payment Mode", "Bill Amount", "Tax", "Remarks"],
        [[r.record_id, dmy_slash_date(r.day), POS_STORES[(r.amount_minor // 100) % len(POS_STORES)],
          f"TID{4000 + (r.amount_minor // 700) % 900}", r.party, r.item, str(r.qty),
          POS_MODES[(r.amount_minor // 100) % len(POS_MODES)],
          grouped(r.amount_minor / 100), grouped(round(r.amount_minor * 9 / 118) / 100), r.note]
         for r in plan.pos],
    )

    # settlement: a payments export, not a settlement export — same
    # provider, a different point in the money flow. Paise as integers,
    # epoch-second timestamps, no net column at all.
    write_csv(
        out_dir / "razorpay_payments_mar2026.csv",
        ["id", "entity", "order_id", "amount", "currency", "status", "method", "captured",
         "description", "card_id", "bank", "vpa", "fee", "tax", "created_at", "notes"],
        [[s.txn_id, "payment", s.reference, str(s.gross_minor), s.currency, "captured",
          RZP_METHODS[(s.gross_minor // 100) % len(RZP_METHODS)], "TRUE", s.note,
          "", "", "", str(s.fee_minor), str(s.tax_minor),
          epoch(s.day, clock(s.gross_minor)[0]), ""]
         for s in plan.razorpay_payments],
    )

    # settlement: UPI collections. Rupee symbol, Indian grouping,
    # dd/mm/yyyy HH:MM, a stated net and a refund column.
    write_csv(
        out_dir / "upi_collections_settlement_mar2026.csv",
        ["Txn Id", "Merchant Ref No", "Payer VPA", "Payer Name", "Txn Date", "Credit Date",
         "Settlement Utr", "Txn Amount", "MDR", "GST", "Net Amount", "Refund Amount",
         "Status", "Remarks"],
        [[s.txn_id, s.reference, vpa_for(s.party, s.gross_minor), s.party,
          f"{dmy_slash_date(s.day)} {clock(s.gross_minor)[0]}", dmy_slash_date(s.settled_day),
          f"AXISUPI{s.settled_day.replace('-', '')[2:]}{(s.gross_minor % 900) + 100}",
          rupee(s.gross_minor / 100), rupee(s.fee_minor / 100), rupee(s.tax_minor / 100),
          rupee(s.net_minor / 100), rupee(s.refund_minor / 100) if s.refund_minor else "",
          "SUCCESS", s.note]
         for s in plan.upi],
    )

    # settlement: the card acquirer's own POS report, as an XLSX whose
    # real header is on row 5 under a three-line title block. Third
    # unbranded settlement source: an acquirer reference, a commission
    # and a settlement date, and no vendor name in any column.
    write_xlsx(
        out_dir / "card_acquirer_settlement_mar2026.xlsx", "Settlement Report",
        ["CARD ACQUIRING SERVICES - MERCHANT SETTLEMENT REPORT",
         "Merchant: SAHYADRI COFFEE WORKS PVT LTD    MID: 4029130044710",
         "Period: 01-03-2026 to 13-04-2026    Generated: 15-04-2026"],
        ["ACQUIRERREF", "TXNID", "ORDERID", "TXNAMOUNT", "TXNDATE", "PAYMENTMODE", "COMMISSION",
         "GST", "NETAMOUNT", "REFUNDAMT", "SETTLEMENTDATE", "STATUS", "REMARKS"],
        [[f"BTX{7100000000 + (s.gross_minor % 89999999)}", s.txn_id, s.reference,
          plain(s.gross_minor / 100), stamp(s.day, f"{clock(s.gross_minor)[0]}:{clock(s.gross_minor)[1]:02d}"),
          s.method, plain(s.fee_minor / 100), plain(s.tax_minor / 100), plain(s.net_minor / 100),
          plain(s.refund_minor / 100) if s.refund_minor else "",
          dmy(s.settled_day), "TXN_SUCCESS", s.note]
         for s in plan.acquirer],
    )

    # settlement: a third bank account, money in and out in ONE signed
    # column with accounting-style parenthesised negatives.
    axis_out: list[list[str]] = []
    axis_balance = 286400.00
    for txn_day, value_day, description, utr, amount, direction in AXIS_ROWS:
        value = float(amount)
        axis_balance += value if direction == "CR" else -value
        axis_out.append([
            dmonY(txn_day), dmonY(value_day), description, utr,
            grouped(value) if direction == "CR" else parens(value),
            direction, grouped(axis_balance),
        ])
    write_csv(
        out_dir / "bank_axis_current_1104_marapr2026.csv",
        ["Transaction Date", "Value Date", "Description", "UTR", "Amount", "Dr/Cr", "Balance"],
        axis_out,
    )

    # settlement: the gateway's fee and adjustment register — yyyy/mm/dd,
    # split Debit/Credit, and its own reference namespace.
    payment_ids = [s.txn_id for s in plan.razorpay_payments]
    write_csv(
        out_dir / "gateway_fee_adjustments_mar2026.csv",
        ["Entry Id", "Posting Date", "Merchant Ref", "Adjustment Type", "Payment Id",
         "Debit", "Credit", "Currency", "Description"],
        [[f"ENT-{80100 + index}", ymd_slash(day), ref, kind,
          payment_ids[(index * 37) % len(payment_ids)], debit, credit, "INR", description]
         for index, (day, ref, kind, debit, credit, description) in enumerate(ADJUSTMENT_ROWS)],
    )

    # ---- the duplicate upload -----------------------------------------
    # Copied byte-for-byte rather than regenerated, so it is a genuine
    # duplicate no matter what the XLSX writer does.
    shutil.copyfile(out_dir / "bank_icici_escrow_8347_mar2026.xlsx",
                    out_dir / "bank_icici_escrow_8347_mar2026 (1).xlsx")

    return {
        "agg_a_total_minor": agg_a_total,
        "agg_b_total_minor": agg_b_total,
        "agg_c_total_minor": sum(handwritten_ledger_amounts()[r] for r in AGG_C_MEMBERS),
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
    every ledger row across all seven ledger files, as the mapper will
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
    for row in bulk_plan().ledger:
        out.append((row.record_id, row.record_id, row.amount_minor, row.day,
                    f"{row.note} {row.party}"))
    return out


def settlement_population(built: dict) -> list[tuple[str, str, int, str, str]]:
    """(payment_id, reference, gross_minor, day, description) for every
    settlement row across all thirteen settlement files."""
    out: list[tuple[str, str, int, str, str]] = []
    for ref, gross, captured, _s, _r, _c, note in RAZORPAY_ROWS:
        out.append((built["razorpay_payment_ids"][ref], ref, minor(gross), captured, note))
    for ref, gross, paid_stamp, _s, _mode, counterparty, detail in COLLECTION_SWEEP_ROWS:
        out.append((f"sweep::{ref}", ref, minor(gross), paid_stamp.split(" ")[0],
                    f"{detail} {counterparty}"))
    for ref, gross, day, _t, _s, _n, _m in NODAL_ROWS:
        out.append((f"nodal::{ref}", ref, minor(gross), day, ""))
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

    plan = bulk_plan()
    for row in plan.razorpay_payments:
        out.append((row.txn_id, row.reference, row.gross_minor, row.day, row.note))
    for row in plan.upi:
        out.append((row.txn_id, row.reference, row.gross_minor, row.day, f"{row.note} {row.party}"))
    for row in plan.acquirer:
        out.append((row.txn_id, row.reference, row.gross_minor, row.day, row.note))
    for txn_day, _value_day, description, utr, amount, _direction in AXIS_ROWS:
        out.append((utr, utr, minor(amount), txn_day, description))
    for index, (day, ref, _kind, debit, credit, description) in enumerate(ADJUSTMENT_ROWS):
        out.append((f"ENT-{80100 + index}", ref, minor(debit or credit),
                    day.replace("/", "-"), description))
    return out


def subset_sums(pool: dict[str, tuple[int, str]], sizes=(2, 3)) -> dict[int, list[tuple[tuple[str, ...], int, str, str]]]:
    """Every 2- and 3-way sum of the unmatched population, indexed by the
    sum, with the group's earliest and latest day alongside.

    This is what `batch.detect_aggregated_settlements` searches for, done
    once over the pool instead of once per settlement — the engine's loop
    is O(settlements x subsets) and at several thousand settlements that
    is not a check anyone would wait for.
    """
    from itertools import combinations

    index: dict[int, list[tuple[tuple[str, ...], int, str, str]]] = {}
    for size in sizes:
        for group in combinations(sorted(pool), size):
            total = sum(pool[r][0] for r in group)
            days = sorted(pool[r][1] for r in group)
            index.setdefault(total, []).append((group, total, days[0], days[-1]))
    return index


def check_invariants(built: dict) -> list[str]:
    """Every claim the demo script makes about this data, asserted here.

    A dataset bug in this repository once invalidated a whole evaluation
    because a supposedly semantic case leaked its identifier into both
    sides. These checks exist so that cannot happen again silently, and
    they are the reason the population can be grown to several thousand
    records without the interesting cases quietly rotting underneath it.
    """
    from app.engine import normalize

    notes: list[str] = []
    ledger = ledger_population()
    settlements = settlement_population(built)
    ledger_by_id = {row[0]: row[1:] for row in ledger}

    # -- 1. the aggregated bank credits are exactly the sum of their parts
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

    agg_c_parts = [ledger_by_id[r][1] for r in AGG_C_MEMBERS]
    axis_credit = next(minor(a) for _t, _v, _d, utr, a, _dir in AXIS_ROWS if utr == AGG_C_BANK_REF)
    if sum(agg_c_parts) != axis_credit or axis_credit != built["agg_c_total_minor"]:
        raise InvariantError(
            f"AGG-C: the Axis credit {axis_credit} is not the exact sum of {AGG_C_MEMBERS} "
            f"({' + '.join(str(p) for p in agg_c_parts)})")
    notes.append(f"AGG-C  {' + '.join(str(p) for p in agg_c_parts)} = {axis_credit} paise "
                 f"(Rs. {grouped(axis_credit / 100)}) -> {AGG_C_BANK_REF}")

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
    for record_id, settlement_ref, label in SEMANTIC_PAIRS:
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

    # -- 3b. the reformatted reference is recoverable WITHOUT a model ----
    # The mirror image of the check above, and the reason both are worth
    # asserting: one pair must be impossible to link deterministically,
    # the other must be possible, and neither claim survives a data edit
    # unless something checks it.
    reformatted = ledger_by_id["WB-105204"]
    wrapped = next(s for s in settlements if s[1] == REFORMATTED_SETTLEMENT_REF)
    if normalize.normalize_reference(reformatted[0]) == normalize.normalize_reference(wrapped[1]):
        raise InvariantError("REFORMATTED: WB-105204 matches exactly, so nothing is being demonstrated")
    shared = normalize.reference_cores(reformatted[0]) & normalize.reference_cores(wrapped[1])
    if not shared:
        raise InvariantError("REFORMATTED: WB-105204 and its settlement share no reference core, "
                             "so the deterministic fuzzy tier cannot recover it")
    if reformatted[1] != wrapped[2]:
        raise InvariantError("REFORMATTED: WB-105204 and its settlement disagree on amount")
    notes.append(f"REFMT  WB-105204 vs {REFORMATTED_SETTLEMENT_REF} share core {sorted(shared)} at an "
                 "identical amount — recoverable with no model call")

    # -- 4. pending vs missing are decided by AS_OF, not by today -------
    for record_id in PENDING_RECORDS:
        row = ledger_by_id[record_id]
        if datetime.fromisoformat(row[2]).replace(tzinfo=timezone.utc) <= PENDING_CUTOFF:
            raise InvariantError(
                f"PENDING: {record_id} is old enough that a settlement would be due — it would read MISSING")
    for record_id in MISSING_RECORDS:
        row = ledger_by_id[record_id]
        if datetime.fromisoformat(row[2]).replace(tzinfo=timezone.utc) > PENDING_CUTOFF:
            raise InvariantError(
                f"MISSING: {record_id} is inside the settlement window and would read PENDING")
    latest = max(datetime.fromisoformat(day) for _p, _r, _a, day, _t in settlements)
    if latest.replace(tzinfo=timezone.utc) > AS_OF:
        raise InvariantError(f"AS_OF: a settlement is dated {latest.date()}, after the observation point")
    notes.append(f"TIME   as_of {AS_OF.date()} (latest bank value date), pending cutoff "
                 f"{PENDING_CUTOFF.date()}; pending {', '.join(PENDING_RECORDS)}; "
                 f"missing {', '.join(MISSING_RECORDS)}")

    # -- 5. the fee/tax exceptions genuinely do not add up --------------
    broken = next(r for r in NODAL_ROWS if r[5] is not None)
    gross = minor(broken[1])
    fee, tax = fee_tax(gross)
    if minor(broken[5]) == gross - fee - tax:
        raise InvariantError("FEE/TAX: the stated net actually reconciles — the exception would not fire")
    notes.append(f"FEETAX {broken[0]} stated net {minor(broken[5])} vs gross-fee-tax {gross - fee - tax} "
                 f"(difference {minor(broken[5]) - (gross - fee - tax)} paise)")

    broken_bulk = [s for s in bulk_plan().settlements
                   if s.net_minor != s.gross_minor - s.fee_minor - s.tax_minor - s.refund_minor]
    expected_bulk = sum(plan.get("fee_tax", 0) for plan in BULK_ANOMALY_PLAN.values())
    if len(broken_bulk) != expected_bulk:
        raise InvariantError(
            f"FEE/TAX: {len(broken_bulk)} generated payout rows fail their own arithmetic, "
            f"expected {expected_bulk}")
    notes.append(f"FEETAX {expected_bulk} generated payout rows also fail gross-fee-tax-refund, "
                 f"by {min(abs(s.net_minor - (s.gross_minor - s.fee_minor - s.tax_minor - s.refund_minor)) for s in broken_bulk)}"
                 f"-{max(abs(s.net_minor - (s.gross_minor - s.fee_minor - s.tax_minor - s.refund_minor)) for s in broken_bulk)} paise")

    # -- 5b. the amount mismatch is not a fee, and not a missing line ----
    mismatch = ledger_by_id["POS-300412"]
    mismatch_settlement = next(s for s in settlements
                               if s[1] == "POS-300412" and s[2] != mismatch[1])
    delta = mismatch[1] - mismatch_settlement[2]
    single = [rid for rid, _ref, amount, _d, _t in ledger if abs(amount - abs(delta)) <= 2]
    if single:
        raise InvariantError(f"AMOUNT MISMATCH: the {delta} paise difference equals ledger record(s) {single}, "
                             "so a missing line item would explain it")
    notes.append(f"AMTMIS POS-300412 books {mismatch[1]}, the payout says {mismatch_settlement[2]} "
                 f"({delta} paise short) and no record in the workspace accounts for the difference")

    # -- 6. exactly one settlement per reference, except where intended -
    by_reference: dict[str, list[str]] = {}
    for payment_id, reference, _amount, _day, _text in settlements:
        by_reference.setdefault(normalize.normalize_reference(reference), []).append(payment_id)
    intended_duplicates = {
        normalize.normalize_reference(ref): why
        for ref, why in INTENDED_DUPLICATE_REFERENCES.items()
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
    pool = {rid: (ledger_by_id[rid][1], ledger_by_id[rid][2]) for rid in POSSIBLY_UNMATCHED
            if rid in ledger_by_id}
    if len(pool) != len(POSSIBLY_UNMATCHED):
        raise InvariantError("AGGREGATION: POSSIBLY_UNMATCHED names a record that is not in the ledger")
    sums = subset_sums(pool)
    hits: list[tuple[str, tuple[str, ...]]] = []
    for payment_id, _reference, gross, day, _text in settlements:
        when = datetime.fromisoformat(day)
        for offset in range(-2, 3):
            for group, _total, first, last in sums.get(gross + offset, ()):
                if (abs((datetime.fromisoformat(first) - when).days) <= 21
                        and abs((datetime.fromisoformat(last) - when).days) <= 21):
                    hits.append((payment_id, group))
    expected = [("UTR881318", AGG_A_MEMBERS), (AGG_C_BANK_REF, AGG_C_MEMBERS)]
    if sorted(hits) != sorted(expected):
        raise InvariantError(f"AGGREGATION: expected exactly {expected}, found {sorted(hits)}")
    notes.append(f"AGGR   over {len(settlements)} settlements and {len(pool)} possibly-unmatched records, "
                 f"the only 2-3 combinations that sum to a settlement are the two intended ones")

    # -- 8. ledger amount collisions are only the ones the demo names ---
    amounts: dict[int, list[str]] = {}
    for rid, _ref, amount, _day, _text in ledger:
        amounts.setdefault(amount, []).append(rid)
    collisions = {amount: ids for amount, ids in sorted(amounts.items()) if len(ids) > 1}
    if collisions != {minor("8650.00"): ["ORD-7031", "ORD-7032"]}:
        raise InvariantError(f"AMOUNTS: unintended ledger amount collisions {collisions}")
    notes.append(f"AMTS   {len(ledger)} ledger records, and the only two sharing an amount are the trap pair")

    # -- 9. every unresolved record's amount twins are the intended ones
    # This is the check that keeps several thousand generated records
    # from quietly manufacturing a candidate for a hand-written case: the
    # amount index is O(1) and recall-oriented, so one colliding amount
    # anywhere in the population is enough to change an outcome.
    for record_id in sorted(POSSIBLY_UNMATCHED):
        amount = ledger_by_id[record_id][1]
        twins = sorted(ref for _pid, ref, gross, _d, _t in settlements if abs(gross - amount) <= 2)
        expected_twins = sorted(EXPECTED_AMOUNT_TWINS[record_id])
        if twins != expected_twins:
            raise InvariantError(
                f"AMOUNT TWINS: {record_id} (Rs. {grouped(amount / 100)}) is matched on amount by "
                f"{twins}, expected {expected_twins}")
    notes.append(f"TWINS  each of the {len(POSSIBLY_UNMATCHED)} possibly-unmatched records is amount-matched "
                 "only by the settlements it was designed to be matched by")

    return notes


# ---------------------------------------------------------------------------
# ingestion verification — the current detector, on the real bytes
# ---------------------------------------------------------------------------

# What each file is, and the one mapping a human is expected to confirm.
FILE_PLAN: list[tuple[str, str, dict[str, str]]] = [
    ("sahyadri_webstore_orders_mar2026.csv", "ORDERS", {}),
    ("sahyadri_erp_gl_export_fy2026.csv", "ACCOUNTING", {}),
    ("sahyadri_pos_counter_sales_mar2026.csv", "ORDERS", {}),
    ("razorpay_payments_mar2026.csv", "PAYMENT_GATEWAY", {}),
    ("upi_collections_settlement_mar2026.csv", "PAYMENT_GATEWAY", {}),
    ("card_acquirer_settlement_mar2026.xlsx", "PAYMENT_GATEWAY", {}),
    ("bank_axis_current_1104_marapr2026.csv", "BANK_STATEMENT", {}),
    ("gateway_fee_adjustments_mar2026.csv", "PAYMENT_GATEWAY", {}),
    ("sahyadri_invoices_receivable_mar2026.csv", "ORDERS", {}),
    # `Name` is Shopify's order number, but the detector reads a bare
    # "Name" column as a counterparty, which is the more common reading.
    # This is the one confirmation the demo asks for, on purpose.
    ("sahyadri_shopify_orders_mar2026.csv", "ORDERS", {"reference": "Name"}),
    ("sahyadri_tally_sales_register_mar2026.csv", "ACCOUNTING", {}),
    ("sahyadri_zoho_books_invoices_mar2026.csv", "ACCOUNTING", {}),
    ("razorpay_settlements_mar_apr2026.csv", "PAYMENT_GATEWAY", {}),
    ("collections_settlement_advice_mar2026.csv", "PAYMENT_GATEWAY", {}),
    ("nodal_payout_advice_mar2026.csv", "PAYMENT_GATEWAY", {}),
    ("kartway_marketplace_payout_mar2026.csv", "PAYMENT_GATEWAY", {}),
    ("refunds_chargebacks_mar2026.csv", "PAYMENT_GATEWAY", {}),
    ("bank_hdfc_current_5521_mar2026.csv", "BANK_STATEMENT", {}),
    ("bank_hdfc_current_5521_apr2026.xlsx", "BANK_STATEMENT", {}),
    ("bank_icici_escrow_8347_mar2026.xlsx", "BANK_STATEMENT", {}),
]

# record_id -> (scenario tag, what the demo script claims should happen,
#               what an offline run with ACCORD_AI_DISABLED=1 must produce)
#
# The third column is the one `--verify` enforces. The second is the
# claim about a run with a real provider, which this file deliberately
# does not measure and therefore does not assert.
SCENARIO_INDEX: dict[str, tuple[str, str, tuple[str, str | None]]] = {
    "ORD-7021": ("S2a semantic bank narration", "RECONCILED via the model, or HUMAN_REVIEW below threshold",
                 ("EXCEPTION", "MISSING_SETTLEMENT")),
    "ZB-6104": ("S2b semantic marketplace payout", "RECONCILED via the model, or HUMAN_REVIEW below threshold",
                ("HUMAN_REVIEW", "AMBIGUOUS_MATCH")),
    "ORD-7031": ("S3 identical-amount trap", "EXCEPTION — refused, never auto-matched",
                 ("EXCEPTION", "MISSING_SETTLEMENT")),
    "ORD-7032": ("S3 the trap's twin", "RECONCILED on its own exact reference",
                 ("RECONCILED", None)),
    "BR-4471": ("S4a aggregated settlement", "HUMAN_REVIEW / AGGREGATED_SETTLEMENT",
                ("HUMAN_REVIEW", "AGGREGATED_SETTLEMENT")),
    "BR-4472": ("S4a aggregated settlement", "HUMAN_REVIEW / AGGREGATED_SETTLEMENT",
                ("HUMAN_REVIEW", "AGGREGATED_SETTLEMENT")),
    "BR-4473": ("S4a aggregated settlement", "HUMAN_REVIEW / AGGREGATED_SETTLEMENT",
                ("HUMAN_REVIEW", "AGGREGATED_SETTLEMENT")),
    "ZB-6107": ("S5a pending", "EXCEPTION / PENDING_SETTLEMENT",
                ("EXCEPTION", "PENDING_SETTLEMENT")),
    "ORD-7034": ("S5b missing", "EXCEPTION / MISSING_SETTLEMENT",
                 ("EXCEPTION", "MISSING_SETTLEMENT")),
    "BR-4481": ("S6 fee/tax arithmetic", "EXCEPTION / FEE_TAX_INCONSISTENT",
                ("EXCEPTION", "FEE_TAX_INCONSISTENT")),
    "ORD-7036": ("S7 refund offset", "RECONCILED", ("RECONCILED", None)),
    "ORD-7037": ("S7b chargeback not booked", "EXCEPTION / REFUND_MISMATCH",
                 ("EXCEPTION", "REFUND_MISMATCH")),
    "ORD-7104": ("S8 same payment in two sources", "HUMAN_REVIEW / AMBIGUOUS_MATCH",
                 ("HUMAN_REVIEW", "AMBIGUOUS_MATCH")),
    "ORD-7038": ("S9 currency mismatch", "EXCEPTION / CURRENCY_MISMATCH",
                 ("EXCEPTION", "CURRENCY_MISMATCH")),
    "SH-88211": ("S10 ambiguous for the model too", "HUMAN_REVIEW",
                 ("HUMAN_REVIEW", "AMBIGUOUS_MATCH")),
    "ORD-7040": ("S11 truncated reference", "RECONCILED via the model, or HUMAN_REVIEW",
                 ("HUMAN_REVIEW", "LOW_CONFIDENCE_MATCH")),
    # ---- the large sources ------------------------------------------
    "WB-104217": ("S13 semantic, second bank account", "RECONCILED via the model, or HUMAN_REVIEW",
                  ("HUMAN_REVIEW", "AMBIGUOUS_MATCH")),
    "WB-104931": ("S14 merchant alias", "RECONCILED via the model, or HUMAN_REVIEW",
                  ("HUMAN_REVIEW", "AMBIGUOUS_MATCH")),
    "WB-105204": ("S15 gateway-reformatted reference", "RECONCILED / CORROBORATED, no model call",
                  ("RECONCILED", None)),
    "WB-105633": ("S16 one reference reported twice", "HUMAN_REVIEW / AMBIGUOUS_MATCH",
                  ("HUMAN_REVIEW", "AMBIGUOUS_MATCH")),
    "GLX-207209": ("S16 one reference reported twice", "HUMAN_REVIEW / AMBIGUOUS_MATCH",
                   ("HUMAN_REVIEW", "AMBIGUOUS_MATCH")),
    "GLX-204880": ("S17 two equally plausible candidates", "HUMAN_REVIEW — ambiguous for the model too",
                   ("HUMAN_REVIEW", "AMBIGUOUS_MATCH")),
    "POS-300412": ("S18 amount mismatch nothing explains", "EXCEPTION / AMOUNT_MISMATCH",
                   ("EXCEPTION", "AMOUNT_MISMATCH")),
    "POS-300771": ("S19 second aggregation", "HUMAN_REVIEW / AGGREGATED_SETTLEMENT",
                   ("HUMAN_REVIEW", "AGGREGATED_SETTLEMENT")),
    "POS-300779": ("S19 second aggregation", "HUMAN_REVIEW / AGGREGATED_SETTLEMENT",
                   ("HUMAN_REVIEW", "AGGREGATED_SETTLEMENT")),
    "WB-106402": ("S20 missing settlement", "EXCEPTION / MISSING_SETTLEMENT",
                  ("EXCEPTION", "MISSING_SETTLEMENT")),
    "GLX-206115": ("S20 missing settlement", "EXCEPTION / MISSING_SETTLEMENT",
                   ("EXCEPTION", "MISSING_SETTLEMENT")),
    "POS-302640": ("S20 missing settlement", "EXCEPTION / MISSING_SETTLEMENT",
                   ("EXCEPTION", "MISSING_SETTLEMENT")),
    "WB-106988": ("S21 pending, not due yet", "EXCEPTION / PENDING_SETTLEMENT",
                  ("EXCEPTION", "PENDING_SETTLEMENT")),
    "WB-107455": ("S21 pending, not due yet", "EXCEPTION / PENDING_SETTLEMENT",
                  ("EXCEPTION", "PENDING_SETTLEMENT")),
    "POS-303551": ("S21 pending, not due yet", "EXCEPTION / PENDING_SETTLEMENT",
                   ("EXCEPTION", "PENDING_SETTLEMENT")),
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
    import time

    # The offline heuristic verifier, forced: no key is read, no network
    # call is made, and the run is reproducible. It means the genuinely
    # semantic records cannot resolve here — that is the point of them,
    # and it is reported rather than hidden.
    os.environ["ACCORD_AI_DISABLED"] = "1"

    from app.domain.models import PolicyConfig, ReconciliationRecord
    from app.engine.batch import process_batch
    from app.ingest.classify import CONFIDENCE_THRESHOLD, classify_source
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
    print("INGESTION  (reader -> detect_schema -> classify -> mapper, on the real bytes)")
    print("=" * 78)

    mapped_sources = []
    confirmations: list[str] = []
    ingest_started = time.perf_counter()
    for filename, source_type, overrides in FILE_PLAN:
        path = out_dir / filename
        columns, rows, fmt, header_row, notes = read_file(path)
        if not columns:
            failures.append(f"{filename}: could not be read ({'; '.join(notes) or 'no columns'})")
            print(f"\n  {filename}\n    UNREADABLE: {'; '.join(notes)}")
            continue
        detected = detect_schema(columns, rows)
        classified = classify_source(filename, columns, rows, detected)
        mapping = dict(detected.mapping)
        for canonical, column in overrides.items():
            for existing, taken in list(mapping.items()):
                if taken == column:
                    del mapping[existing]
            mapping[canonical] = column

        # A confident classification that disagrees with the plan is a
        # real regression. A low-confidence one that disagrees is the
        # system doing its job: it says so and asks, which is why
        # sahyadri_zoho_books_invoices_mar2026.csv is in this set at all.
        if classified.source_type.value != source_type and not classified.needs_confirmation:
            failures.append(f"{filename}: classified {classified.source_type.value} at "
                            f"{classified.confidence:.2f} without asking, the plan says {source_type}")

        unmapped = [g.column for g in detected.guesses if g.canonical is None]
        print(f"\n  {filename}   [{fmt}"
              + (f", header row {header_row}" if fmt == "xlsx" else "")
              + f", {detected.row_count} rows, amounts={detected.amount_scale}]")
        print(f"    class     {classified.source_type.value} "
              f"({classified.confidence:.2f}) -> {classified.suggested_role}"
              f"  provider={classified.provider or '-'}  stage={classified.stage}")
        print(f"    mapping   " + ", ".join(f"{k}={v}" for k, v in sorted(detected.mapping.items())))
        if detected.debit_column:
            print(f"    paired    debit={detected.debit_column} credit={detected.credit_column}")
        if unmapped:
            print(f"    unmapped  {', '.join(unmapped)}")
        if detected.unmapped_required:
            print(f"    BLOCKED   required field(s) unresolved: {', '.join(detected.unmapped_required)}")
            failures.append(f"{filename}: required field(s) unresolved: {detected.unmapped_required}")
        if classified.needs_confirmation:
            confirmations.append(filename)
            print(f"    CONFIRM   role inferred below {CONFIDENCE_THRESHOLD} — Accord asks before running")
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
    ingest_seconds = time.perf_counter() - ingest_started

    print()
    print("=" * 78)
    print("RECONCILIATION  (deterministic tiers only; ACCORD_AI_DISABLED=1)")
    print("=" * 78)
    print(f"  {len(FILE_PLAN)} sources, {len(ledger)} ledger records, "
          f"{len(settlements)} settlement records, {len(rejected)} rejected rows")
    print(f"  {len(confirmations)} source(s) need confirmation before a run: "
          f"{', '.join(confirmations) or 'none'}")
    print(f"  ingestion (read + detect + classify + map): {ingest_seconds:.2f}s")

    records = [ReconciliationRecord(record_id=r.order_id, merchant=r) for r in ledger]
    run_started = time.perf_counter()
    results = process_batch(records, settlements, policy=PolicyConfig())
    run_seconds = time.perf_counter() - run_started
    print(f"  reconciliation (process_batch, {len(records)} records): {run_seconds:.2f}s")

    derived_as_of = max(s.settlement_date for s in settlements)
    print(f"  derived as_of = {derived_as_of.isoformat()}  (expected {AS_OF.isoformat()})")
    if derived_as_of != AS_OF:
        failures.append(f"derived as_of {derived_as_of} != AS_OF {AS_OF}")

    counts: dict[str, int] = {}
    exceptions: dict[str, int] = {}
    classifications: dict[str, int] = {}
    for result in results:
        counts[result.outcome.value] = counts.get(result.outcome.value, 0) + 1
        if result.exception_type:
            exceptions[result.exception_type.value] = exceptions.get(result.exception_type.value, 0) + 1
        classifications[result.classification.value] = classifications.get(result.classification.value, 0) + 1

    total = len(results)
    print()
    print("  outcome distribution")
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {name:<14} {count:>6}   {count / total:6.2%}")
    print("  exception / review types")
    for name, count in sorted(exceptions.items(), key=lambda kv: -kv[1]):
        print(f"    {name:<24} {count:>6}")
    print("  match classifications")
    for name, count in sorted(classifications.items(), key=lambda kv: -kv[1]):
        print(f"    {name:<28} {count:>6}")

    no_ai = sum(1 for r in results if not r.ai_invoked)
    ai_calls = sum(r.ai_calls for r in results)
    print(f"  {no_ai}/{total} records decided without any model call "
          f"({no_ai / total:.2%}); {ai_calls} verifier calls in total")

    unmatched = sum(1 for r in results
                    if r.matched_payment_id is None and r.outcome.value != "RECONCILED")
    print(f"  {unmatched} records ended unmatched (PolicyConfig.max_aggregation_candidates is "
          f"{PolicyConfig().max_aggregation_candidates}; above it the aggregation pass is skipped)")
    if unmatched > PolicyConfig().max_aggregation_candidates:
        failures.append(f"{unmatched} unmatched records exceeds max_aggregation_candidates — "
                        "the aggregation findings would be silently skipped")

    print()
    print("  scenario records")
    print(f"  {'record':<12} {'outcome':<14} {'exception':<24} {'classification':<28} scenario")
    by_id = {r.record_id: r for r in results}
    for record_id, (tag, _expected, offline) in sorted(SCENARIO_INDEX.items()):
        result = by_id.get(record_id)
        if result is None:
            failures.append(f"scenario record {record_id} is missing from the run")
            print(f"  {record_id:<12} MISSING")
            continue
        actual = (result.outcome.value,
                  result.exception_type.value if result.exception_type else None)
        flag = "" if actual == offline else "   <-- EXPECTED " + str(offline)
        if actual != offline:
            failures.append(f"{record_id}: offline run produced {actual}, expected {offline}")
        print(f"  {record_id:<12} {result.outcome.value:<14} "
              f"{(result.exception_type.value if result.exception_type else '-'):<24} "
              f"{result.classification.value:<28} {tag}{flag}")

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
    print("Verification passed: every file read and classified, every required field mapped, "
          "every invariant held, every scenario landed where it was designed to.")
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
        "bulk_seed": BULK_SEED,
        "as_of": AS_OF.isoformat(),
        "pending_cutoff": PENDING_CUTOFF.isoformat(),
        "ledger_record_count": len(ledger_population()),
        "settlement_record_count": len(settlement_population(built)),
        "aggregation": {
            "AGG_A": {"members": list(AGG_A_MEMBERS), "bank_ref": "UTR881318",
                      "total_minor": built["agg_a_total_minor"]},
            "AGG_B": {"members": list(AGG_B_MEMBERS), "bank_ref": "UTR774008",
                      "total_minor": built["agg_b_total_minor"]},
            "AGG_C": {"members": list(AGG_C_MEMBERS), "bank_ref": AGG_C_BANK_REF,
                      "total_minor": built["agg_c_total_minor"]},
        },
        "possibly_unmatched": list(POSSIBLY_UNMATCHED),
        "scenarios": {k: {"tag": v[0], "expected": v[1],
                          "expected_offline": {"outcome": v[2][0], "exception_type": v[2][1]}}
                      for k, v in sorted(SCENARIO_INDEX.items())},
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
    data_files = [p for p in args.out_dir.iterdir() if p.name != "_manifest.json"]
    print(f"Demo workspace written to {args.out_dir}")
    print(f"  {len(FILE_PLAN)} distinct sources ({len(data_files)} files, "
          f"one of them a byte-identical duplicate upload)")
    print(f"  {len(ledger)} ledger rows, {len(settlements)} settlement rows, "
          f"{len(ledger) + len(settlements)} records in total")
    print(f"  observation point pinned at {AS_OF.date()}")

    if args.verify:
        print()
        return verify(args.out_dir, built)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
