"""
Synthetic dataset generator for the Settlement Reconciliation Autopilot.

Fully deterministic given a seed: rerunning this script with the same
--seed reproduces byte-identical output. That reproducibility is the
whole point — the held-out evaluation set is only meaningful as a fixed
reference, not something regenerated differently on every run.

Design: one shared pool of Razorpay settlement records (including
deliberate orphans, duplicates, and near-miss decoys), and merchant
records split by category into dev/holdout with STRATIFIED proportions
(every category appears in both splits at roughly the same ratio) so
neither split is skewed toward "easy" or "hard" cases by chance.

Ground truth (the expected outcome) is assigned from the PROBLEM
DEFINITION of each category at construction time — never by running the
matching engine and recording what it happened to output. That's what
makes the resulting accuracy numbers a real measurement instead of a
tautology.

Usage:
    python backend/data/generate_dataset.py --seed 20260903 --total 5000
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain.canonical import content_hash  # noqa: E402

OUT_DIR = Path(__file__).parent / "datasets"

PRODUCTS = [
    "Premium Plan", "Pro Subscription", "Starter Kit", "Annual Membership",
    "Consulting Session", "Widget Bundle", "Express Shipping Add-on",
    "Gift Card", "Onboarding Fee", "Data Export Service", "Team Seat Upgrade",
    "Priority Support Add-on",
]
# Deliberately fine-grained (paise-level, across a wide range) rather than
# a short list of round numbers — real order amounts have far more entropy
# than a handful of price points, and a low-entropy amount space made
# unrelated transactions collide on amount by pure chance far more often
# than a real system would ever see (found during dev-set iteration, see
# docs/EVALUATION.md).
AMOUNT_MIN_MINOR = 9900
AMOUNT_MAX_MINOR = 2499900

FEE_RATE = 0.02
GST_RATE = 0.18

BASE_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Category -> (share of total, expected outcome literal used only for scoring)
CATEGORY_SHARES = {
    "clean_match": 0.55,
    "fee_tax_rounding": 0.10,
    "delayed_settlement_normal": 0.08,
    "delayed_settlement_excessive": 0.03,
    "partial_refund": 0.07,
    "refund_mismatch": 0.02,
    "missing_settlement": 0.06,
    "amount_mismatch": 0.05,
    "duplicate_reference": 0.02,
    "ambiguous_text_reference": 0.02,
}

DEV_SPLIT = 0.8


def compute_fee_tax(gross: int) -> tuple[int, int]:
    fee = round(gross * FEE_RATE)
    tax = round(fee * GST_RATE)
    return fee, tax


@dataclass
class Merchant:
    order_id: str
    reference_id: str | None
    amount_minor: int
    currency: str
    order_date: str
    status: str
    refund_amount_minor: int
    description: str


@dataclass
class Razorpay:
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
class GroundTruthRecord:
    record_id: str
    merchant: Merchant
    ground_truth_case: str
    ground_truth_outcome: str


def iso(dt: datetime) -> str:
    return dt.isoformat()


class Generator:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.seq = 0
        self.razorpay_pool: list[Razorpay] = []
        self.gt_records: list[GroundTruthRecord] = []

    def _next_id(self) -> int:
        self.seq += 1
        return 200000 + self.seq

    def _order_date(self) -> datetime:
        return BASE_DATE + timedelta(days=self.rng.randint(0, 240), hours=self.rng.randint(0, 23))

    def _amount(self) -> int:
        return self.rng.randint(AMOUNT_MIN_MINOR // 100, AMOUNT_MAX_MINOR // 100) * 100

    def _product(self) -> str:
        return self.rng.choice(PRODUCTS)

    def _base_pair(self, oid: int, amount: int, product: str, order_date: datetime) -> tuple[Merchant, Razorpay]:
        order_id = f"ORD{oid}"
        reference = f"ORD-{oid}"
        payment_id = f"pay_{oid:010d}"
        settlement_id = f"setl_{oid:010d}"
        fee, tax = compute_fee_tax(amount)
        settlement_date = order_date + timedelta(days=self.rng.randint(1, 3))
        merchant = Merchant(
            order_id=order_id, reference_id=reference, amount_minor=amount, currency="INR",
            order_date=iso(order_date), status="captured", refund_amount_minor=0,
            description=f"Order {order_id} - {product}",
        )
        razorpay = Razorpay(
            payment_id=payment_id, order_reference=reference, settlement_id=settlement_id,
            gross_amount_minor=amount, fee_minor=fee, tax_minor=tax, net_amount_minor=amount - fee - tax,
            refund_amount_minor=0, order_date=iso(order_date), settlement_date=iso(settlement_date),
            currency="INR", status="settled", description=f"Settlement for {order_id} ({product})",
        )
        return merchant, razorpay

    def _emit(self, case: str, outcome: str, merchant: Merchant, razorpay: list[Razorpay]) -> None:
        self.razorpay_pool.extend(razorpay)
        self.gt_records.append(GroundTruthRecord(record_id=merchant.order_id, merchant=merchant, ground_truth_case=case, ground_truth_outcome=outcome))

    # ---- category generators ----------------------------------------

    def clean_match(self) -> None:
        oid = self._next_id()
        m, r = self._base_pair(oid, self._amount(), self._product(), self._order_date())
        self._emit("clean_match", "RECONCILED", m, [r])

    def fee_tax_rounding(self) -> None:
        oid = self._next_id()
        m, r = self._base_pair(oid, self._amount(), self._product(), self._order_date())
        # A different (still plausible) rounding method on Razorpay's side
        # produces a 1-2 paise discrepancy in net — within tolerance.
        r.net_amount_minor += self.rng.choice([-2, -1, 1, 2])
        self._emit("fee_tax_rounding", "RECONCILED", m, [r])

    def delayed_settlement_normal(self) -> None:
        oid = self._next_id()
        order_date = self._order_date()
        m, r = self._base_pair(oid, self._amount(), self._product(), order_date)
        r.settlement_date = iso(order_date + timedelta(days=self.rng.randint(4, 14)))
        self._emit("delayed_settlement_normal", "RECONCILED", m, [r])

    def delayed_settlement_excessive(self) -> None:
        oid = self._next_id()
        order_date = self._order_date()
        m, r = self._base_pair(oid, self._amount(), self._product(), order_date)
        r.settlement_date = iso(order_date + timedelta(days=self.rng.randint(35, 90)))
        self._emit("delayed_settlement_excessive", "EXCEPTION", m, [r])

    def partial_refund(self) -> None:
        oid = self._next_id()
        amount = self._amount()
        m, r = self._base_pair(oid, amount, self._product(), self._order_date())
        refund = round(amount * self.rng.uniform(0.2, 0.8) / 100) * 100
        m.status = "partially_refunded"
        m.refund_amount_minor = refund
        r.status = "partially_refunded"
        r.refund_amount_minor = refund
        r.net_amount_minor = amount - r.fee_minor - r.tax_minor - refund
        self._emit("partial_refund", "RECONCILED", m, [r])

    def refund_mismatch(self) -> None:
        oid = self._next_id()
        amount = self._amount()
        m, r = self._base_pair(oid, amount, self._product(), self._order_date())
        refund = round(amount * self.rng.uniform(0.2, 0.8) / 100) * 100
        m.status = "partially_refunded"
        m.refund_amount_minor = refund
        r.status = "partially_refunded"
        # Razorpay's side disagrees on the refund amount by a material sum.
        r.refund_amount_minor = refund + self.rng.choice([-5000, -2000, 2000, 5000, 8000])
        r.net_amount_minor = amount - r.fee_minor - r.tax_minor - r.refund_amount_minor
        self._emit("refund_mismatch", "EXCEPTION", m, [r])

    def missing_settlement(self) -> None:
        oid = self._next_id()
        m, _ = self._base_pair(oid, self._amount(), self._product(), self._order_date())
        self._emit("missing_settlement", "EXCEPTION", m, [])  # deliberately no Razorpay counterpart

    def amount_mismatch(self) -> None:
        oid = self._next_id()
        amount = self._amount()
        m, r = self._base_pair(oid, amount, self._product(), self._order_date())
        delta = self.rng.choice([-10000, -5000, -1000, 1000, 5000, 10000])
        m.amount_minor = amount + delta
        self._emit("amount_mismatch", "EXCEPTION", m, [r])

    def duplicate_reference(self) -> None:
        oid = self._next_id()
        amount = self._amount()
        order_date = self._order_date()
        m, r1 = self._base_pair(oid, amount, self._product(), order_date)
        # A retried/duplicated webhook: a second settlement record under
        # the SAME reference, with a near-identical amount — amount alone
        # cannot tell them apart.
        oid2 = self._next_id()
        r2 = Razorpay(
            payment_id=f"pay_{oid2:010d}", order_reference=r1.order_reference, settlement_id=f"setl_{oid2:010d}",
            gross_amount_minor=amount,  # identical to r1 on purpose — amount can't disambiguate
            fee_minor=r1.fee_minor, tax_minor=r1.tax_minor, net_amount_minor=r1.net_amount_minor,
            refund_amount_minor=0, order_date=r1.order_date, settlement_date=r1.settlement_date,
            currency="INR", status="settled", description=r1.description + " (retry)",
        )
        self._emit("duplicate_reference", "HUMAN_REVIEW", m, [r1, r2])

    def ambiguous_text_reference(self) -> None:
        flavor = self.rng.choice(["fuzzy_strong", "semantic_true_match", "semantic_decoy"])
        oid = self._next_id()
        amount = self._amount()
        order_date = self._order_date()
        product = self._product()
        order_id = f"ORD{oid}"
        m = Merchant(
            order_id=order_id, reference_id=f"ORD.{oid}.CHK", amount_minor=amount, currency="INR",
            order_date=iso(order_date), status="captured", refund_amount_minor=0,
            description=f"Payment for Order {oid} - {product} - customer checkout",
        )
        fee, tax = compute_fee_tax(amount)

        if flavor == "fuzzy_strong":
            # Reference differs but description overlap is very high —
            # resolved deterministically, never reaches the model.
            r = Razorpay(
                payment_id=f"pay_{oid:010d}", order_reference=f"RZP/{oid}/SETL", settlement_id=f"setl_{oid:010d}",
                gross_amount_minor=amount, fee_minor=fee, tax_minor=tax, net_amount_minor=amount - fee - tax,
                refund_amount_minor=0, order_date=iso(order_date), settlement_date=iso(order_date + timedelta(days=2)),
                currency="INR", status="settled",
                description=f"Payment for Order {oid} {product} customer checkout settlement",
            )
            self._emit("ambiguous_text_reference_fuzzy_strong", "RECONCILED", m, [r])
        elif flavor == "semantic_true_match":
            # Genuinely the same transaction, but only moderate lexical
            # overlap — this is the case the model is actually for.
            r = Razorpay(
                payment_id=f"pay_{oid:010d}", order_reference=f"RZP/{oid}/SETL", settlement_id=f"setl_{oid:010d}",
                gross_amount_minor=amount, fee_minor=fee, tax_minor=tax, net_amount_minor=amount - fee - tax,
                refund_amount_minor=0, order_date=iso(order_date), settlement_date=iso(order_date + timedelta(days=2)),
                currency="INR", status="settled",
                description=f"Settlement note: order {oid} {product.lower()} razorpay reference differs from merchant record",
            )
            self._emit("ambiguous_text_reference_semantic_true_match", "RECONCILED", m, [r])
        else:  # semantic_decoy: a DIFFERENT transaction, nearby in time,
            # sharing generic words (the product name) but a different
            # order number — a real semantic read should reject this;
            # pure lexical overlap might not.
            decoy_oid = self._next_id()
            r = Razorpay(
                payment_id=f"pay_{decoy_oid:010d}", order_reference=f"RZP/{decoy_oid}/SETL", settlement_id=f"setl_{decoy_oid:010d}",
                gross_amount_minor=amount, fee_minor=fee, tax_minor=tax, net_amount_minor=amount - fee - tax,
                refund_amount_minor=0, order_date=iso(order_date + timedelta(days=1)),
                settlement_date=iso(order_date + timedelta(days=3)),
                currency="INR", status="settled",
                description=f"Settlement note: order {decoy_oid} {product.lower()} priority customer",
            )
            self._emit("ambiguous_text_reference_semantic_decoy", "EXCEPTION", m, [r])

    def generate(self, total: int) -> None:
        counts = {cat: round(total * share) for cat, share in CATEGORY_SHARES.items()}
        # Fix any rounding drift against `total` on the largest category.
        drift = total - sum(counts.values())
        counts["clean_match"] += drift

        dispatch = {
            "clean_match": self.clean_match,
            "fee_tax_rounding": self.fee_tax_rounding,
            "delayed_settlement_normal": self.delayed_settlement_normal,
            "delayed_settlement_excessive": self.delayed_settlement_excessive,
            "partial_refund": self.partial_refund,
            "refund_mismatch": self.refund_mismatch,
            "missing_settlement": self.missing_settlement,
            "amount_mismatch": self.amount_mismatch,
            "duplicate_reference": self.duplicate_reference,
            "ambiguous_text_reference": self.ambiguous_text_reference,
        }
        # Interleave categories (not blocks) so downstream date ordering
        # in the pool is naturally shuffled, then we shuffle explicitly
        # too before splitting.
        plan: list[str] = []
        for cat, n in counts.items():
            plan.extend([cat] * n)
        self.rng.shuffle(plan)
        for cat in plan:
            dispatch[cat]()


def stratified_split(records: list[GroundTruthRecord], dev_share: float, rng: random.Random) -> tuple[list[GroundTruthRecord], list[GroundTruthRecord]]:
    by_case: dict[str, list[GroundTruthRecord]] = {}
    for r in records:
        by_case.setdefault(r.ground_truth_case, []).append(r)
    dev, holdout = [], []
    for case, items in by_case.items():
        items = items[:]
        rng.shuffle(items)
        cut = round(len(items) * dev_share)
        dev.extend(items[:cut])
        holdout.extend(items[cut:])
    rng.shuffle(dev)
    rng.shuffle(holdout)
    return dev, holdout


def write_jsonl(path: Path, records: list[GroundTruthRecord]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps({
                "record_id": r.record_id,
                "merchant": asdict(r.merchant),
                "ground_truth_case": r.ground_truth_case,
                "ground_truth_outcome": r.ground_truth_outcome,
            }) + "\n")


def write_pool(path: Path, pool: list[Razorpay]) -> None:
    with path.open("w") as f:
        for r in pool:
            f.write(json.dumps(asdict(r)) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20260903)
    parser.add_argument("--total", type=int, default=5000)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR,
                        help="Where to write the split. A second evaluation set goes in its own "
                             "directory so it cannot overwrite the one being iterated against.")
    args = parser.parse_args()
    out_dir = args.out_dir

    gen = Generator(args.seed)
    gen.generate(args.total)

    split_rng = random.Random(args.seed + 1)
    dev, holdout = stratified_split(gen.gt_records, DEV_SPLIT, split_rng)

    out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(out_dir / "dev.jsonl", dev)
    write_jsonl(out_dir / "holdout.jsonl", holdout)
    write_pool(out_dir / "razorpay_pool.jsonl", gen.razorpay_pool)

    case_counts: dict[str, int] = {}
    for r in gen.gt_records:
        case_counts[r.ground_truth_case] = case_counts.get(r.ground_truth_case, 0) + 1

    manifest = {
        "seed": args.seed,
        "total_records": len(gen.gt_records),
        "dev_count": len(dev),
        "holdout_count": len(holdout),
        "razorpay_pool_count": len(gen.razorpay_pool),
        "category_counts": case_counts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest["dataset_version"] = content_hash({k: v for k, v in manifest.items() if k != "generated_at"})
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"Generated {len(gen.gt_records)} records (dev={len(dev)}, holdout={len(holdout)}), "
          f"razorpay pool={len(gen.razorpay_pool)}")
    print(f"dataset_version={manifest['dataset_version']}")
    print(json.dumps(case_counts, indent=2))


if __name__ == "__main__":
    main()
