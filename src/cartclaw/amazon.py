"""Amazon actions over the dedicated browser. Every action reads the real page; nothing is cached."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from amazonorders.exception import AmazonOrdersError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from . import parse
from .browser import Session

BASE = "https://www.amazon.com"
CART_URL = f"{BASE}/gp/cart/view.html"
CHECKOUT_URL = f"{BASE}/checkout/entry/cart?proceedToCheckout=1"
_ASIN = re.compile(r"^[A-Z0-9]{10}$")
_PERIOD = re.compile(r"^(last30|months-3|year-\d{4})$")


class AmazonError(Exception):
    """A failure with a message written for the person at the keyboard."""


async def _open(page: Page, url: str) -> str:
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(1_200)
    return await _check_blocked(page)


async def _check_blocked(page: Page) -> str:
    if parse.is_signin(page.url):
        await page.bring_to_front()
        raise AmazonError(
            "Amazon wants you to sign in. The Amazon window is on its sign-in page: "
            "sign in there (tick 'Keep me signed in'), then retry."
        )
    html = await page.content()
    if parse.is_captcha(html):
        await page.bring_to_front()
        raise AmazonError(
            "Amazon is showing a CAPTCHA in the Amazon window. Solve it there, then retry."
        )
    return html


def _check_asin(asin: str) -> str:
    asin = asin.strip().upper()
    if not _ASIN.match(asin):
        raise AmazonError(
            f"'{asin}' is not an ASIN (10 letters or digits, e.g. B07V3946XL)."
        )
    return asin


async def orders(
    session: Session, period: str = "months-3", max_pages: int = 3
) -> list[parse.Order]:
    if not _PERIOD.match(period):
        raise AmazonError("period must be 'last30', 'months-3' or 'year-YYYY'.")
    async with session.lock:
        page = await session.page()
        url: str | None = f"{BASE}/your-orders/orders?timeFilter={period}"
        found: list[parse.Order] = []
        for _ in range(max(1, max_pages)):
            html = await _open(page, url)
            try:
                found += parse.parse_orders(html, date.today())
            except AmazonOrdersError as e:
                raise AmazonError(f"Could not read the order history page: {e}") from e
            url = parse.next_orders_page(html)
            if not url:
                break
        return found


@dataclass
class ReturnableItem:
    order_number: str | None
    placed: str | None
    asin: str | None
    title: str
    return_by: str


async def returnable_items(session: Session) -> list[ReturnableItem]:
    items = [
        ReturnableItem(o.order_number, o.placed, i.asin, i.title, i.return_window.date)
        for o in await orders(session, "months-3", max_pages=5)
        for i in o.items
        if i.return_window and i.return_window.status == "open"
    ]
    return sorted(items, key=lambda i: i.return_by)


async def product(session: Session, asin: str) -> parse.Product:
    asin = _check_asin(asin)
    async with session.lock:
        return parse.parse_product(
            await _open(await session.page(), f"{BASE}/dp/{asin}")
        )


async def cart(session: Session) -> parse.Cart:
    async with session.lock:
        return parse.parse_cart(await _open(await session.page(), CART_URL))


async def cart_add(session: Session, asin: str, quantity: int = 1) -> parse.Cart:
    asin = _check_asin(asin)
    if not 1 <= quantity <= 30:
        raise AmazonError("quantity must be between 1 and 30.")
    async with session.lock:
        page = await session.page()
        info = parse.parse_product(await _open(page, f"{BASE}/dp/{asin}"))
        if not info.can_add_to_cart:
            raise AmazonError(
                f"Amazon shows no 'Add to cart' button for {asin} ({info.availability or 'no availability shown'})."
            )
        if quantity > 1:
            await page.select_option("select#quantity", str(quantity), force=True)
        await page.get_by_role("button", name="Add to cart", exact=True).first.click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1_500)
        result = parse.parse_cart(await _open(page, CART_URL))
        if not any(i.asin == asin for i in result.items):
            raise AmazonError(
                f"Clicked 'Add to cart' for {asin} but it is not in the cart."
            )
        return result


async def cart_remove(session: Session, asin: str) -> parse.Cart:
    asin = _check_asin(asin)
    async with session.lock:
        page = await session.page()
        await _open(page, CART_URL)
        row = page.locator(
            f'#sc-active-cart [data-asin="{asin}"][data-itemtype="active"]'
        ).first
        if not await row.count():
            raise AmazonError(f"{asin} is not in the cart.")
        await (
            row.locator(
                'input[value="Delete"], [data-action="delete"] input, [data-action="delete"] button'
            )
            .or_(row.get_by_role("button", name=re.compile("delete", re.I)))
            .first.click()
        )
        await page.wait_for_timeout(2_000)
        result = parse.parse_cart(await _open(page, CART_URL))
        if any(i.asin == asin for i in result.items):
            raise AmazonError(f"Clicked Delete for {asin} but it is still in the cart.")
        return result


async def _screenshot(page: Page) -> bytes | None:
    """Chromium draws nothing while its window is minimized, so a screenshot would wait forever.
    Restore the window for the capture, minimize it again, and go without a picture if it still fails.
    """
    cdp = await page.context.new_cdp_session(page)
    try:
        win = await cdp.send("Browser.getWindowForTarget")
        minimized = win["bounds"].get("windowState") == "minimized"

        async def set_state(state: str) -> None:
            await cdp.send(
                "Browser.setWindowBounds",
                {"windowId": win["windowId"], "bounds": {"windowState": state}},
            )

        if minimized:
            await set_state("normal")
        try:
            return await page.screenshot(full_page=True, timeout=15_000)
        except PlaywrightTimeout:
            return None
        finally:
            if minimized:
                await set_state("minimized")
    finally:
        await cdp.detach()


async def _cards(page: Page, use: str | None = None) -> list[parse.PaymentOption]:
    """From checkout, read the saved cards on Amazon's payment page and, with `use` (last 4
    digits), switch the order to that card. Opening checkout from the cart puts Amazon's
    default card back, so a chosen card is applied again before an order is placed.
    Leaves the page on checkout."""
    spc = page.url
    href = parse.payment_page_href(await page.content())
    if not href:
        if use:
            raise AmazonError("Amazon's checkout page shows no way to change the card.")
        return []
    cards = [
        c
        for c in parse.parse_payment_options(await _open(page, BASE + href))
        if not c.expired
    ]
    if not use:
        await _open(page, spc)
        return cards
    if use not in {c.last4 for c in cards}:
        await _open(page, spc)
        raise AmazonError(f"No saved card ending in {use} that has not expired.")
    await (
        page.locator(".pmts-instrument-selector")
        .filter(has_text=re.compile(rf"ending in {use}\b"))
        .locator('input:not([value*="isExpired=true"])')
        .first.check(force=True)
    )
    await page.get_by_role("button", name="Use this payment method").first.click()
    await page.wait_for_url(re.compile(r"/spc"), timeout=30_000)
    await page.wait_for_timeout(1_500)
    await _check_blocked(page)
    return cards


async def checkout_preview(
    session: Session, card: str | None = None
) -> tuple[parse.CheckoutSummary, bytes | None, list[parse.PaymentOption]]:
    """Open Amazon's checkout review page and read it, switching to `card` (last 4 digits)
    first if given. Places nothing."""
    async with session.lock:
        page = await session.page()
        await _open(page, CHECKOUT_URL)
        if "/checkout/" not in page.url:
            raise AmazonError(
                "Amazon did not open checkout. The cart is probably empty."
            )
        cards = await _cards(page, card)
        summary = parse.parse_checkout(await page.content())
        if card and parse.card_last4(summary.paying_with) != card:
            await page.bring_to_front()
            raise AmazonError(
                f"Amazon did not switch to the card ending in {card}. It may want you to "
                "confirm the card: do that in the Amazon window, then retry."
            )
        if summary.total is None or not summary.can_place:
            await page.bring_to_front()
            raise AmazonError(
                "Amazon's checkout page is asking for something first (address, payment or a choice). "
                "Finish that in the Amazon window, then retry."
            )
        return summary, await _screenshot(page), cards


async def place_order(session: Session, approved: parse.CheckoutSummary) -> str | None:
    """Only called after a human approves on the approval page. Re-reads checkout and refuses
    to click unless it still matches what the human approved."""
    async with session.lock:
        page = await session.page()
        current = parse.parse_checkout(await _open(page, CHECKOUT_URL))
        want = parse.card_last4(approved.paying_with)
        if want and parse.card_last4(current.paying_with) != want:
            await _cards(page, want)
            current = parse.parse_checkout(await page.content())
        reason = parse.summaries_match(approved, current)
        if reason:
            raise AmazonError(
                f"Not ordered: {reason} since you approved it. Ask your agent for a new approval."
            )
        await page.locator("input[name='placeYourOrder1']:visible").first.click()
        try:
            await page.wait_for_url(re.compile(r"thank-?you", re.I), timeout=45_000)
        except PlaywrightTimeout:
            if not re.search(
                r"order placed|thank you", await page.inner_text("body"), re.I
            ):
                await page.bring_to_front()
                raise AmazonError(
                    "Clicked 'Place your order' but Amazon showed no confirmation. Check the Amazon "
                    "window and Your Orders before trying again, so nothing is ordered twice."
                )
        number = parse.find_order_number(await page.inner_text("body"))
        return number or await _newest_order_number(page, approved.total)


async def _newest_order_number(page: Page, total: float | None) -> str | None:
    """Amazon's thank-you page can leave the order number out; Your Orders lists it.
    Never raises: the order is already placed, and a lookup failure must not say otherwise."""
    try:
        html = await _open(page, f"{BASE}/your-orders/orders?timeFilter=last30")
        today = date.today()
        for o in parse.parse_orders(html, today):
            if o.placed == today.isoformat() and o.total == total:
                return o.order_number
    except Exception:  # noqa: BLE001
        pass
    return None
