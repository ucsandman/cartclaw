# CartClaw

Let your AI agent use your own Amazon account: read orders and return deadlines, look up products, fill the cart. It can only buy after you click **Place order** on an approval page. [cartclaw.dev](https://cartclaw.dev)

Not affiliated with or endorsed by Amazon. Amazon is a trademark of Amazon.com, Inc.

Everything runs on your machine. The agent drives a separate browser window with its own profile, signed in to your account once. No Amazon password or session leaves your computer, and your everyday browser is never touched.

## What the agent can do

| Tool | Does |
|---|---|
| `orders` | Order history (`last30`, `months-3`, `year-YYYY`) with each item's return window |
| `returnable_items` | Items you can still return, soonest deadline first |
| `product` | Title, price and availability for an ASIN |
| `cart` | What is in the cart |
| `cart_add` / `cart_remove` | Change the cart |
| `checkout_request` | Ask you to approve buying the cart |
| `checkout_status` | Whether you approved and the order went through |

There is no tool that places an order.

## How buying works

1. The agent fills the cart and calls `checkout_request`.
2. The server opens Amazon's checkout review page in its window and reads the total, items, address, card and delivery date. Nothing is placed.
3. An approval page opens in your normal browser with those details, a screenshot of Amazon's page, and two buttons: **Place order** and **Don't buy**. If the card is wrong, pick another saved card and click **Use this card**: the server switches it on Amazon's checkout and the page shows the new total.
4. If you click **Place order**, the server reloads checkout, switches back to the card you approved (Amazon puts its default card back whenever checkout reopens), checks that the total, items, address and card still match what you saw, and only then clicks Amazon's "Place your order". If anything changed, nothing is ordered.
5. The request expires after 15 minutes. A newer request replaces an older one.

## Setup

Needs [uv](https://docs.astral.sh/uv/), git, and Brave, Chrome, Edge or Chromium.

1. Sign in to Amazon once. A window opens; tick **Keep me signed in**:
   `uvx --from git+https://github.com/ucsandman/cartclaw cartclaw login`
2. Add it to Claude Code:
   `claude mcp add --scope user cartclaw -- uvx --from git+https://github.com/ucsandman/cartclaw cartclaw`

Other MCP clients can run the same `uvx` command as a local stdio server.

The browser window starts by itself when a tool needs it and stays open between sessions. You can minimize it.

Optional settings (environment variables): `CARTCLAW_BROWSER` (browser path), `CARTCLAW_PROFILE` (profile folder, default `~/.cartclaw/profile`), `CARTCLAW_CDP_PORT` (default `9333`).

## Development

`git clone https://github.com/ucsandman/cartclaw && cd cartclaw && uv sync`. The marketing site at [cartclaw.dev](https://cartclaw.dev) is static HTML plus one Vercel function in `site/`.

Site operations (2026-10-02):
- A push to `main` deploys the site. The Vercel project's root directory is `site`.
- Google Search Console and Bing verify through the `google-site-verification` and `msvalidate.01` meta tags in `site/index.html`. Removing either tag revokes that verification.
- Vercel Web Analytics loads from `/_vercel/insights/script.js` on every page.
- Pro waitlist signups are stored as private files in the Vercel Blob store `cartclaw-waitlist`.

- `uv run pytest`: parsers against saved Amazon pages (personal details scrubbed), the approval page, and the tool list. No network.
- `uv run python scripts/live_smoke.py`: read-only check against your real account over the MCP protocol.

## Limits

- amazon.com (US) only.
- Returns: return deadlines are read, but starting a return is not built yet.
- The approval page lists saved cards only. To pay with a bank account or gift card balance, select it in the Amazon window first.
- The approval gate stops the agent from buying through this server. It is not a sandbox: an agent with shell access to your machine could drive the browser's control port directly.
- Automating your account may break Amazon's Conditions of Use. You are the one accessing your account, but Amazon can still act on it.
- Amazon changes its pages. When a parser breaks, the tool says what it could not read instead of guessing.

Order history parsing uses [amazon-orders](https://github.com/alexdlaird/amazon-orders) (MIT).
