"""Tiny web service that answers a Klaviyo web feed with a product's per-store
inventory, so browse-abandonment emails can say "in stock at your local store".

Klaviyo calls, at email-send time:
    GET /inventory?product_id={{ event.ProductID }}&token=<FEED_TOKEN>

and the JSON response is exposed in the email as {{ feeds.<name>.* }}.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Query

from inventory import get_product_inventory, load_dotenv

load_dotenv()

# Optional shared secret. If set, the feed URL must include &token=<value>.
# Inventory counts aren't sensitive, but a token keeps the endpoint from being
# trivially scrapable.
FEED_TOKEN = os.environ.get("FEED_TOKEN", "").strip()

app = FastAPI(title="Bici Klaviyo inventory feed", docs_url=None, redoc_url=None)


@app.get("/")
def health() -> dict:
    return {"ok": True, "service": "bici-klaviyo-inventory-feed"}


@app.get("/inventory")
def inventory(
    product_id: str = Query(..., description="Shopify product id (event.ProductID)"),
    token: str = Query("", description="shared secret, if FEED_TOKEN is set"),
) -> dict:
    if FEED_TOKEN and token != FEED_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")
    return get_product_inventory(product_id)
