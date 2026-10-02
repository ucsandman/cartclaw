"""The human gate. A local page shows Amazon's checkout summary with two buttons; only the
Place order button on that page can make the server click Amazon's 'Place your order'.
The page URL carries a one-time nonce and is opened in the user's own browser, never
returned to the agent."""

from __future__ import annotations

import asyncio
import html
import json
import secrets
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from . import parse
from .parse import CheckoutSummary, PaymentOption

TTL_SECONDS = 15 * 60


@dataclass
class Approval:
    id: str
    nonce: str
    summary: CheckoutSummary
    screenshot: bytes | None
    created: float
    status: str = "pending"  # pending, switching, placing, placed, rejected, expired, superseded, failed
    detail: str = ""
    order_number: str | None = None
    cards: list[PaymentOption] = field(default_factory=list)
    note: str = ""  # shown on the page after a card switch


PlaceFn = Callable[[Approval], Awaitable[str | None]]
# Switch the order to the card with these last 4 digits and read checkout again.
ChangeCardFn = Callable[
    [Approval, str],
    Awaitable[tuple[CheckoutSummary, bytes | None, list[PaymentOption]]],
]


class Desk:
    def __init__(
        self,
        place: PlaceFn,
        loop: asyncio.AbstractEventLoop,
        ttl: float = TTL_SECONDS,
        open_page: Callable[[str], object] = webbrowser.open,
        change_card: ChangeCardFn | None = None,
    ) -> None:
        self._place, self._loop, self._ttl, self._open = place, loop, ttl, open_page
        self._change_card = change_card
        self._approvals: dict[str, Approval] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self.base = ""

    def start(self) -> None:
        if self._server:
            return
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None

    def url(self, a: Approval) -> str:
        return f"{self.base}/a/{a.id}?n={a.nonce}"

    def create(
        self,
        summary: CheckoutSummary,
        screenshot: bytes | None,
        cards: list[PaymentOption] | None = None,
    ) -> Approval:
        self.start()
        a = Approval(
            secrets.token_urlsafe(9),
            secrets.token_urlsafe(24),
            summary,
            screenshot,
            time.time(),
            cards=cards or [],
        )
        with self._lock:
            # One cart, so only the newest request can be approved.
            for old in self._approvals.values():
                if old.status in ("pending", "switching"):
                    old.status = "superseded"
            self._approvals[a.id] = a
        print(f"cartclaw: approval page {self.url(a)}", file=sys.stderr)
        self._open(self.url(a))
        return a

    def get(self, approval_id: str) -> Approval | None:
        with self._lock:
            a = self._approvals.get(approval_id)
            if a and a.status == "pending" and time.time() - a.created > self._ttl:
                a.status = "expired"
            return a

    def decide(
        self, approval_id: str, nonce: str, decision: str, card: str = ""
    ) -> tuple[int, str]:
        a = self.get(approval_id)
        if not a or not secrets.compare_digest(a.nonce, nonce):
            return 403, "This approval link is not valid."
        # The card dropdown belongs to this form. Place order with a different card picked
        # switches the card and shows the order again; it never buys on the card shown before.
        if decision in ("card", "approve") and card:
            if card != parse.card_last4(a.summary.paying_with):
                return self.switch_card(approval_id, nonce, card)
        if decision == "card":
            return 200, "unchanged"
        with self._lock:
            if a.status != "pending":
                return 409, f"This request is already {a.status}."
            if decision == "reject":
                a.status = "rejected"
                return 200, "rejected"
            if decision != "approve":
                return 400, "Unknown decision."
            a.status = "placing"
        asyncio.run_coroutine_threadsafe(self._run_place(a), self._loop)
        return 200, "placing"

    def switch_card(self, approval_id: str, nonce: str, last4: str) -> tuple[int, str]:
        a = self.get(approval_id)
        if not a or not secrets.compare_digest(a.nonce, nonce):
            return 403, "This approval link is not valid."
        with self._lock:
            if a.status != "pending":
                return 409, f"This request is already {a.status}."
            if not self._change_card or last4 not in {c.last4 for c in a.cards}:
                return 400, "That card is not one of your saved cards."
            a.status, a.detail, a.note = "switching", "", ""
        asyncio.run_coroutine_threadsafe(self._run_switch(a, last4), self._loop)
        return 200, "switching"

    async def _run_switch(self, a: Approval, last4: str) -> None:
        result, detail = None, ""
        try:
            result = await self._change_card(a, last4)
        except Exception as e:  # shown on the page; the order keeps the card it had
            detail = f"Card not changed: {str(e) or type(e).__name__}"
        with self._lock:
            if a.status != "switching":  # a newer request superseded it
                return
            if result:
                a.summary, a.screenshot, a.cards = result
                label = next((c.label for c in a.cards if c.last4 == last4), last4)
                a.note = (
                    f"Card switched to {label}. Nothing is ordered yet: "
                    "check the total, then click Place order."
                )
            a.status, a.detail, a.created = "pending", detail, time.time()

    async def _run_place(self, a: Approval) -> None:
        try:
            a.order_number = await self._place(a)
            a.status = "placed"
        except Exception as e:  # the message is shown to the human and the agent
            a.status, a.detail = "failed", str(e) or type(e).__name__


