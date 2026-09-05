"""
The hard evaluation dataset.

The V3 generator was saturated: it embedded the order number in both the
reference and the description, so once identity evidence was used
properly every ambiguous case fell out deterministically and the model was
never called. A dataset that a deterministic system scores 100% on cannot
measure anything above that system.

This generator withholds identity evidence on purpose for a substantial
minority of records, the way real reconciliation data does:

- bank narration is truncated and consonant-stripped, the way statements
  actually render it
- merchant names appear as legal entity on one side and trading name on
  the other
- some references live in different numbering systems entirely (an
  invoice number against a UTR), so no identifier can corroborate
- some references are corrupted, transposed, or truncated rather than
  merely reformatted

And it keeps the traps that punish naive matching:

- same amount, different transaction
- same amount and same date, different transaction
- adjacent invoice numbers with identical amounts
- near-duplicate transactions minutes apart
- references that contradict within one numbering system

Ground truth is assigned from each category's construction, never by
running the engine. Nothing leaks the answer: where a case is meant to
require semantics, no shared identifier exists anywhere in either record.

Usage:
    python backend/data/generate_final_dataset.py --seed 90210 --total 4000
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain.canonical import content_hash  # noqa: E402

OUT_DIR = Path(__file__).parent / "datasets_final"

BASE_DATE = datetime(2026, 1, 6, tzinfo=timezone.utc)
FEE_RATE = 0.021
GST_RATE = 0.18
DEV_SPLIT = 0.75

# (legal entity, trading name, bank-narration form)
COUNTERPARTIES = [
    ("Northwind Retail Private Limited", "Northwind Store", "NORTHWND RTL"),
    ("Kalyan Technologies Pvt Ltd", "KalyanTech", "KALYANTECH"),
    ("Sunrise Commerce LLP", "Sunrise Shop", "SUNRISE COMM"),
    ("Meridian Softworks Pvt Ltd", "Meridian Apps", "MERIDIAN SFT"),
    ("Anand Traders Private Limited", "Anand Traders", "ANANDTRDRS"),
    ("Bluepeak Services Pvt Ltd", "Bluepeak", "BLUEPEAK SVC"),
    ("Vantage Analytics Pvt Ltd", "Vantage", "VANTAGE ANLY"),
    ("Harbourline Foods Pvt Ltd", "Harbourline", "HARBRLINE FD"),
]

# (booked description, bank-narration form)
PRODUCTS = [
    ("Annual cloud platform subscription", "CLDPLTFRM ANNL"),
    ("Pro subscription renewal", "PRO SUBSCR RNW"),
    ("Enterprise onboarding fee", "ENTPR ONBRDNG"),
    ("Data export service", "DATA EXP SVC"),
    ("Team seat upgrade", "TEAM SEAT UPG"),
    ("Priority support add-on", "PRIO SUPPORT"),
    ("Consulting session block", "CONSULT SESN"),
    ("Widget bundle premium", "WDGT BNDL PRM"),
    ("Express shipping add-on", "EXP SHIPPING"),
    ("Starter plan monthly", "STARTER PLAN"),
]

CATEGORY_SHARES = {
    # ---- resolvable deterministically -------------------------------
    "exact_reference": 0.26,
    "reformatted_reference": 0.09,
    "fee_tax_normal": 0.07,
    "delayed_normal": 0.05,
    "partial_refund": 0.05,
    # ---- deterministic exceptions -----------------------------------
    "amount_mismatch": 0.05,
    "fee_tax_broken": 0.03,
    "delayed_excessive": 0.03,
    "refund_mismatch": 0.02,
    "currency_mismatch": 0.02,
    "missing_settlement": 0.06,
    "pending_settlement": 0.02,
    # ---- genuine ambiguity: identity evidence withheld --------------
    "bank_narration_match": 0.07,      # truncated narration, UTR namespace
    "merchant_alias_match": 0.05,      # legal entity vs trading name
    "corrupted_reference": 0.04,       # transposed/truncated digits
    # ---- traps ------------------------------------------------------
    "same_amount_different_txn": 0.04,
    "same_amount_same_date": 0.02,
    "adjacent_invoice_same_amount": 0.02,
    "near_duplicate": 0.01,
}


def fee_tax(gross: int) -> tuple[int, int]:
    fee = round(gross * FEE_RATE)
    return fee, round(fee * GST_RATE)


@dataclass
class Ledger:
    order_id: str
    reference_id: str | None
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


@dataclass
class Case:
    record_id: str
    ledger: Ledger
    category: str
    expected_outcome: str
    requires_semantics: bool


class Generator:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.invoice_seq = 40000
        self.utr_seq = 880000
        self.pool: list[Settlement] = []
        self.cases: list[Case] = []
        # The observation point, so "pending" is well defined.
        self.as_of = BASE_DATE + timedelta(days=210)

    # ---- primitives -------------------------------------------------

    def _invoice(self) -> int:
        self.invoice_seq += self.rng.randint(1, 4)
        return self.invoice_seq

    def _utr(self) -> int:
        """Bank references live in their own numbering system, six digits
        wide, deliberately never overlapping the invoice counter."""
        self.utr_seq += self.rng.randint(1, 9)
        return self.utr_seq

    def _amount(self) -> int:
        return self.rng.randint(199, 48000) * 100

    def _date(self, days_before_as_of: int | None = None) -> datetime:
        if days_before_as_of is None:
            days_before_as_of = self.rng.randint(12, 190)
        return self.as_of - timedelta(days=days_before_as_of, hours=self.rng.randint(0, 23))

    def _settle(self, ident: str, amount: int, when: datetime, description: str, reference: str,
                delay: int = 2, refund: int = 0, currency: str = "INR",
                net_override: int | None = None) -> Settlement:
        fee, tax = fee_tax(amount)
        return Settlement(
            payment_id=f"pay_{ident}", order_reference=reference, settlement_id=f"setl_{ident}",
            gross_amount_minor=amount, fee_minor=fee, tax_minor=tax,
            net_amount_minor=net_override if net_override is not None else amount - fee - tax - refund,
            refund_amount_minor=refund, order_date=when.isoformat(),
            settlement_date=(when + timedelta(days=delay)).isoformat(),
            currency=currency, status="partially_refunded" if refund else "settled",
            description=description,
        )

    def _emit(self, category: str, outcome: str, ledger: Ledger,
              settlements: list[Settlement], requires_semantics: bool = False) -> None:
        self.pool.extend(settlements)
        self.cases.append(Case(ledger.order_id, ledger, category, outcome, requires_semantics))

    def _ledger(self, order_id: str, reference: str | None, amount: int, when: datetime,
                description: str, currency: str = "INR", refund: int = 0,
                status: str = "captured") -> Ledger:
        return Ledger(order_id, reference, amount, currency, when.isoformat(), status, refund, description)

    def _noise(self, near: datetime, count: int) -> list[Settlement]:
        out = []
        for _ in range(count):
            inv, amount = self._invoice(), self._amount()
            _, trading, _ = self.rng.choice(COUNTERPARTIES)
            booked, _ = self.rng.choice(PRODUCTS)
            out.append(self._settle(f"{inv:06d}", amount, near + timedelta(days=self.rng.randint(-5, 5)),
                                    f"Settlement INV-{inv} {trading} {booked}", f"INV-{inv}"))
        return out

    # ---- deterministic categories -----------------------------------

    def exact_reference(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date()
        legal, trading, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, f"{booked} - {legal}")
        self._emit("exact_reference", "RECONCILED", led,
                   [self._settle(f"{inv:06d}", amount, when, f"Settlement INV-{inv} {trading} {booked}", f"INV-{inv}")])

    def reformatted_reference(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date()
        legal, trading, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV/{inv}/A", amount, when, f"{booked} - {legal}")
        self._emit("reformatted_reference", "RECONCILED", led,
                   [self._settle(f"{inv:06d}", amount, when, f"Settlement {inv} {trading} {booked}", f"INV-{inv}")])

    def fee_tax_normal(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date()
        _, trading, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, booked)
        self._emit("fee_tax_normal", "RECONCILED", led,
                   [self._settle(f"{inv:06d}", amount, when, f"Settlement INV-{inv} {trading}", f"INV-{inv}")])

    def delayed_normal(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date()
        _, trading, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, booked)
        self._emit("delayed_normal", "RECONCILED", led,
                   [self._settle(f"{inv:06d}", amount, when, f"Settlement INV-{inv} {trading}",
                                 f"INV-{inv}", delay=self.rng.randint(6, 16))])

    def partial_refund(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date()
        refund = round(amount * self.rng.uniform(0.1, 0.4) / 100) * 100
        _, trading, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, booked,
                           refund=refund, status="partially_refunded")
        self._emit("partial_refund", "RECONCILED", led,
                   [self._settle(f"{inv:06d}", amount, when, f"Settlement INV-{inv} {trading}",
                                 f"INV-{inv}", refund=refund)])

    def amount_mismatch(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date()
        _, trading, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, booked)
        off = amount + self.rng.choice([-1, 1]) * self.rng.randint(500, 90000)
        self._emit("amount_mismatch", "EXCEPTION", led,
                   [self._settle(f"{inv:06d}", max(off, 100), when, f"Settlement INV-{inv} {trading}", f"INV-{inv}")])

    def fee_tax_broken(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date()
        _, trading, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, booked)
        fee, tax = fee_tax(amount)
        self._emit("fee_tax_broken", "EXCEPTION", led,
                   [self._settle(f"{inv:06d}", amount, when, f"Settlement INV-{inv} {trading}", f"INV-{inv}",
                                 net_override=amount - fee - tax - self.rng.randint(700, 9000))])

    def delayed_excessive(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date(days_before_as_of=self.rng.randint(60, 180))
        _, trading, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, booked)
        self._emit("delayed_excessive", "EXCEPTION", led,
                   [self._settle(f"{inv:06d}", amount, when, f"Settlement INV-{inv} {trading}",
                                 f"INV-{inv}", delay=self.rng.randint(30, 55))])

    def refund_mismatch(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date()
        _, trading, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        refund = round(amount * 0.3 / 100) * 100
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, booked,
                           refund=refund, status="partially_refunded")
        self._emit("refund_mismatch", "EXCEPTION", led,
                   [self._settle(f"{inv:06d}", amount, when, f"Settlement INV-{inv} {trading}",
                                 f"INV-{inv}", refund=max(refund // 3, 100))])

    def currency_mismatch(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date()
        _, trading, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, booked, currency="USD")
        self._emit("currency_mismatch", "EXCEPTION", led,
                   [self._settle(f"{inv:06d}", amount, when, f"Settlement INV-{inv} {trading}", f"INV-{inv}")])

    def missing_settlement(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date()
        legal, _, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, f"{booked} - {legal}")
        self._emit("missing_settlement", "EXCEPTION", led, self._noise(when, 2))

    def pending_settlement(self) -> None:
        inv, amount = self._invoice(), self._amount()
        when = self.as_of - timedelta(days=self.rng.randint(0, 1), hours=self.rng.randint(1, 20))
        legal, _, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, f"{booked} - {legal}")
        self._emit("pending_settlement", "EXCEPTION", led, [])

    # ---- genuine ambiguity ------------------------------------------

    def bank_narration_match(self) -> None:
        """The real bank-statement case. The settlement side is a bank line:
        its reference is a UTR from a different numbering system, and its
        narration is truncated and consonant-stripped. Nothing identifies
        the pair except amount, date and what the narration means."""
        inv, amount, when = self._invoice(), self._amount(), self._date()
        legal, _, bank_form = self.rng.choice(COUNTERPARTIES)
        booked, bank_product = self.rng.choice(PRODUCTS)
        utr = self._utr()
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, f"{booked} - {legal}")
        self._emit("bank_narration_match", "RECONCILED", led,
                   [self._settle(f"{utr:06d}", amount, when + timedelta(days=1),
                                 f"NEFT INWARD {bank_form} {bank_product}", f"UTR{utr}", delay=0)]
                   + self._noise(when, 1),
                   requires_semantics=True)

    def merchant_alias_match(self) -> None:
        """Legal entity on the books, trading name on the statement, and a
        reference in the bank's own numbering system."""
        inv, amount, when = self._invoice(), self._amount(), self._date()
        legal, trading, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        utr = self._utr()
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, f"{legal} - {booked}")
        self._emit("merchant_alias_match", "RECONCILED", led,
                   [self._settle(f"{utr:06d}", amount, when, f"{trading} payout", f"UTR{utr}", delay=1)]
                   + self._noise(when, 1),
                   requires_semantics=True)

    def corrupted_reference(self) -> None:
        """Two digits transposed in the reference — same numbering system,
        so the identifiers genuinely contradict, and only the amount, date
        and wording can rescue it."""
        inv, amount, when = self._invoice(), self._amount(), self._date()
        _, trading, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        # Transposing two identical digits is a no-op, and a "corrupted"
        # reference that is in fact identical reconciles cleanly — which
        # showed up as a false auto-reconciliation attributable to the
        # generator rather than the engine. Find a pair that differs.
        digits = list(str(inv))
        swap = next((i for i in range(len(digits) - 1, 0, -1) if digits[i] != digits[i - 1]), None)
        if swap is None:                      # e.g. 44444 — perturb instead
            digits[-1] = str((int(digits[-1]) + 1) % 10)
        else:
            digits[swap], digits[swap - 1] = digits[swap - 1], digits[swap]
        corrupted = "".join(digits)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, f"{booked} - {trading}")
        self._emit("corrupted_reference", "HUMAN_REVIEW", led,
                   [self._settle(f"{inv:06d}", amount, when, f"Settlement {trading} {booked}",
                                 f"INV-{corrupted}")],
                   requires_semantics=True)

    # ---- traps ------------------------------------------------------

    def same_amount_different_txn(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date()
        legal, _, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, f"{booked} - {legal}")
        other_legal, other_trading, _ = self.rng.choice([c for c in COUNTERPARTIES if c[0] != legal])
        other_booked, _ = self.rng.choice([p for p in PRODUCTS if p[0] != booked])
        utr = self._utr()
        self._emit("same_amount_different_txn", "EXCEPTION", led,
                   [self._settle(f"{utr:06d}", amount, when + timedelta(days=2),
                                 f"NEFT INWARD {other_trading} {other_booked}", f"UTR{utr}", delay=0)])

    def same_amount_same_date(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date()
        legal, _, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, f"{booked} - {legal}")
        other_legal, other_trading, _ = self.rng.choice([c for c in COUNTERPARTIES if c[0] != legal])
        other_booked, _ = self.rng.choice([p for p in PRODUCTS if p[0] != booked])
        utr = self._utr()
        self._emit("same_amount_same_date", "EXCEPTION", led,
                   [self._settle(f"{utr:06d}", amount, when, f"{other_trading} {other_booked}",
                                 f"UTR{utr}", delay=0)])

    def adjacent_invoice_same_amount(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date()
        _, trading, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, f"{booked} - {trading}")
        self._emit("adjacent_invoice_same_amount", "EXCEPTION", led,
                   [self._settle(f"{inv + 1:06d}", amount, when + timedelta(hours=3),
                                 f"Settlement INV-{inv + 1} {trading} {booked}", f"INV-{inv + 1}")])

    def near_duplicate(self) -> None:
        inv, amount, when = self._invoice(), self._amount(), self._date()
        _, trading, _ = self.rng.choice(COUNTERPARTIES)
        booked, _ = self.rng.choice(PRODUCTS)
        led = self._ledger(f"ORD{inv}", f"INV-{inv}", amount, when, f"{booked} - {trading}")
        second = self._invoice()
        self._emit("near_duplicate", "HUMAN_REVIEW", led, [
            self._settle(f"{inv:06d}", amount, when, f"Settlement INV-{inv} {trading} {booked}", f"INV-{inv}"),
            self._settle(f"{second:06d}", amount, when + timedelta(minutes=8),
                         f"Settlement INV-{inv} {trading} {booked} retry", f"INV-{inv}"),
        ])

    def generate(self, total: int) -> None:
        counts = {c: round(total * share) for c, share in CATEGORY_SHARES.items()}
        counts["exact_reference"] += total - sum(counts.values())
        dispatch = {name: getattr(self, name) for name in CATEGORY_SHARES}
        plan = [c for c, n in counts.items() for _ in range(n)]
        self.rng.shuffle(plan)
        for category in plan:
            dispatch[category]()


