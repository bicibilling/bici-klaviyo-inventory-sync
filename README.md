# bici-klaviyo-inventory-sync

A tiny web service that tells a **Klaviyo web feed** how much of a product is in
stock at each Bici store, so browse-abandonment (and other) emails can say
"in stock to see at your local store."

## How it fits together

```
Customer views a product
        │  Klaviyo records a "Viewed Product" event (ProductID = Shopify product id)
        ▼
Browse-abandonment email is built (at send time)
        │  Klaviyo fetches the web feed:
        │      GET /inventory?product_id={{ event.ProductID }}&token=<FEED_TOKEN>
        ▼
This service sums the per-store variant metafields in Shopify and returns JSON
        ▼
Email renders conditional content from {{ feeds.bici_inventory.* }}
```

## The endpoint

```
GET /inventory?product_id=8073171337279&token=<FEED_TOKEN>
```

```json
{
  "product_id": "8073171337279",
  "title": "Rapha Women's Pro Team Training Jersey",
  "found": true,
  "victoria": 5,
  "langford": 0,
  "adanac": 13,
  "virtualwarehouse": 0,
  "in_stock_anywhere": true
}
```

It sums `Channels.bicivictoria_inventory` / `bicilangford_inventory` /
`biciadanac_inventory` / `virtualwarehouse_inventory` across **all variants** of
the product. An unknown product or a Shopify hiccup returns all-zeros with
`found: false` and HTTP 200, so the email still renders (the safe "not in stock"
branch) instead of breaking. Results are cached in memory for 5 minutes.

## Use it in a Klaviyo flow

1. **Create the web feed** — Klaviyo → Settings → Web Feeds (or the flow email's
   feed config). Name it `bici_inventory`, method GET, URL:
   `https://<your-render-url>/inventory?product_id={{ event.ProductID }}&token=<FEED_TOKEN>`
2. **Use it in the email** with conditional content:
   ```liquid
   {% if feeds.bici_inventory.in_stock_anywhere %}
     Good news — it's in stock to see in person! In stock at:
     {% if feeds.bici_inventory.victoria > 0 %}Victoria{% endif %}
     {% if feeds.bici_inventory.langford > 0 %}Langford{% endif %}
     {% if feeds.bici_inventory.adanac > 0 %}Vancouver (Adanac){% endif %}
   {% else %}
     Want it shipped? Order online and we'll send it your way.
   {% endif %}
   ```

## Configuration

| Variable              | What                                                |
| --------------------- | --------------------------------------------------- |
| `SHOPIFY_STORE`       | myshopify subdomain, e.g. `la-bicicletta-vancouver` |
| `SHOPIFY_ADMIN_TOKEN` | Shopify Admin token with `read_products`            |
| `FEED_TOKEN`          | shared secret added to the feed URL (optional)      |

The service does **not** need a Klaviyo key — Klaviyo calls it, not vice-versa.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in the values
uvicorn app:app --reload
curl "http://localhost:8000/inventory?product_id=8073171337279"
```

## Deploy (Render)

Blueprint deploy from `render.yaml` (a `web` service). Set `SHOPIFY_STORE`,
`SHOPIFY_ADMIN_TOKEN`, and `FEED_TOKEN` in the dashboard.

Free Render web services sleep after ~15 min idle. The
`.github/workflows/keep-alive.yml` Action pings the health URL every ~14 min to
keep it warm so the feed doesn't cold-start mid-send — set the repo **variable**
`FEED_HEALTH_URL` to the deployed root URL. Move to a paid plan and delete that
workflow if you'd rather not rely on the pinger.
