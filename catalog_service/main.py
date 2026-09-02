"""
A standalone mock merchant catalog service.

This is deliberately a SEPARATE process on its own port, not a Python
function Interlock calls in-process. The entire point of Interlock's
ground-truth check is "independently fetch from the merchant's own
system, don't trust the agent's claim" — if the "independent fetch"
were just reading a shared in-memory dict, that claim would be fiction.

Honesty note (see README): this is a stand-in for a real merchant catalog
API (Zomato/Swiggy/BigBasket-class integrations are not accessible in a
hackathon). It implements the same shape of interface — GET a product by
id, get current price/availability/category — that Interlock's catalog
client talks to, so swapping this for a real merchant endpoint is a
client-config change, not an architecture change.

The /admin/* endpoints exist ONLY so the scenario runner and demo can
simulate a merchant-side change (price update, product swap, restock)
between when an agent observes the catalog and when the transaction
reaches Interlock. A real deployment would not expose these — real drift
comes from real merchant systems changing on their own schedule.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DATA_PATH = Path(__file__).parent / "data.json"

app = FastAPI(title="Mock Merchant Catalog Service")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def _load() -> dict:
    return json.loads(DATA_PATH.read_text())


def _save(data: dict) -> None:
    DATA_PATH.write_text(json.dumps(data, indent=2))


class ProductOut(BaseModel):
    merchant_id: str
    product_id: str
    name: str
    category: str
    price_minor: int
    currency: str = "INR"
    available: bool = True


class ProductPatch(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    price_minor: Optional[int] = None
    available: Optional[bool] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/merchants/{merchant_id}/products", response_model=list[ProductOut])
def list_products(merchant_id: str):
    data = _load()
    merchant = data.get(merchant_id)
    if not merchant:
        raise HTTPException(404, f"unknown merchant {merchant_id}")
    return list(merchant.values())


@app.get("/merchants/{merchant_id}/products/{product_id}", response_model=ProductOut)
def get_product(merchant_id: str, product_id: str):
    data = _load()
    merchant = data.get(merchant_id)
    if not merchant or product_id not in merchant:
        raise HTTPException(404, f"unknown product {merchant_id}/{product_id}")
    return merchant[product_id]


@app.patch("/admin/merchants/{merchant_id}/products/{product_id}", response_model=ProductOut)
def patch_product(merchant_id: str, product_id: str, patch: ProductPatch):
    """Test-harness-only: simulate a merchant-side catalog change."""
    data = _load()
    merchant = data.get(merchant_id)
    if not merchant or product_id not in merchant:
        raise HTTPException(404, f"unknown product {merchant_id}/{product_id}")
    product = merchant[product_id]
    for field, value in patch.model_dump(exclude_unset=True).items():
        product[field] = value
    _save(data)
    return product


@app.post("/admin/reset")
def reset():
    """Restores the seed catalog — used between scenario runs / eval batches."""
    seed = json.loads((Path(__file__).parent / "data.seed.json").read_text())
    _save(seed)
    return {"status": "reset"}
