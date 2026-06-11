"""Read per-store inventory for a single Shopify product by summing the
per-location variant metafields (namespace "Channels") across all its variants.

Used by the web service (app.py) to answer a Klaviyo web feed at email-send
time: given a product id, return how many units sit at each Bici store.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests

SHOPIFY_API_VERSION = "2024-10"

# variant metafield key (namespace "Channels") -> the field name we return.
LOCATION_FIELDS = [
    ("bicivictoria_inventory", "victoria"),
    ("bicilangford_inventory", "langford"),
    ("biciadanac_inventory", "adanac"),
    ("virtualwarehouse_inventory", "virtualwarehouse"),
]

CACHE_TTL_SECONDS = 300  # serve a product's sums from memory for 5 min
_cache: dict[str, tuple[float, dict]] = {}


def load_dotenv() -> None:
    """Fill missing env vars from a local .env (gitignored). Real env vars
    (Render) always win."""
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"missing required environment variable {name}")
    return val


def store_handle(raw: str) -> str:
    return raw.strip().lower().removesuffix(".myshopify.com")


def _to_int(metafield: dict | None) -> int:
    """Null / non-numeric metafield counts as 0 (matches the theme's
    `| plus: 0` coercion)."""
    if not metafield:
        return 0
    try:
        return int(float(metafield.get("value") or 0))
    except (TypeError, ValueError):
        return 0


def _numeric_id(raw: str) -> str:
    """Tolerate a bare id ("123") or a gid ("gid://shopify/Product/123")."""
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    return digits


PRODUCT_QUERY = """
query($id: ID!) {
  product(id: $id) {
    title
    variants(first: 100) {
      pageInfo { hasNextPage }
      nodes {
        victoria:         metafield(namespace: "Channels", key: "bicivictoria_inventory") { value }
        langford:         metafield(namespace: "Channels", key: "bicilangford_inventory") { value }
        adanac:           metafield(namespace: "Channels", key: "biciadanac_inventory") { value }
        virtualwarehouse: metafield(namespace: "Channels", key: "virtualwarehouse_inventory") { value }
      }
    }
  }
}
"""


def shopify_graphql(query: str, variables: dict) -> dict:
    store = store_handle(require_env("SHOPIFY_STORE"))
    token = require_env("SHOPIFY_ADMIN_TOKEN")
    url = f"https://{store}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    resp = requests.post(
        url,
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        raise RuntimeError(f"Shopify GraphQL errors: {body['errors']}")
    return body


def get_product_inventory(product_id: str, *, use_cache: bool = True) -> dict:
    """Return per-store sums for one product, e.g.:
        {"product_id": "8073171337279", "title": "...", "found": true,
         "victoria": 5, "langford": 0, "adanac": 13, "virtualwarehouse": 0,
         "in_stock_anywhere": true}

    Unknown product / Shopify hiccup => all-zero, found=False, but still a 200
    so the email renders the safe ("not in stock") branch rather than breaking.
    """
    pid = _numeric_id(product_id)
    base = {f: 0 for _, f in LOCATION_FIELDS}
    if not pid:
        return {"product_id": str(product_id), "title": None, "found": False,
                **base, "in_stock_anywhere": False}

    now = time.time()
    if use_cache and pid in _cache:
        ts, cached = _cache[pid]
        if now - ts < CACHE_TTL_SECONDS:
            return cached

    result = {"product_id": pid, "title": None, "found": False,
              **base, "in_stock_anywhere": False}
    try:
        data = shopify_graphql(PRODUCT_QUERY, {"id": f"gid://shopify/Product/{pid}"})
        product = data.get("data", {}).get("product")
        if product:
            totals = dict(base)
            for v in product["variants"]["nodes"]:
                # The GraphQL query aliases each metafield to its output field
                # name (victoria/langford/adanac/virtualwarehouse), so look the
                # value up by `field`, not the metafield key.
                for _key, field in LOCATION_FIELDS:
                    totals[field] += _to_int(v.get(field))
            result.update(totals)
            result["title"] = product.get("title")
            result["found"] = True
            result["in_stock_anywhere"] = any(val > 0 for val in totals.values())
    except (requests.RequestException, RuntimeError):
        # Leave result as the safe all-zero default.
        pass

    if use_cache:
        _cache[pid] = (now, result)
    return result
