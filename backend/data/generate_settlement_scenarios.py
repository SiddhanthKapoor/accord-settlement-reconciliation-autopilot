"""
Development-only scenarios for settlement-presence discrimination.

The question this isolates is narrower than "did we match correctly":
**when no correct match exists, can the system tell that apart from a
coincidence?** Evaluation V2 regressed almost entirely on that question,
and the held-out data that revealed it must not be used to design the
fix. This file is the development stand-in for that failure class.

Every example is one merchant record plus a settlement population, with
ground truth stating whether a genuine counterpart exists at all:

    present        a real counterpart is in the population
    absent         no counterpart exists; anything retrieved is coincidence
    pending        no counterpart exists YET — too early in the settlement cycle
    ambiguous      more than one genuinely plausible counterpart

The interesting cases are the ones where `absent` still retrieves
something. In a population of a few thousand settlements, an exact amount
collision is ordinary, not remarkable — which is the whole point: amount
agreement is a reason to *look*, not evidence of identity.

Usage:
    python backend/data/generate_settlement_scenarios.py --seed 4127 --count 300
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT_PATH = Path(__file__).parent / "datasets" / "settlement_scenarios.jsonl"
POOL_PATH = Path(__file__).parent / "datasets" / "settlement_scenarios_pool.jsonl"

BASE_DATE = datetime(2026, 4, 1, tzinfo=timezone.utc)
FEE_RATE = 0.02
GST_RATE = 0.18

# The observation point. A settlement that would not have run yet by this
# date is pending, not missing — the distinction the engine currently
# cannot make.
AS_OF = BASE_DATE + timedelta(days=120)

PRODUCTS = [
    "Premium Plan", "Pro Subscription", "Starter Kit", "Annual Membership",
    "Consulting Session", "Widget Bundle", "Express Shipping Add-on",
    "Gift Card", "Onboarding Fee", "Data Export Service", "Team Seat Upgrade",
    "Priority Support Add-on",
]

MERCHANT_NAMES = [
    "Nirvana Retail", "Kalyan Technologies", "Sunrise Commerce",
    "Meridian Softworks", "Anand Traders", "Bluepeak Services",
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


class ScenarioGenerator:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.seq = 700000
        self.examples: list[dict] = []
        # One shared settlement population across every scenario, as the
        # real pipeline has. Per-scenario pools would make the IDF text
        # statistics meaningless -- boilerplate is only recognisable as
        # boilerplate against a corpus -- and would hide the cross-record
        # collisions that make this problem hard in the first place.
        self.pool: list[Settlement] = []

    def _next(self) -> int:
        self.seq += self.rng.randint(1, 9)
        return self.seq

    def _amount(self) -> int:
        return self.rng.randint(150, 22000) * 100

    def _date(self, days_before_as_of: int | None = None) -> datetime:
        if days_before_as_of is None:
            days_before_as_of = self.rng.randint(20, 110)
        return AS_OF - timedelta(days=days_before_as_of, hours=self.rng.randint(0, 23))

    def _settlement(self, oid: int, amount: int, order_date: datetime, description: str,
                    reference: str, delay_days: int = 2, refund: int = 0) -> Settlement:
        fee, tax = fee_tax(amount)
        return Settlement(
            payment_id=f"pay_{oid:010d}", order_reference=reference, settlement_id=f"setl_{oid:010d}",
            gross_amount_minor=amount, fee_minor=fee, tax_minor=tax,
            net_amount_minor=amount - fee - tax - refund, refund_amount_minor=refund,
            order_date=order_date.isoformat(),
            settlement_date=(order_date + timedelta(days=delay_days)).isoformat(),
            currency="INR", status="partially_refunded" if refund else "settled",
            description=description,
        )

    def _merchant(self, oid: int, amount: int, order_date: datetime, description: str,
                  reference: str, status: str = "captured", refund: int = 0) -> Merchant:
        return Merchant(
            order_id=f"ORD{oid}", reference_id=reference, amount_minor=amount, currency="INR",
            order_date=order_date.isoformat(), status=status, refund_amount_minor=refund,
            description=description,
        )

    def _noise(self, near_date: datetime, count: int, near_amount: int | None = None) -> list[Settlement]:
        """Unrelated settlements that happen to be nearby in time."""
        out = []
        for _ in range(count):
            oid = self._next()
            product = self.rng.choice(PRODUCTS)
            merchant = self.rng.choice(MERCHANT_NAMES)
            amount = near_amount if near_amount is not None else self._amount()
            out.append(self._settlement(
                oid, amount, near_date + timedelta(days=self.rng.randint(-6, 6)),
                f"Settlement for ORD{oid} {merchant} {product}", f"RZP/{oid}/SETL",
            ))
        return out

    def _emit(self, scenario: str, truth: str, merchant: Merchant, population: list[Settlement],
              true_payment_id: str | None, note: str) -> None:
        self.pool.extend(population)
        self.examples.append({
            "example_id": f"SET{len(self.examples):04d}",
            "scenario": scenario,
            "settlement_truth": truth,          # present | absent | pending | ambiguous
            "true_payment_id": true_payment_id,
            "note": note,
            "as_of": AS_OF.isoformat(),
            "merchant": asdict(merchant),
        })

    # ---------- absent: retrieval finds only coincidence ----------

    def amount_collision_unrelated_reference(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        m = self._merchant(oid, amount, date, f"Order {oid} {self.rng.choice(PRODUCTS)}", f"ORD-{oid}")
        decoy_id = self._next()
        decoy = self._settlement(decoy_id, amount, date + timedelta(days=1),
                                 f"Settlement for ORD{decoy_id} {self.rng.choice(PRODUCTS)}",
                                 f"RZP/{decoy_id}/SETL")
        self._emit("amount_collision_unrelated_reference", "absent", m,
                   [decoy] + self._noise(date, 4), None,
                   "Exact amount match, entirely unrelated reference and product. The archetypal "
                   "coincidence: amount agreement is the only signal.")

    def amount_collision_unrelated_merchant(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        mine, theirs = self.rng.sample(MERCHANT_NAMES, 2)
        m = self._merchant(oid, amount, date, f"{mine} order {oid} {self.rng.choice(PRODUCTS)}", f"ORD-{oid}")
        decoy_id = self._next()
        decoy = self._settlement(decoy_id, amount, date,
                                 f"{theirs} payout {decoy_id}", f"RZP/{decoy_id}/SETL")
        self._emit("amount_collision_unrelated_merchant", "absent", m,
                   [decoy] + self._noise(date, 4), None,
                   "Same amount, same day, different counterparty.")

    def amount_collision_outside_window(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date(days_before_as_of=100)
        m = self._merchant(oid, amount, date, f"Order {oid} {self.rng.choice(PRODUCTS)}", f"ORD-{oid}")
        decoy_id = self._next()
        far = date - timedelta(days=75)
        decoy = self._settlement(decoy_id, amount, far,
                                 f"Settlement for ORD{decoy_id} {self.rng.choice(PRODUCTS)}",
                                 f"RZP/{decoy_id}/SETL")
        self._emit("amount_collision_outside_window", "absent", m,
                   [decoy] + self._noise(date, 3), None,
                   "Same amount but months away — temporally implausible as the same payment.")

    def amount_collision_boilerplate_text(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        product = self.rng.choice(PRODUCTS)
        m = self._merchant(oid, amount, date, f"Payment for order {oid} {product} customer checkout", f"ORD-{oid}")
        decoy_id = self._next()
        decoy = self._settlement(decoy_id, amount, date + timedelta(days=1),
                                 f"Payment for order {decoy_id} {product} customer checkout settlement",
                                 f"RZP/{decoy_id}/SETL")
        self._emit("amount_collision_boilerplate_text", "absent", m,
                   [decoy] + self._noise(date, 4), None,
                   "Same amount and near-identical template wording, different order number. "
                   "Text similarity here is boilerplate, not evidence.")

    def genuinely_missing_no_candidates(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        m = self._merchant(oid, amount, date, f"Order {oid} {self.rng.choice(PRODUCTS)}", f"ORD-{oid}")
        self._emit("genuinely_missing_no_candidates", "absent", m,
                   self._noise(date, 5), None,
                   "Nothing in the population resembles it on any signal.")

    # ---------- pending: absent, but not yet due ----------

    def pending_within_settlement_cycle(self) -> None:
        oid, amount = self._next(), self._amount()
        date = AS_OF - timedelta(days=self.rng.randint(0, 1), hours=self.rng.randint(1, 20))
        m = self._merchant(oid, amount, date, f"Order {oid} {self.rng.choice(PRODUCTS)}", f"ORD-{oid}")
        self._emit("pending_within_settlement_cycle", "pending", m,
                   self._noise(date - timedelta(days=5), 4), None,
                   "Captured hours ago. A T+2 settlement cannot exist yet; calling this a missing "
                   "settlement would be wrong, not merely conservative.")

    def pending_with_amount_collision(self) -> None:
        oid, amount = self._next(), self._amount()
        date = AS_OF - timedelta(days=1, hours=6)
        m = self._merchant(oid, amount, date, f"Order {oid} {self.rng.choice(PRODUCTS)}", f"ORD-{oid}")
        decoy_id = self._next()
        decoy = self._settlement(decoy_id, amount, date - timedelta(days=30),
                                 f"Settlement for ORD{decoy_id}", f"RZP/{decoy_id}/SETL")
        self._emit("pending_with_amount_collision", "pending", m,
                   [decoy] + self._noise(date, 3), None,
                   "Too recent to have settled, and an old unrelated record shares the amount.")

    # ---------- present: a real counterpart exists ----------

    def present_exact_reference(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        product = self.rng.choice(PRODUCTS)
        m = self._merchant(oid, amount, date, f"Order {oid} {product}", f"ORD-{oid}")
        true = self._settlement(oid, amount, date, f"Settlement for ORD{oid} {product}", f"ORD-{oid}")
        self._emit("present_exact_reference", "present", m,
                   [true] + self._noise(date, 4), true.payment_id,
                   "Straightforward: reference matches after normalization.")

    def present_reformatted_reference(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        product = self.rng.choice(PRODUCTS)
        m = self._merchant(oid, amount, date, f"Order {oid} {product}", f"ORD.{oid}.CHK")
        true = self._settlement(oid, amount, date, f"Settlement note order {oid} {product.lower()}",
                                f"RZP/{oid}/SETL")
        self._emit("present_reformatted_reference", "present", m,
                   [true] + self._noise(date, 4), true.payment_id,
                   "Reference reformatted but the identifier core survives, and the amount agrees.")

    def present_delayed_settlement(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date(days_before_as_of=40)
        product = self.rng.choice(PRODUCTS)
        m = self._merchant(oid, amount, date, f"Order {oid} {product}", f"ORD-{oid}")
        true = self._settlement(oid, amount, date, f"Settlement for ORD{oid} {product}",
                                f"ORD-{oid}", delay_days=self.rng.randint(9, 18))
        self._emit("present_delayed_settlement", "present", m,
                   [true] + self._noise(date, 4), true.payment_id,
                   "Late but real. Delay is a property of the matched settlement, not a reason "
                   "to doubt it exists.")

    def present_with_amount_collision_decoy(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        product = self.rng.choice(PRODUCTS)
        m = self._merchant(oid, amount, date, f"Order {oid} {product}", f"ORD-{oid}")
        true = self._settlement(oid, amount, date, f"Settlement for ORD{oid} {product}", f"ORD-{oid}")
        decoy_id = self._next()
        decoy = self._settlement(decoy_id, amount, date + timedelta(days=2),
                                 f"Settlement for ORD{decoy_id}", f"RZP/{decoy_id}/SETL")
        self._emit("present_with_amount_collision_decoy", "present", m,
                   [true, decoy] + self._noise(date, 3), true.payment_id,
                   "The right answer and a same-amount impostor both present. Reference evidence "
                   "must break the tie.")

    def present_partial_refund(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        product = self.rng.choice(PRODUCTS)
        refund = round(amount * self.rng.uniform(0.1, 0.4) / 100) * 100
        m = self._merchant(oid, amount, date, f"Order {oid} {product}", f"ORD-{oid}",
                           status="partially_refunded", refund=refund)
        true = self._settlement(oid, amount, date, f"Settlement for ORD{oid} {product}",
                                f"ORD-{oid}", refund=refund)
        self._emit("present_partial_refund", "present", m,
                   [true] + self._noise(date, 4), true.payment_id,
                   "Refund recorded consistently on both sides; gross still agrees.")

    # ---------- ambiguous: more than one genuine possibility ----------

    def ambiguous_two_plausible(self) -> None:
        oid, amount, date = self._next(), self._amount(), self._date()
        product = self.rng.choice(PRODUCTS)
        m = self._merchant(oid, amount, date, f"Order {oid} {product}", f"ORD-{oid}")
        first = self._settlement(oid, amount, date, f"Settlement for ORD{oid} {product}", f"ORD-{oid}")
        second = self._settlement(self._next(), amount, date + timedelta(hours=6),
                                  f"Settlement for ORD{oid} {product} retry", f"ORD-{oid}")
        self._emit("ambiguous_two_plausible", "ambiguous", m,
                   [first, second] + self._noise(date, 3), None,
                   "Two settlements carry the same reference and amount. Picking one would be a "
                   "guess wearing a decision's clothes.")

    def generate(self, count: int) -> None:
        makers = [
            # absent (the class V2 regressed on) — weighted heaviest
            self.amount_collision_unrelated_reference,
            self.amount_collision_unrelated_merchant,
            self.amount_collision_outside_window,
            self.amount_collision_boilerplate_text,
            self.genuinely_missing_no_candidates,
            # pending
            self.pending_within_settlement_cycle,
            self.pending_with_amount_collision,
            # present
            self.present_exact_reference,
            self.present_reformatted_reference,
            self.present_delayed_settlement,
            self.present_with_amount_collision_decoy,
            self.present_partial_refund,
            # ambiguous
            self.ambiguous_two_plausible,
        ]
        for i in range(count):
            makers[i % len(makers)]()
        self.rng.shuffle(self.examples)
        for i, ex in enumerate(self.examples):
            ex["example_id"] = f"SET{i:04d}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=4127)
    parser.add_argument("--count", type=int, default=300)
    args = parser.parse_args()

    gen = ScenarioGenerator(args.seed)
    gen.generate(args.count)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w") as f:
        for ex in gen.examples:
            f.write(json.dumps(ex) + "\n")
    gen.rng.shuffle(gen.pool)
    with POOL_PATH.open("w") as f:
        for s in gen.pool:
            f.write(json.dumps(asdict(s)) + "\n")

    by_truth: dict[str, int] = {}
    by_scenario: dict[str, int] = {}
    for e in gen.examples:
        by_truth[e["settlement_truth"]] = by_truth.get(e["settlement_truth"], 0) + 1
        by_scenario[e["scenario"]] = by_scenario.get(e["scenario"], 0) + 1

    print(f"Wrote {len(gen.examples)} scenarios to {OUT_PATH}")
    print(f"Wrote {len(gen.pool)} shared settlement records to {POOL_PATH}")
    print(f"  as-of date: {AS_OF.isoformat()}")
    for truth, n in sorted(by_truth.items()):
        print(f"  {truth:<12} {n:>4}")
    print("  by scenario:")
    for name, n in sorted(by_scenario.items()):
        print(f"    {name:<44} {n:>4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
