"""MCP tools. There is deliberately no tool that places an order: checkout_request opens an
approval page for the human, and only their click there places it."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from . import amazon
from .approval import Approval, Desk
from .browser import BrowserError, Session

INSTRUCTIONS = """Shop the user's own Amazon account through a dedicated browser window on their machine.
Read orders, return deadlines, products and the cart freely. To buy, put items in the cart and call
checkout_request: it opens an approval page in the user's browser and only the user's click there places
the order. Never claim an order was placed until checkout_status says 'placed'."""

mcp = MCPServer("cartclaw", instructions=INSTRUCTIONS)
session = Session()
_desk: Desk | None = None

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
CART = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)


async def _run(coro):
    try:
        return await coro
    except (amazon.AmazonError, BrowserError) as e:
        raise ToolError(str(e)) from e


def _desk_for_loop() -> Desk:
    global _desk
    if _desk is None:

        async def place(a: Approval) -> str | None:
            return await amazon.place_order(session, a.summary)

        async def change_card(a: Approval, last4: str):
            return await amazon.checkout_preview(session, card=last4)

        _desk = Desk(place, asyncio.get_running_loop(), change_card=change_card)
    return _desk


def _approval_view(a: Approval) -> dict[str, Any]:
    return {
        "approval_id": a.id,
        "status": a.status,
        "detail": a.detail,
        "order_number": a.order_number,
        "total": a.summary.total,
        "items": a.summary.items,
        "paying_with": a.summary.paying_with,
    }


@mcp.tool(annotations=READ)
async def orders(period: str = "months-3", max_pages: int = 3) -> dict[str, Any]:
    """Order history with each item's return window. period: 'last30', 'months-3' or 'year-YYYY'.
    Amazon shows 10 orders per page; max_pages caps how many pages are read."""
    found = await _run(amazon.orders(session, period, max_pages))
    return {"period": period, "orders": [asdict(o) for o in found]}


@mcp.tool(annotations=READ)
async def returnable_items() -> dict[str, Any]:
    """Items from the last 3 months whose return window is still open, soonest deadline first."""
    return {"items": [asdict(i) for i in await _run(amazon.returnable_items(session))]}


@mcp.tool(annotations=READ)
async def product(asin: str) -> dict[str, Any]:
    """Title, price and availability for one product by ASIN (e.g. from orders)."""
    return asdict(await _run(amazon.product(session, asin)))


@mcp.tool(annotations=READ)
async def cart() -> dict[str, Any]:
    """What is in the cart now, with the subtotal."""
    return asdict(await _run(amazon.cart(session)))


@mcp.tool(annotations=CART)
async def cart_add(asin: str, quantity: int = 1) -> dict[str, Any]:
    """Add a product to the cart (one-time purchase, not Subscribe & Save). Returns the cart."""
    return asdict(await _run(amazon.cart_add(session, asin, quantity)))


@mcp.tool(annotations=CART)
async def cart_remove(asin: str) -> dict[str, Any]:
    """Remove a product from the cart. Returns the cart."""
    return asdict(await _run(amazon.cart_remove(session, asin)))


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, open_world_hint=True
    )
)
async def checkout_request() -> dict[str, Any]:
    """Ask the user to approve buying the current cart. Opens Amazon's checkout review (nothing is
    placed), then an approval page in the user's browser showing total, items, address and card.
    Returns at once with status 'pending'; poll checkout_status. The user can approve or decline."""
    summary, shot, cards = await _run(amazon.checkout_preview(session))
    a = _desk_for_loop().create(summary, shot, cards)
    view = _approval_view(a)
    view["next"] = (
        "Tell the user an approval page opened in their browser for "
        f"${summary.total:,.2f}, then poll checkout_status."
    )
    return view


@mcp.tool(annotations=READ)
async def checkout_status(approval_id: str) -> dict[str, Any]:
    """Where an approval stands: pending, switching (the user is changing the card), placing,
    placed (with order_number), rejected, expired, superseded or failed (with detail)."""
    a = _desk.get(approval_id) if _desk else None
    if not a:
        raise ToolError(f"No approval {approval_id} in this session.")
    return _approval_view(a)
