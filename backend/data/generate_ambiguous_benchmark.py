"""
Development-only benchmark for the ambiguous-matching problem.

This is NOT an evaluation set. It exists so that changes to candidate
ranking, the semantic prompt, and the response schema can be developed
and compared against something other than the held-out data. Nothing
here is ever scored in a headline metric.

The unit of this benchmark is deliberately narrower than the main
dataset's: one merchant record, a small pool of settlement candidates,
and the payment_id of the one that is genuinely the same payment (or
null if none of them is). That isolates the matching decision from the
downstream financial checks, so a matching regression can't hide behind
an unrelated arithmetic pass.

Design constraint that makes this benchmark worth anything: **amount
alone must not solve it.** Roughly half the true matches sit next to a
distractor with an identical amount, and roughly half the non-matches
are single candidates whose amount matches exactly. A ranker that just
looks for an equal amount scores near chance here, which is the point --
the residual after deterministic narrowing is what the semantic layer is
supposed to earn its place on.

Usage:
    python backend/data/generate_ambiguous_benchmark.py --seed 771 --count 240
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT_PATH = Path(__file__).parent / "datasets" / "ambiguous_benchmark.jsonl"

BASE_DATE = datetime(2026, 3, 1, tzinfo=timezone.utc)
FEE_RATE = 0.02
GST_RATE = 0.18

# (canonical name, common abbreviation, alias a settlement system might use)
PRODUCTS = [
    ("Consulting Session", "Consult Sesn", "Advisory Hour"),
    ("Annual Membership", "Ann Membership", "Yearly Plan"),
    ("Premium Plan", "Prem Plan", "Premium Tier"),
    ("Data Export Service", "Data Exp Svc", "Bulk Export"),
    ("Team Seat Upgrade", "Team Seat Upg", "Additional Seat"),
    ("Priority Support Add-on", "Prio Support", "Support Escalation"),
    ("Onboarding Fee", "Onbrdng Fee", "Implementation Fee"),
    ("Widget Bundle", "Wdgt Bundle", "Component Pack"),
    ("Express Shipping Add-on", "Exp Shipping", "Courier Upgrade"),
    ("Starter Kit", "Startr Kit", "Beginner Pack"),
]

# (legal entity as the merchant books it, trading name a gateway shows)
MERCHANT_NAMES = [
    ("Nirvana Retail Private Limited", "Nirvana Store"),
    ("Kalyan Technologies Pvt Ltd", "KalyanTech"),
    ("Sunrise Commerce LLP", "Sunrise Shop"),
    ("Meridian Softworks Pvt Ltd", "Meridian Apps"),
    ("Anand Traders Private Limited", "Anand Traders"),
    ("Bluepeak Services Pvt Ltd", "Bluepeak"),
]

CUSTOMERS = [
    ("Rahul Sharma", "R. Sharma", "SHARMA RAHUL"),
    ("Priya Nair", "P. Nair", "NAIR PRIYA"),
    ("Arjun Mehta", "A. Mehta", "MEHTA ARJUN"),
    ("Divya Iyer", "D. Iyer", "IYER DIVYA"),
    ("Faisal Khan", "F. Khan", "KHAN FAISAL"),
    ("Sneha Reddy", "S. Reddy", "REDDY SNEHA"),
]

GATEWAY_NOISE = [
    "UPI/COLLECT/{n}", "NEFT REF {n}", "TXN{n}/POS", "IMPS-{n}",
    "RRN {n} AUTH OK", "CARD**{n} SETTLED",
]


def fee_tax(gross: int) -> tuple[int, int]:
    fee = round(gross * FEE_RATE)
    return fee, round(fee * GST_RATE)


@dataclass
class Merchant:
    order_id: str
    reference_id: str
    amount_minor: int
    currency: str
    order_date: str
    status: str
    refund_amount_minor: int
    description: str


@dataclass
class Settlement:
    payment_id: str
    order_reference: str
    settlement_id: str
    gross_amount_minor: int
    fee_minor: int
    tax_minor: int
    net_amount_minor: int
    refund_amount_minor: int
    order_date: str
    settlement_date: str
    currency: str
    status: str
    description: str


class BenchmarkGenerator:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.seq = 90000
        self.examples: list[dict] = []

    def _next(self) -> int:
        self.seq += self.rng.randint(1, 7)
        return self.seq

    def _amount(self) -> int:
        return self.rng.randint(120, 24000) * 100

    def _date(self) -> datetime:
        return BASE_DATE + timedelta(days=self.rng.randint(0, 120), hours=self.rng.randint(0, 23))

    def _settlement(self, oid: int, amount: int, order_date: datetime, description: str,
                    reference: str, delay_days: int = 2) -> Settlement:
        fee, tax = fee_tax(amount)
        return Settlement(
            payment_id=f"pay_{oid:010d}", order_reference=reference, settlement_id=f"setl_{oid:010d}",
            gross_amount_minor=amount, fee_minor=fee, tax_minor=tax,
            net_amount_minor=amount - fee - tax, refund_amount_minor=0,
            order_date=order_date.isoformat(),
            settlement_date=(order_date + timedelta(days=delay_days)).isoformat(),
            currency="INR", status="settled", description=description,
        )

    def _merchant(self, oid: int, amount: int, order_date: datetime, description: str, reference: str) -> Merchant:
        return Merchant(
            order_id=f"ORD{oid}", reference_id=reference, amount_minor=amount, currency="INR",
            order_date=order_date.isoformat(), status="captured", refund_amount_minor=0,
            description=description,
        )

    def _emit(self, variation: str, merchant: Merchant, candidates: list[Settlement],
              true_payment_id: str | None, expected_resolution: str, note: str) -> None:
        shuffled = candidates[:]
        self.rng.shuffle(shuffled)
        self.examples.append({
            "example_id": f"AMB{len(self.examples):04d}",
            "variation": variation,
            "is_true_match": true_payment_id is not None,
            "expected_resolution": expected_resolution,
            "note": note,
            "merchant": asdict(merchant),
            "candidates": [asdict(c) for c in shuffled],
            "true_payment_id": true_payment_id,
        })

    # ---------- true-match variations ----------

    def abbreviation(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        full, abbr, _ = self.rng.choice(PRODUCTS)
        cust = self.rng.choice(CUSTOMERS)
        m = self._merchant(oid, amount, date, f"Order {oid} {full} for {cust[0]}", f"ORD-{oid}")
        true = self._settlement(oid, amount, date, f"{abbr} {cust[1]} stlmt", f"RZP{oid}")
        self._emit("abbreviation", m, [true] + self._distractors(date, amount, count=3),
                   true.payment_id, "semantic",
                   "Product name abbreviated and customer name initialised on the settlement side.")

    def word_order(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        full, _, _ = self.rng.choice(PRODUCTS)
        cust = self.rng.choice(CUSTOMERS)
        m = self._merchant(oid, amount, date, f"{full} - {cust[0]} - order {oid}", f"ORD/{oid}")
        true = self._settlement(oid, amount, date, f"{cust[2]} {full.lower()} settlement ref {oid}", f"RZP-{oid}")
        self._emit("word_order", m, [true] + self._distractors(date, amount, count=3),
                   true.payment_id, "deterministic",
                   "Same tokens, different order, plus a shared numeric reference core.")

    def merchant_alias(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        legal, trading = self.rng.choice(MERCHANT_NAMES)
        full, _, _ = self.rng.choice(PRODUCTS)
        m = self._merchant(oid, amount, date, f"{legal} - {full} - invoice {oid}", f"INV-2026-{oid}")
        true = self._settlement(oid, amount, date, f"{trading} {full} payout", f"RZP/{oid}")
        self._emit("merchant_alias", m, [true] + self._distractors(date, amount, count=3),
                   true.payment_id, "semantic",
                   "Merchant booked under its legal entity, settled under its trading name.")

    def product_alias(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        full, _, alias = self.rng.choice(PRODUCTS)
        m = self._merchant(oid, amount, date, f"Order {oid} - {full}", f"ORD_{oid}")
        # No shared numeric core: reference formats diverge completely.
        true = self._settlement(oid, amount, date, f"{alias} settlement", f"SETL{self._next()}")
        self._emit("product_alias", m, [true] + self._distractors(date, amount, count=3),
                   true.payment_id, "semantic",
                   "Product recorded under a synonym; references share nothing. Amount and date carry the match.")

    def invoice_format(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        full, _, _ = self.rng.choice(PRODUCTS)
        year = 2026
        m = self._merchant(oid, amount, date, f"Invoice {year}/{oid} {full}", f"INV-{year}-{oid}")
        true = self._settlement(oid, amount, date, f"invoice {year}{oid} {full.lower()}", f"INV{year}{oid}")
        self._emit("invoice_format", m, [true] + self._distractors(date, amount, count=3),
                   true.payment_id, "deterministic",
                   "Identical invoice number, different separator convention on each side.")

    def punctuation(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        full, _, _ = self.rng.choice(PRODUCTS)
        m = self._merchant(oid, amount, date, f"{full}: order #{oid}", f"ORD.{oid}.A")
        true = self._settlement(oid, amount, date, f"{full} -- order {oid}", f"ORD-{oid}-A")
        self._emit("punctuation", m, [true] + self._distractors(date, amount, count=3),
                   true.payment_id, "deterministic",
                   "Punctuation-only divergence in both reference and description.")

    def noisy_description(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        full, _, _ = self.rng.choice(PRODUCTS)
        noise = self.rng.choice(GATEWAY_NOISE).format(n=self.rng.randint(100000, 999999))
        m = self._merchant(oid, amount, date, f"Order {oid} {full}", f"ORD-{oid}")
        true = self._settlement(oid, amount, date, f"{noise} {full.lower()}", f"RZP{oid}X")
        self._emit("noisy_description", m, [true] + self._distractors(date, amount, count=3),
                   true.payment_id, "semantic",
                   "Settlement description dominated by gateway routing noise.")

    def customer_name_variation(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        cust = self.rng.choice(CUSTOMERS)
        m = self._merchant(oid, amount, date, f"Payment from {cust[0]} order {oid}", f"ORD-{oid}")
        true = self._settlement(oid, amount, date, f"collection {cust[2]}", f"RZP/{oid}/S")
        self._emit("customer_name_variation", m, [true] + self._distractors(date, amount, count=3),
                   true.payment_id, "semantic",
                   "Customer name reordered and upper-cased; product not mentioned on the settlement side.")

    def amount_ambiguous_true_match(self) -> None:
        """True match whose amount is duplicated by a distractor, so the
        amount signal is deliberately useless here."""
        oid, amount, date = self._next(), self._amount(), self._date()
        full, _, alias = self.rng.choice(PRODUCTS)
        cust = self.rng.choice(CUSTOMERS)
        m = self._merchant(oid, amount, date, f"Order {oid} {full} {cust[0]}", f"ORD-{oid}")
        true = self._settlement(oid, amount, date, f"{alias} {cust[1]}", f"RZP{oid}")
        twin_oid = self._next()
        other_cust = self.rng.choice([c for c in CUSTOMERS if c != cust])
        twin = self._settlement(twin_oid, amount, date + timedelta(hours=5),
                                f"{alias} {other_cust[1]}", f"RZP{twin_oid}")
        self._emit("amount_collision_true_match", m, [true, twin] + self._distractors(date, amount, count=2),
                   true.payment_id, "semantic",
                   "A different payment shares the exact amount and product; only the customer disambiguates.")

    # ---------- non-match variations ----------

    def near_duplicate_different(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        full, _, _ = self.rng.choice(PRODUCTS)
        cust = self.rng.choice(CUSTOMERS)
        m = self._merchant(oid, amount, date, f"Order {oid} {full} {cust[0]}", f"ORD-{oid}")
        other = self._next()
        decoy = self._settlement(other, amount + self.rng.choice([-500, 500, 1200]), date + timedelta(days=1),
                                 f"Order {other} {full} {cust[0]}", f"RZP{other}")
        self._emit("near_duplicate_different", m, [decoy] + self._distractors(date, amount, count=2),
                   None, "reject",
                   "Same customer and product one day later, different order number and amount.")

    def reference_core_collision(self) -> None:
        """A genuinely different payment whose reference happens to carry
        the same digits -- the trap for any core-digit matcher."""
        oid, amount, date = self._next(), self._amount(), self._date()
        full, _, _ = self.rng.choice(PRODUCTS)
        other_full = self.rng.choice([p for p in PRODUCTS if p[0] != full])[0]
        m = self._merchant(oid, amount, date, f"Order {oid} {full}", f"ORD-{oid}")
        # Same digits, different meaning: their invoice counter, not our order id.
        decoy = self._settlement(self._next(), amount, date + timedelta(days=2),
                                 f"{other_full} batch invoice {oid}", f"INV-{oid}")
        self._emit("reference_core_collision", m, [decoy] + self._distractors(date, amount, count=2),
                   None, "reject",
                   "Shared digit sequence across differently-scoped identifiers; different product entirely.")

    def same_amount_different_txn(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        full, _, _ = self.rng.choice(PRODUCTS)
        other_full = self.rng.choice([p for p in PRODUCTS if p[0] != full])[0]
        cust, other_cust = self.rng.sample(CUSTOMERS, 2)
        m = self._merchant(oid, amount, date, f"Order {oid} {full} {cust[0]}", f"ORD-{oid}")
        other = self._next()
        decoy = self._settlement(other, amount, date + timedelta(days=1),
                                 f"{other_full} {other_cust[1]}", f"RZP{other}")
        self._emit("same_amount_different_txn", m, [decoy], None, "reject",
                   "Identical amount, different customer and product. Amount alone cannot reject this.")

    def sequential_orders(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        full, _, _ = self.rng.choice(PRODUCTS)
        cust = self.rng.choice(CUSTOMERS)
        m = self._merchant(oid, amount, date, f"Order {oid} {full} {cust[0]}", f"ORD-{oid}")
        nxt = oid + 1
        decoy = self._settlement(nxt, amount, date + timedelta(minutes=40),
                                 f"Order {nxt} {full} {cust[0]}", f"RZP{nxt}")
        self._emit("sequential_orders", m, [decoy], None, "reject",
                   "Adjacent order numbers, same customer, same product, minutes apart, same amount. "
                   "The hardest legitimate rejection in the set.")

    def missing_counterpart(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        full, _, _ = self.rng.choice(PRODUCTS)
        m = self._merchant(oid, amount, date, f"Order {oid} {full}", f"ORD-{oid}")
        self._emit("missing_counterpart", m, self._distractors(date, amount, count=3),
                   None, "reject",
                   "No counterpart exists; every candidate is an unrelated nearby settlement.")

    # ---------- shared ----------

    def _distractors(self, near_date: datetime, near_amount: int, count: int) -> list[Settlement]:
        out = []
        for _ in range(count):
            oid = self._next()
            full, _, _ = self.rng.choice(PRODUCTS)
            cust = self.rng.choice(CUSTOMERS)
            amount = max(10000, int(near_amount * self.rng.uniform(0.4, 2.2)))
            out.append(self._settlement(
                oid, amount, near_date + timedelta(days=self.rng.randint(-4, 4)),
                f"Order {oid} {full} {cust[1]}", f"RZP{oid}",
            ))
        return out

    def generate(self, count: int) -> None:
        true_match_makers = [
            self.abbreviation, self.word_order, self.merchant_alias, self.product_alias,
            self.invoice_format, self.punctuation, self.noisy_description,
            self.customer_name_variation, self.amount_ambiguous_true_match,
        ]
        non_match_makers = [
            self.near_duplicate_different, self.reference_core_collision,
            self.same_amount_different_txn, self.sequential_orders, self.missing_counterpart,
        ]
        half = count // 2
        for i in range(half):
            true_match_makers[i % len(true_match_makers)]()
        for i in range(count - half):
            non_match_makers[i % len(non_match_makers)]()
        self.rng.shuffle(self.examples)
        for i, ex in enumerate(self.examples):
            ex["example_id"] = f"AMB{i:04d}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=771)
    parser.add_argument("--count", type=int, default=240)
    args = parser.parse_args()

    gen = BenchmarkGenerator(args.seed)
    gen.generate(args.count)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w") as f:
        for ex in gen.examples:
            f.write(json.dumps(ex) + "\n")

    true_matches = sum(1 for e in gen.examples if e["is_true_match"])
    by_variation: dict[str, int] = {}
    for e in gen.examples:
        by_variation[e["variation"]] = by_variation.get(e["variation"], 0) + 1

    print(f"Wrote {len(gen.examples)} examples to {OUT_PATH}")
    print(f"  true matches : {true_matches}")
    print(f"  non-matches  : {len(gen.examples) - true_matches}")
    print("  by variation :")
    for name, n in sorted(by_variation.items()):
        print(f"    {name:<32} {n:>4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
