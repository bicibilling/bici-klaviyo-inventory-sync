#!/usr/bin/env python3
"""Sync per-store inventory from Shopify variant metafields into Klaviyo's
product catalog as custom_metadata, so browse-abandonment (and other) flows
can tell a customer an item is in stock at their local Bici store.

For each active Shopify product it sums the per-location stock across ALL
variants (the email cares about the product, not a single size) and writes
three numbers onto the product's Klaviyo catalog item:

    victoria_inventory, langford_inventory, adanac_inventory

plus virtualwarehouse_inventory and a local_inventory_synced_at timestamp.

Reads three values from the environment (or a local .env, gitignored):
    SHOPIFY_STORE         e.g. "la-bicicletta-vancouver"
    SHOPIFY_ADMIN_TOKEN   shpat_...   (read_products)
    KLAVIYO_API_KEY       pk_...      (Catalogs full access)

Usage:
    python sync.py                 # full sweep: read Shopify, push to Klaviyo
    python sync.py --dry-run       # read + sum, print, DON'T write to Klaviyo
    python sync.py --limit 200     # only process the first N products (testing)
    python sync.py --discover 123  # print the Klaviyo catalog item for Shopify
                                   #   product id 123 (to confirm the id format)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# --- The per-location variant metafields (namespace "Channels"), and the
# --- custom_metadata field name we write each one to in Klaviyo. ---
LOCATION_METAFIELDS = [
    ("bicivictoria_inventory", "victoria_inventory"),
    ("bicilangford_inventory", "langford_inventory"),
    ("biciadanac_inventory", "adanac_inventory"),
    ("virtualwarehouse_inventory", "virtualwarehouse_inventory"),
]
METAFIELD_NAMESPACE = "Channels"

SHOPIFY_API_VERSION = "2024-10"
KLAVIYO_REVISION = "2024-10-15"

# Shopify caps a single query's cost at 1000; 50 products x 100 variants with a
# few metafield objects each stays comfortably under that (measured ~600).
PRODUCTS_PAGE_SIZE = 50
KLAVIYO_BATCH_SIZE = 100  # max items per bulk-update job


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #
def load_dotenv() -> None:
    """Minimal .env loader (no dependency). Real env vars always win, so this
    only fills in what isn't already set — matches how the GitHub Action runs
    (secrets come from the environment, not a file)."""
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
        sys.exit(f"ERROR: missing required environment variable {name}")
    return val


def store_handle(raw: str) -> str:
    return raw.strip().lower().removesuffix(".myshopify.com")


# --------------------------------------------------------------------------- #
# Shopify
# --------------------------------------------------------------------------- #
PRODUCTS_QUERY = """
query($cursor: String, $pageSize: Int!) {
  products(first: $pageSize, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      variants(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          victoria:        metafield(namespace: "Channels", key: "bicivictoria_inventory") { value }
          langford:        metafield(namespace: "Channels", key: "bicilangford_inventory") { value }
          adanac:          metafield(namespace: "Channels", key: "biciadanac_inventory") { value }
          virtualwarehouse: metafield(namespace: "Channels", key: "virtualwarehouse_inventory") { value }
        }
      }
    }
  }
}
"""

# GraphQL alias -> the custom_metadata field we store the summed value under.
_ALIAS_TO_FIELD = {
    "victoria": "victoria_inventory",
    "langford": "langford_inventory",
    "adanac": "adanac_inventory",
    "virtualwarehouse": "virtualwarehouse_inventory",
}


def _to_int(metafield: dict | None) -> int:
    """A null metafield, or a non-numeric value, counts as 0 (matches the
    theme's `| plus: 0` coercion)."""
    if not metafield:
        return 0
    try:
        return int(float(metafield.get("value") or 0))
    except (TypeError, ValueError):
        return 0


def _product_numeric_id(gid: str) -> str:
    """gid://shopify/Product/123 -> "123"."""
    return gid.rsplit("/", 1)[-1]


def shopify_graphql(store: str, token: str, query: str, variables: dict) -> dict:
    url = f"https://{store}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    for attempt in range(5):
        resp = requests.post(url, headers=headers,
                             json={"query": query, "variables": variables}, timeout=60)
        if resp.status_code == 429:  # throttled — back off and retry
            time.sleep(2 * (attempt + 1))
            continue
        resp.raise_for_status()
        body = resp.json()
        if "errors" in body:
            # Throttled errors come back 200 with an errors array.
            if any("THROTTLED" in str(e.get("extensions", {}).get("code", ""))
                   for e in body["errors"]):
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Shopify GraphQL errors: {body['errors']}")
        return body
    raise RuntimeError("Shopify GraphQL: exhausted retries (throttled)")


def iter_product_inventory(store: str, token: str, limit: int | None):
    """Yield {id, title, custom_metadata} for each active product, where
    custom_metadata holds the per-location sums across all variants."""
    cursor = None
    seen = 0
    truncated_variants = 0
    while True:
        body = shopify_graphql(store, token, PRODUCTS_QUERY,
                               {"cursor": cursor, "pageSize": PRODUCTS_PAGE_SIZE})
        conn = body["data"]["products"]
        cost = body.get("extensions", {}).get("cost", {})
        throttle = cost.get("throttleStatus", {})

        for node in conn["nodes"]:
            totals = {field: 0 for field in _ALIAS_TO_FIELD.values()}
            variants = node["variants"]
            for v in variants["nodes"]:
                for alias, field in _ALIAS_TO_FIELD.items():
                    totals[field] += _to_int(v.get(alias))
            if variants["pageInfo"]["hasNextPage"]:
                # >100 variants: rare, but flag it so totals aren't silently low.
                truncated_variants += 1
            yield {
                "id": _product_numeric_id(node["id"]),
                "title": node["title"],
                "custom_metadata": totals,
            }
            seen += 1
            if limit and seen >= limit:
                if truncated_variants:
                    print(f"WARNING: {truncated_variants} product(s) had >100 "
                          f"variants; their totals may be undercounted.")
                return

        # Be polite to the cost budget: if we're running low, let it restore.
        available = throttle.get("currentlyAvailable")
        restore = throttle.get("restoreRate", 1000)
        if available is not None and available < 2000 and restore:
            time.sleep(min(5, (2000 - available) / restore))

        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]

    if truncated_variants:
        print(f"WARNING: {truncated_variants} product(s) had >100 variants; "
              f"their totals may be undercounted.")


