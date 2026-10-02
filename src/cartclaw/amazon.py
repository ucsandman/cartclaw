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


async def checkout_preview(session: Session) -> tuple[parse.CheckoutSummary, bytes]:
    """Open Amazon's checkout review page and read it. Places nothing."""
    async with session.lock:
        page = await session.page()
        html = await _open(page, CHECKOUT_URL)
        if "/checkout/" not in page.url:
            raise AmazonError(
                "Amazon did not open checkout. The cart is probably empty."
            )
        summary = parse.parse_checkout(html)
        if summary.total is None or not summary.can_place:
            await page.bring_to_front()
            raise AmazonError(
                "Amazon's checkout page is asking for something first (address, payment or a choice). "
                "Finish that in the Amazon window, then retry."
            )
        return summary, await page.screenshot(full_page=True)


async def place_order(session: Session, approved: parse.CheckoutSummary) -> str | None:
    """Only called after a human approves on the approval page. Re-reads checkout and refuses
    to click unless it still matches what the human approved."""
    async with session.lock:
        page = await session.page()
        current = parse.parse_checkout(await _open(page, CHECKOUT_URL))
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
        return parse.find_order_number(await page.inner_text("body"))
