# bici-klaviyo-inventory-sync

Pushes **per-store inventory** from Shopify into the **Klaviyo product catalog**
so email flows (e.g. browse abandonment) can tell a customer an item is in stock
at their local Bici store.

## What it does

For every active Shopify product it sums the per-location stock across **all
variants** and writes these fields onto the product's Klaviyo catalog item as
`custom_metadata`:

| custom_metadata field          | source (variant metafield, namespace `Channels`) |
| ------------------------------ | ------------------------------------------------- |
| `victoria_inventory`           | `bicivictoria_inventory`                          |
| `langford_inventory`           | `bicilangford_inventory`                          |
| `adanac_inventory`             | `biciadanac_inventory`                            |
| `virtualwarehouse_inventory`   | `virtualwarehouse_inventory`                      |
| `local_inventory_synced_at`    | timestamp of the sync run (UTC ISO-8601)          |

It's a **full sweep**: every run refreshes every product.

## Use it in a Klaviyo flow

In a browse-abandonment email, look up the abandoned product and read the field:

```liquid
{% catalog event.ProductID %}
  {% if catalog_item.custom_metadata.langford_inventory > 0 %}
    Good news — it's in stock to see at our Langford store!
  {% endif %}
{% endcatalog %}
```

## Configuration

Set these as environment variables (local `.env`, or GitHub Actions secrets):

| Variable              | What                                                  |
| --------------------- | ----------------------------------------------------- |
| `SHOPIFY_STORE`       | myshopify subdomain, e.g. `la-bicicletta-vancouver`   |
| `SHOPIFY_ADMIN_TOKEN` | Shopify Admin token with `read_products`              |
| `KLAVIYO_API_KEY`     | Klaviyo **private** key with Catalogs full access     |

Copy `.env.example` to `.env` for local runs (`.env` is gitignored).

## Running

```bash
pip install -r requirements.txt

python sync.py --dry-run --limit 50   # read + print, write nothing (safe test)
python sync.py --discover 123456      # confirm the Klaviyo catalog item id format
python sync.py                        # full sweep: read Shopify, push to Klaviyo
```

## Scheduling

Runs hourly via GitHub Actions (`.github/workflows/sync.yml`) — the repo is
public, so Actions minutes are unlimited/free. Add `SHOPIFY_STORE`,
`SHOPIFY_ADMIN_TOKEN`, and `KLAVIYO_API_KEY` under
**Settings → Secrets and variables → Actions**. You can also trigger a run
manually from the **Actions** tab ("Run workflow").

No secrets live in the code — only in `.env` (gitignored) and Actions secrets.
