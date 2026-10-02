import asyncio
import urllib.error
from dataclasses import replace
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from cartclaw import parse
from cartclaw.approval import Desk

SUMMARY = parse.parse_checkout(
    (Path(__file__).parent / "fixtures" / "checkout.html").read_text(encoding="utf-8")
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(
    url: str, data: dict | None = None, origin: str | None = None
) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body)
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=5) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


@pytest.fixture
async def desk_and_calls():
    calls = []
    opened = []

    async def place(a):
        calls.append(a.id)
        if a.summary.total == -1:
            raise RuntimeError("Not ordered: order total changed")
        return "113-0000000-0000000"

    desk = Desk(place, asyncio.get_running_loop(), open_page=opened.append)
    yield desk, calls, opened
    desk.close()


async def post(desk, a, decision, nonce=None, origin=None):
    return await asyncio.to_thread(
        fetch,
        f"{desk.base}/a/{a.id}/decide",
        {"n": nonce or a.nonce, "decision": decision},
        origin or desk.base,
    )


async def settle(desk, a):
    for _ in range(50):
        if desk.get(a.id).status != "placing":
            return
        await asyncio.sleep(0.02)


async def test_page_opens_in_browser_and_shows_summary(desk_and_calls):
    desk, _, opened = desk_and_calls
    a = desk.create(SUMMARY, b"png")
    assert opened == [desk.url(a)]
    code, page = await asyncio.to_thread(fetch, desk.url(a))
    assert code == 200
    assert "$16.07" in page and "Place order" in page and "Don't buy" in page
    assert "Keurig" in page and "Visa 0000" in page


async def test_approval_works_without_screenshot(desk_and_calls):
    desk, _, _ = desk_and_calls
    a = desk.create(SUMMARY, None)
    code, page = await asyncio.to_thread(fetch, desk.url(a))
    assert code == 200 and "Place order" in page and "shot.png" not in page
    shot = desk.url(a).replace("?", "/shot.png?", 1)
    assert (await asyncio.to_thread(fetch, shot))[0] == 404


CARDS = [
    parse.PaymentOption("Visa ending in 0000", "0000", False),
    parse.PaymentOption("Discover ending in 1111", "1111", False),
    parse.PaymentOption("MasterCard ending in 2222", "2222", False),
]


@pytest.fixture
async def card_desk():
    placed, switched = [], []

    async def place(a):
        placed.append(a.summary.paying_with)
        return "113-0000000-0000000"

    async def change_card(a, last4):
        switched.append(last4)
        if last4 == "2222":
            raise RuntimeError("Amazon wants you to confirm the card")
        return replace(SUMMARY, paying_with=f"Discover {last4}"), None, CARDS

    desk = Desk(
        place,
        asyncio.get_running_loop(),
        open_page=lambda u: None,
        change_card=change_card,
    )
    yield desk, placed, switched
    desk.close()


async def pick(desk, a, card, decision="card"):
    code, _ = await asyncio.to_thread(
        fetch,
        f"{desk.base}/a/{a.id}/decide",
        {"n": a.nonce, "card": card, "decision": decision},
        desk.base,
    )
    for _ in range(50):
        if desk.get(a.id).status != "switching":
            break
        await asyncio.sleep(0.02)
    return code


async def test_card_picker_switches_card_then_places_on_it(card_desk):
    desk, placed, switched = card_desk
    a = desk.create(SUMMARY, b"", CARDS)
    _, page = await asyncio.to_thread(fetch, desk.url(a))
    assert 'value="0000" selected' in page and "Use this card" in page
    assert await pick(desk, a, "1111") == 303
    assert switched == ["1111"] and a.status == "pending"
    assert a.summary.paying_with == "Discover 1111"
    await pick(
        desk, a, "1111", decision="approve"
    )  # the browser sends the dropdown too
    await settle(desk, a)
    assert placed == ["Discover 1111"]