def stratified_split(cases: list[Case], dev_share: float, rng: random.Random) -> tuple[list[Case], list[Case]]:
    by_category: dict[str, list[Case]] = {}
    for case in cases:
        by_category.setdefault(case.category, []).append(case)
    dev, holdout = [], []
    for items in by_category.values():
        shuffled = items[:]
        rng.shuffle(shuffled)
        cut = round(len(shuffled) * dev_share)
        dev.extend(shuffled[:cut])
        holdout.extend(shuffled[cut:])
    rng.shuffle(dev)
    rng.shuffle(holdout)
    return dev, holdout


def write_cases(path: Path, cases: list[Case]) -> None:
    with path.open("w") as f:
        for case in cases:
            f.write(json.dumps({
                "record_id": case.record_id,
                "merchant": asdict(case.ledger),
                "ground_truth_case": case.category,
                "ground_truth_outcome": case.expected_outcome,
                "requires_semantics": case.requires_semantics,
            }) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=90210)
    parser.add_argument("--total", type=int, default=4000)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args()

    gen = Generator(args.seed)
    gen.generate(args.total)
    dev, holdout = stratified_split(gen.cases, DEV_SPLIT, random.Random(args.seed + 7))

    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    write_cases(out / "dev.jsonl", dev)
    write_cases(out / "holdout.jsonl", holdout)
    with (out / "razorpay_pool.jsonl").open("w") as f:
        for s in gen.pool:
            f.write(json.dumps(asdict(s)) + "\n")

    counts: dict[str, int] = {}
    for case in gen.cases:
        counts[case.category] = counts.get(case.category, 0) + 1

    manifest = {
        "generator": "generate_final_dataset.py",
        "generator_version": "1.0",
        "seed": args.seed,
        "total_records": len(gen.cases),
        "dev_count": len(dev),
        "holdout_count": len(holdout),
        "razorpay_pool_count": len(gen.pool),
        "as_of": gen.as_of.isoformat(),
        "semantics_required_count": sum(1 for c in gen.cases if c.requires_semantics),
        "category_counts": counts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest["dataset_version"] = content_hash({k: v for k, v in manifest.items() if k != "generated_at"})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"Wrote {len(gen.cases)} cases and {len(gen.pool)} settlements to {out}")
    print(f"  dev {len(dev)} / holdout {len(holdout)}   as-of {gen.as_of.date()}")
    print(f"  requiring semantics: {manifest['semantics_required_count']} "
          f"({manifest['semantics_required_count'] / len(gen.cases):.1%})")
    for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {name:<32} {n:>5}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