# --------------------------------------------------------------------------- #
# Klaviyo
# --------------------------------------------------------------------------- #
def klaviyo_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Klaviyo-API-Key {api_key}",
        "revision": KLAVIYO_REVISION,
        "accept": "application/json",
        "content-type": "application/json",
    }


def klaviyo_item_id(shopify_product_id: str) -> str:
    """Catalog item id for a Shopify-integrated product. Confirm against your
    account with `--discover <product_id>` before trusting it in bulk."""
    return f"$shopify:::$default:::{shopify_product_id}"


def klaviyo_discover(api_key: str, shopify_product_id: str) -> None:
    """Print the catalog item Klaviyo holds for a given Shopify product id, so
    we can confirm the exact item-id format for this account."""
    item_id = klaviyo_item_id(shopify_product_id)
    url = f"https://a.klaviyo.com/api/catalog-items/{requests.utils.quote(item_id, safe='')}/"
    resp = requests.get(url, headers=klaviyo_headers(api_key), timeout=30)
    print(f"GET {item_id} -> HTTP {resp.status_code}")
    print(resp.text[:2000])
    if resp.status_code == 404:
        print("\n404 means this id format/product isn't in the catalog. Try the "
              "list endpoint to inspect a real id:\n"
              "  https://a.klaviyo.com/api/catalog-items/?page[size]=1")


def klaviyo_bulk_update(api_key: str, batch: list[dict]) -> tuple[bool, str]:
    """Push one batch (<=100) of {id, custom_metadata} via a bulk-update job.
    Returns (ok, detail)."""
    url = "https://a.klaviyo.com/api/catalog-item-bulk-update-jobs/"
    items = [
        {
            "type": "catalog-item",
            "id": klaviyo_item_id(p["id"]),
            "attributes": {"custom_metadata": p["custom_metadata"]},
        }
        for p in batch
    ]
    payload = {
        "data": {
            "type": "catalog-item-bulk-update-job",
            "attributes": {"items": {"data": items}},
        }
    }
    for attempt in range(5):
        resp = requests.post(url, headers=klaviyo_headers(api_key),
                             json=payload, timeout=60)
        if resp.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code in (200, 201, 202):
            return True, resp.json().get("data", {}).get("id", "")
        return False, f"HTTP {resp.status_code}: {resp.text[:500]}"
    return False, "exhausted retries (rate limited)"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="read + sum from Shopify but do NOT write to Klaviyo")
    parser.add_argument("--limit", type=int, default=None,
                        help="only process the first N products (for testing)")
    parser.add_argument("--discover", metavar="SHOPIFY_PRODUCT_ID",
                        help="print the Klaviyo catalog item for one product id, "
                             "to confirm the id format, then exit")
    args = parser.parse_args()

    load_dotenv()

    if args.discover:
        klaviyo_discover(require_env("KLAVIYO_API_KEY"), args.discover)
        return

    store = store_handle(require_env("SHOPIFY_STORE"))
    token = require_env("SHOPIFY_ADMIN_TOKEN")
    api_key = "" if args.dry_run else require_env("KLAVIYO_API_KEY")

    synced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = time.time()
    total = 0
    failures = 0
    batch: list[dict] = []

    print(f"{'DRY RUN: ' if args.dry_run else ''}Syncing Shopify -> Klaviyo "
          f"(store={store}, batch={KLAVIYO_BATCH_SIZE})")

    def flush(b: list[dict]) -> None:
        nonlocal failures
        if not b or args.dry_run:
            return
        ok, detail = klaviyo_bulk_update(api_key, b)
        if not ok:
            failures += 1
            print(f"  ! bulk update failed for {len(b)} items: {detail}")

    for product in iter_product_inventory(store, token, args.limit):
        product["custom_metadata"]["local_inventory_synced_at"] = synced_at
        total += 1
        if args.dry_run:
            cm = product["custom_metadata"]
            print(f"  {product['id']:>14}  {product['title'][:40]:40} "
                  f"V={cm['victoria_inventory']} L={cm['langford_inventory']} "
                  f"A={cm['adanac_inventory']} VW={cm['virtualwarehouse_inventory']}")
        else:
            batch.append(product)
            if len(batch) >= KLAVIYO_BATCH_SIZE:
                flush(batch)
                batch = []
    flush(batch)

    elapsed = time.time() - started
    print(f"\nDone: {total} products in {elapsed:.0f}s"
          + ("" if args.dry_run else f", {failures} batch failure(s)"))
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