async def test_place_order_with_another_card_picked_switches_and_does_not_buy(
    card_desk,
):
    # The card dropdown sits next to Place order; clicking Place order after picking a
    # different card must never charge the card the page was showing.
    desk, placed, switched = card_desk
    a = desk.create(SUMMARY, b"", CARDS)
    assert await pick(desk, a, "1111", decision="approve") == 303
    assert placed == [] and switched == ["1111"] and a.status == "pending"
    _, page = await asyncio.to_thread(fetch, desk.url(a))
    assert "Card switched to Discover ending in 1111" in page
    assert "Nothing is ordered yet" in page


async def test_card_not_saved_is_refused(card_desk):
    desk, _, switched = card_desk
    a = desk.create(SUMMARY, b"", CARDS)
    assert await pick(desk, a, "9999") == 400
    assert switched == [] and a.status == "pending"


async def test_failed_card_switch_keeps_order_and_says_why(card_desk):
    desk, _, _ = card_desk
    a = desk.create(SUMMARY, b"", CARDS)
    assert await pick(desk, a, "2222") == 303
    assert a.status == "pending" and a.summary.paying_with == "Visa 0000"
    _, page = await asyncio.to_thread(fetch, desk.url(a))
    assert "Card not changed: Amazon wants you to confirm the card" in page


async def test_amazon_text_is_escaped(desk_and_calls):
    desk, _, _ = desk_and_calls
    evil = parse.CheckoutSummary(
        ["<script>alert(1)</script>"], {"Order total:": 1.0}, 1.0, "x", "y", "z", True
    )
    a = desk.create(evil, b"")
    _, page = await asyncio.to_thread(fetch, desk.url(a))
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page


async def test_wrong_nonce_is_refused(desk_and_calls):
    desk, calls, _ = desk_and_calls
    a = desk.create(SUMMARY, b"")
    code, _ = await asyncio.to_thread(fetch, f"{desk.base}/a/{a.id}?n=wrong")
    assert code == 403
    code, _ = await post(desk, a, "approve", nonce="wrong")
    assert code == 403 and calls == [] and desk.get(a.id).status == "pending"


async def test_cross_site_post_is_refused(desk_and_calls):
    desk, calls, _ = desk_and_calls
    a = desk.create(SUMMARY, b"")
    code, _ = await post(desk, a, "approve", origin="https://evil.example")
    assert code == 403 and calls == []


async def test_approve_places_once(desk_and_calls):
    desk, calls, _ = desk_and_calls
    a = desk.create(SUMMARY, b"")
    code, _ = await post(desk, a, "approve")
    assert code == 303
    await settle(desk, a)
    assert (
        desk.get(a.id).status == "placed"
        and desk.get(a.id).order_number == "113-0000000-0000000"
    )
    code, _ = await post(desk, a, "approve")
    assert code == 409 and calls == [a.id]
    _, page = await asyncio.to_thread(fetch, desk.url(a))
    assert "Order placed" in page and "113-0000000-0000000" in page


async def test_reject_never_places(desk_and_calls):
    desk, calls, _ = desk_and_calls
    a = desk.create(SUMMARY, b"")
    assert (await post(desk, a, "reject"))[0] == 303
    assert desk.get(a.id).status == "rejected"
    assert (await post(desk, a, "approve"))[0] == 409
    assert calls == []


async def test_expired_cannot_be_approved(desk_and_calls):
    desk, calls, _ = desk_and_calls
    desk._ttl = 0
    a = desk.create(SUMMARY, b"")
    await asyncio.sleep(0.01)
    assert (await post(desk, a, "approve"))[0] == 409
    assert desk.get(a.id).status == "expired" and calls == []


async def test_newer_request_supersedes_older(desk_and_calls):
    desk, calls, _ = desk_and_calls
    old = desk.create(SUMMARY, b"")
    new = desk.create(SUMMARY, b"")
    assert desk.get(old.id).status == "superseded"
    assert (await post(desk, old, "approve"))[0] == 409
    assert desk.get(new.id).status == "pending" and calls == []


async def test_place_failure_is_reported(desk_and_calls):
    desk, _, _ = desk_and_calls
    bad = parse.CheckoutSummary(["x"], {}, -1, "a", "b", "c", True)
    a = desk.create(bad, b"")
    await post(desk, a, "approve")
    await settle(desk, a)
    got = desk.get(a.id)
    assert got.status == "failed" and "total changed" in got.detail
