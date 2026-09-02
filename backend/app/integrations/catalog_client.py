"""
HTTP client to the merchant catalog service. This is the one function in
the entire codebase that is allowed to define "ground truth" — everything
in the engine compares AGAINST what this returns, never the reverse.

It is a real network call over HTTP to a separate process, on purpose.
"""

from __future__ import annotations

import os

import httpx

from app.domain.models import ProductRef

CATALOG_BASE_URL = os.environ.get("CATALOG_BASE_URL", "http://127.0.0.1:8100")


class CatalogUnavailable(RuntimeError):
    pass


class ProductNotFound(RuntimeError):
    pass


def fetch_ground_truth(merchant_id: str, product_id: str) -> ProductRef:
    """Independently fetch the merchant's current, live view of a product.
    Never cache this across a verify call — staleness of THIS call is
    exactly what the staleness check is trying to catch."""
    url = f"{CATALOG_BASE_URL}/merchants/{merchant_id}/products/{product_id}"
    try:
        resp = httpx.get(url, timeout=5.0)
    except httpx.HTTPError as exc:
        raise CatalogUnavailable(f"catalog service unreachable: {exc}") from exc
    if resp.status_code == 404:
        raise ProductNotFound(f"{merchant_id}/{product_id} not found in merchant catalog")
    resp.raise_for_status()
    return ProductRef.model_validate(resp.json())