def _handler(desk: Desk) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(
            self,
            fmt: str,
            *args: object,
        ) -> None:  # stdout belongs to MCP; keep quiet
            pass

        def _send(
            self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _route(self) -> tuple[Approval | None, str, dict[str, list[str]]]:
            u = urlparse(self.path)
            parts = u.path.strip("/").split("/")
            query = parse_qs(u.query)
            if len(parts) < 2 or parts[0] != "a":
                return None, "", query
            a = desk.get(parts[1])
            nonce = (query.get("n") or [""])[0]
            if not a or not secrets.compare_digest(a.nonce, nonce):
                return None, "", query
            return a, parts[2] if len(parts) > 2 else "", query

        def do_GET(self) -> None:
            a, sub, _ = self._route()
            if not a:
                return self._send(
                    403,
                    _page("Link not valid", "<p>This approval link is not valid.</p>"),
                )
            if sub == "shot.png":
                if not a.screenshot:
                    return self._send(404, b"", "image/png")
                return self._send(200, a.screenshot, "image/png")
            if sub == "status":
                body = json.dumps(
                    {
                        "status": a.status,
                        "detail": a.detail,
                        "order_number": a.order_number,
                    }
                )
                return self._send(200, body.encode(), "application/json")
            self._send(200, render(a, desk.url(a)))

        def do_POST(self) -> None:
            origin = self.headers.get("Origin")
            if origin and origin != desk.base:
                return self._send(
                    403,
                    _page("Blocked", "<p>Request from another site was blocked.</p>"),
                )
            parts = urlparse(self.path).path.strip("/").split("/")
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode())
            if len(parts) != 3 or parts[0] != "a" or parts[2] != "decide":
                return self._send(404, _page("Not found", "<p>Not found.</p>"))
            nonce = (form.get("n") or [""])[0]
            code, msg = desk.decide(
                parts[1],
                nonce,
                (form.get("decision") or [""])[0],
                (form.get("card") or [""])[0],
            )
            if code != 200:
                return self._send(code, _page("Not done", f"<p>{html.escape(msg)}</p>"))
            self.send_response(303)
            self.send_header("Location", f"/a/{parts[1]}?n={nonce}")
            self.end_headers()

    return Handler


_CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1b1b1a;--muted:#6b6a66;--line:#e4e2dc;--go:#14532d;--go-ink:#fff;--warn:#8a3b12}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#f1efe9;--muted:#a3a19a;--line:#33322f;--go:#3f9d62;--go-ink:#0d0d0c;--warn:#f0a274}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:560px;margin:0 auto;padding:32px 16px 64px}
.tag{font-size:13px;color:var(--muted);letter-spacing:.02em}
h1{font-size:26px;line-height:1.2;margin:6px 0 4px}
.lede{color:var(--muted);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px}
.total{font-size:40px;font-weight:700;letter-spacing:-.02em;margin:0}
.items{margin:14px 0 0;padding:0;list-style:none}.items li{padding:8px 0;border-top:1px solid var(--line)}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:15px}td{padding:4px 0}td:last-child{text-align:right;font-variant-numeric:tabular-nums}
tr.grand td{font-weight:700;border-top:1px solid var(--line);padding-top:8px}
dl{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;margin:16px 0 0;font-size:15px}dt{color:var(--muted)}dd{margin:0}
.actions{display:flex;gap:12px;margin-top:22px;flex-wrap:wrap}
button{font:inherit;font-weight:600;border-radius:10px;padding:12px 18px;cursor:pointer;border:1px solid var(--line);background:var(--card);color:var(--ink)}
button.go{background:var(--go);color:var(--go-ink);border-color:var(--go);flex:1}
.note{font-size:13px;color:var(--muted);margin-top:14px}
.pick{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}.pick select{font:inherit;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);flex:1 1 180px;min-width:0}.pick button{padding:8px 12px}
.state{font-size:20px;font-weight:700;margin:0 0 6px}.bad{color:var(--warn)}
details{margin-top:18px}summary{cursor:pointer;color:var(--muted);font-size:14px}details img{width:100%;margin-top:10px;border:1px solid var(--line);border-radius:8px}
"""


def _page(title: str, body: str, refresh: bool = False) -> bytes:
    meta = '<meta http-equiv="refresh" content="2">' if refresh else ""
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>"
    ).encode()


def _money(v: float | None) -> str:
    return "?" if v is None else f"${v:,.2f}"


def render(a: Approval, url: str) -> bytes:
    s, e = a.summary, html.escape
    items = "".join(f"<li>{e(t)}</li>" for t in s.items)
    rows = "".join(
        f'<tr class="{"grand" if k.lower().startswith("order total") else ""}"><td>{e(k.rstrip(":"))}</td>'
        f"<td>{_money(v)}</td></tr>"
        for k, v in s.lines.items()
    )
    pick = ""
    if a.status == "pending" and len(a.cards) > 1:
        current = parse.card_last4(s.paying_with)
        options = "".join(
            f'<option value="{e(c.last4)}"{" selected" if c.last4 == current else ""}>{e(c.label)}</option>'
            for c in a.cards
        )
        # form="decide": the picked card is sent with Place order too, see Desk.decide.
        pick = (
            f'<div class="pick"><select name="card" form="decide" aria-label="Card">{options}</select>'
            f'<button form="decide" name="decision" value="card">Use this card</button></div>'
        )
    facts = (
        f"<dl><dt>Deliver to</dt><dd>{e(s.deliver_to)}</dd><dt>Pay with</dt><dd>{e(s.paying_with)}{pick}</dd>"
        f"<dt>Delivery</dt><dd>{e(s.arriving)}</dd></dl>"
    )
    notice = (
        f'<p class="note bad">{e(a.detail)}</p>'
        if a.detail
        else f'<p class="note">{e(a.note)}</p>'
        if a.note
        else ""
    )
    shot = (
        f'<details><summary>Amazon\'s checkout page, as captured</summary><img src="{e(url.replace("?", "/shot.png?", 1))}" alt="Amazon checkout page screenshot"></details>'
        if a.screenshot
        else ""
    )
    head = '<div class="tag">CartClaw</div>'
    if a.status == "pending":
        minutes = max(1, int((a.created + TTL_SECONDS - time.time()) // 60))
        form = (
            f'<form id="decide" method="post" action="/a/{e(a.id)}/decide"><input type="hidden" name="n" value="{e(a.nonce)}">'
            f'<div class="actions"><button class="go" name="decision" value="approve">Place order &middot; {_money(s.total)}</button>'
            f'<button name="decision" value="reject">Don\'t buy</button></div></form>'
        )
        body = (
            f'{head}<h1>Approve this Amazon order?</h1><p class="lede">Your agent asked to buy this. '
            f'Nothing is ordered until you click Place order.</p><div class="card"><p class="total">{_money(s.total)}</p>'
            f'<ul class="items">{items}</ul><table>{rows}</table>{facts}{notice}{form}'
            f'<p class="note">This request expires in {minutes} min. The order is checked again right before it is placed; '
            f"if the total, items, address or card changed, nothing is ordered.</p></div>{shot}"
        )
        return _page(f"Approve {_money(s.total)} order", body)
    states = {
        "switching": (
            "Switching card…",
            "Amazon is changing the card on this order. This page updates by itself.",
            False,
        ),
        "placing": (
            "Placing your order…",
            "Keep this page open; it updates by itself.",
            False,
        ),
        "placed": (
            "Order placed",
            f"Amazon order {e(a.order_number)}."
            if a.order_number
            else "Amazon confirmed the order. It will show in Your Orders.",
            False,
        ),
        "rejected": ("Not ordered", "You chose Don't buy.", False),
        "expired": (
            "Not ordered",
            "This request expired. Ask your agent again if you still want it.",
            True,
        ),
        "superseded": (
            "Not ordered",
            "Your agent sent a newer request; use that page instead.",
            True,
        ),
        "failed": ("Not ordered", e(a.detail), True),
    }
    title, text, bad = states[a.status]
    body = (
        f'{head}<p class="state{" bad" if bad else ""}">{title}</p><p class="lede">{text}</p>'
        f'<div class="card"><p class="total">{_money(s.total)}</p><ul class="items">{items}</ul>{facts}</div>'
    )
    return _page(title, body, refresh=a.status in ("placing", "switching"))
