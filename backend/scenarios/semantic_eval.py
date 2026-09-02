"""
Held-out evaluation of the semantic product-equivalence classifier — the
one probabilistic component in this system. Everything else in checks.py
is deterministic and is correctness-tested (see tests/), not accuracy-
measured; this file exists because pretending the semantic layer's
accuracy is 100%, or not measuring it at all, would both be dishonest.

Run:
    python backend/scenarios/semantic_eval.py

Runs against whichever backend app/engine/semantic.get_semantic_verifier()
resolves to (heuristic fallback by default, or the real Claude classifier
if ANTHROPIC_API_KEY is set) — no live servers required, this calls the
Python module directly.

The headline number is NOT plain accuracy. In a payments context the two
error types are not equally bad:
  - a DIFFERENT pair wrongly called EQUIVALENT is a false ALLOW — money
    moves for the wrong item. This is the dangerous error.
  - a SAME pair wrongly called MATERIAL_CHANGE is a false BLOCK — an
    annoyance, not a loss.
  - AMBIGUOUS on either is the conservative, safe outcome (decision.py
    turns it into REQUIRE_RECONFIRMATION, never an automatic ALLOW), so
    it is reported separately from "wrong," not folded into "correct."
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.engine.semantic import ProductAttrs, get_semantic_verifier  # noqa: E402


@dataclass
class LabeledPair:
    declared_name: str
    declared_category: str
    observed_name: str
    observed_category: str
    label: str  # "SAME" or "DIFFERENT"
    note: str = ""


DATASET: list[LabeledPair] = [
    # --- genuinely SAME product, surface variation only ------------------
    LabeledPair("Amul Butter 500g", "groceries", "Amul Butter 500 grams", "groceries", "SAME", "unit phrasing"),
    LabeledPair("Amul Butter 500g", "groceries", "AMUL BUTTER 500G", "groceries", "SAME", "case only"),
    LabeledPair("USB-C Cable 1m", "electronics", "USB C Cable 1 metre", "electronics", "SAME", "unit + hyphen"),
    LabeledPair("Wireless Mouse", "electronics", "Wireless  Mouse", "electronics", "SAME", "whitespace"),
    LabeledPair("Salted Potato Chips 150g", "snacks-savory", "Salted Chips 150g", "snacks-savory", "SAME", "dropped filler word"),
    LabeledPair("Oats & Almond Granola Bar 40g", "snacks-health", "Oats and Almond Granola Bar 40g", "snacks-health", "SAME", "'&' vs 'and'"),
    LabeledPair("Wireless Mouse", "electronics", "Wireless Mouse (Black)", "electronics", "SAME", "color variant, same core product"),
    LabeledPair("USB-C Cable 1m", "electronics", "USB-C to USB-C Cable, 1m", "electronics", "SAME", "more precise spec, same item"),

    # --- genuinely DIFFERENT product, some lexical overlap ---------------
    LabeledPair("Wireless Mouse", "electronics", "Wireless Mouse Premium Gaming Bundle", "electronics", "DIFFERENT", "bundle upsell"),
    LabeledPair("Amul Butter 500g", "groceries", "Imported Gourmet Butter Hamper", "gourmet-gifting", "DIFFERENT", "different tier + category"),
    LabeledPair("Salted Potato Chips 150g", "snacks-savory", "Oats & Almond Granola Bar 40g", "snacks-health", "DIFFERENT", "different product entirely"),
    LabeledPair("USB-C Cable 1m", "electronics", "USB-C Cable 3m", "electronics", "DIFFERENT", "different length, materially different SKU"),
    LabeledPair("Wireless Mouse", "electronics", "Wireless Keyboard", "electronics", "DIFFERENT", "different product, same category"),
    LabeledPair("Amul Butter 500g", "groceries", "Amul Cheese 500g", "groceries", "DIFFERENT", "different product, same brand/size"),
    LabeledPair("Salted Potato Chips 150g", "snacks-savory", "Salted Potato Chips 30g", "snacks-savory", "DIFFERENT", "same name, different (much smaller) size"),
    LabeledPair("Wireless Mouse", "electronics", "Wireless Mouse Pad", "electronics", "DIFFERENT", "accessory, not the product itself"),

    # --- genuinely ambiguous: reasonable to punt to reconfirmation --------
    LabeledPair("Salted Potato Chips 150g", "snacks-savory", "Classic Salted Chips 150 grams", "snacks-savory", "SAME", "rebrand wording, arguably same SKU"),
    LabeledPair("Wireless Mouse", "electronics", "Optical Wireless Mouse", "electronics", "SAME", "added accurate spec, likely same item"),
    LabeledPair("Oats & Almond Granola Bar 40g", "snacks-health", "Almond Granola Bar 40g", "snacks-health", "SAME", "brand-name oat detail dropped"),
    LabeledPair("USB-C Cable 1m", "electronics", "Fast Charging USB-C Cable 1m", "electronics", "SAME", "added marketing descriptor, same spec"),
]


def run() -> None:
    verifier = get_semantic_verifier()
    backend_name = type(verifier).__name__

    counts = {
        ("SAME", "EQUIVALENT"): 0, ("SAME", "AMBIGUOUS"): 0, ("SAME", "MATERIAL_CHANGE"): 0,
        ("DIFFERENT", "EQUIVALENT"): 0, ("DIFFERENT", "AMBIGUOUS"): 0, ("DIFFERENT", "MATERIAL_CHANGE"): 0,
    }
    rows = []
    errors = []
    pacing_seconds = 5.0 if backend_name == "GeminiSemanticVerifier" else 0.0  # free-tier RPM headroom
    for i, pair in enumerate(DATASET):
        if pacing_seconds and i > 0:
            time.sleep(pacing_seconds)
        try:
            result = verifier.compare(
                declared=ProductAttrs(name=pair.declared_name, category=pair.declared_category),
                observed=ProductAttrs(name=pair.observed_name, category=pair.observed_category),
                user_constraint_text=None,
            )
        except Exception as exc:  # noqa: BLE001 — a rate-limited pair shouldn't abort the whole eval
            errors.append((pair.declared_name, pair.observed_name, str(exc)[:120]))
            continue
        counts[(pair.label, result.verdict)] += 1
        rows.append((pair.label, result.verdict, pair.declared_name, pair.observed_name, pair.note))

    n_same = sum(v for (label, _), v in counts.items() if label == "SAME")
    n_diff = sum(v for (label, _), v in counts.items() if label == "DIFFERENT")

    dangerous_false_allow = counts[("DIFFERENT", "EQUIVALENT")]
    safe_false_block = counts[("SAME", "MATERIAL_CHANGE")]

    print(f"\nSemantic classifier evaluation — backend: {backend_name}\n")
    print(f"{'DECLARED':<38} {'OBSERVED':<38} {'LABEL':<10} {'VERDICT':<14} NOTE")
    print("-" * 130)
    for label, verdict, declared, observed, note in rows:
        print(f"{declared:<38} {observed:<38} {label:<10} {verdict:<14} {note}")

    print("\n--- Confusion (rows=ground truth, cols=verdict) ---")
    print(f"{'':12}{'EQUIVALENT':>12}{'AMBIGUOUS':>12}{'MATERIAL_CHANGE':>16}")
    print(f"{'SAME':12}{counts[('SAME','EQUIVALENT')]:>12}{counts[('SAME','AMBIGUOUS')]:>12}{counts[('SAME','MATERIAL_CHANGE')]:>16}")
    print(f"{'DIFFERENT':12}{counts[('DIFFERENT','EQUIVALENT')]:>12}{counts[('DIFFERENT','AMBIGUOUS')]:>12}{counts[('DIFFERENT','MATERIAL_CHANGE')]:>16}")

    evaluated = len(rows)
    print("\n--- Honest headline metrics (NOT plain accuracy) ---")
    print(f"n = {evaluated}/{len(DATASET)} pairs evaluated ({n_same} SAME, {n_diff} DIFFERENT)")
    if errors:
        print(f"{len(errors)} pair(s) skipped due to provider errors (e.g. rate limits) — NOT counted as correct or wrong:")
        for declared, observed, msg in errors:
            print(f"    - {declared!r} vs {observed!r}: {msg}")
    if n_diff:
        print(
            f"Dangerous false ALLOW rate (DIFFERENT called EQUIVALENT): "
            f"{dangerous_false_allow}/{n_diff} = {dangerous_false_allow / n_diff:.1%}"
            "  <- this is the number that matters most for a payments system"
        )
    if n_same:
        print(
            f"Safe false BLOCK rate (SAME called MATERIAL_CHANGE): "
            f"{safe_false_block}/{n_same} = {safe_false_block / n_same:.1%}"
            "  <- annoying (unnecessary reconfirmation), not dangerous"
        )
    if evaluated:
        ambiguous_rate = (counts[("SAME", "AMBIGUOUS")] + counts[("DIFFERENT", "AMBIGUOUS")]) / evaluated
        print(f"AMBIGUOUS (punted to reconfirmation) rate: {ambiguous_rate:.1%}")


if __name__ == "__main__":
    run()
