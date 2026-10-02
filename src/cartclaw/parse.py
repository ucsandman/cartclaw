"""Pure HTML parsers for the Amazon pages the server reads. No browser, no network."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from amazonorders.conf import AmazonOrdersConfig
from amazonorders.orders import AmazonOrders
from bs4 import BeautifulSoup

_DATE = r"([A-Z][a-z]+ \d{1,2}, \d{4})"
_RETURN_RX = re.compile(r"return[^\n]*?" + _DATE, re.I)
_MONEY_RX = re.compile(r"-?\$?([\d,]+\.\d{2})")
_ORDER_NUMBER_RX = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _text(tag) -> str:
    return tag.get_text(" ", strip=True) if tag else ""


def money(text: str) -> float | None:
    m = _MONEY_RX.search(text or "")
    return float(m.group(1).replace(",", "")) if m else None


def is_signin(url: str) -> bool:
    return bool(re.search(r"/ap/(signin|mfa|cvf)|/ax/claim", url))


def is_captcha(html: str) -> bool:
    return (
        "validateCaptcha" in html
        or 'id="captchacharacters"' in html
        or bool(
            re.search(r"enter the characters you see|solve this puzzle", html, re.I)
        )
    )


@dataclass
class ReturnWindow:
    status: str  # "open" or "closed"
    date: str  # ISO date the window closes or closed


def parse_return_window(text: str, today: date) -> ReturnWindow | None:
    m = _RETURN_RX.search(text or "")
    if not m:
        return None
    closes = datetime.strptime(m.group(1), "%B %d, %Y").date()
    closed = "closed" in text.lower() or closes < today
    return ReturnWindow("closed" if closed else "open", closes.isoformat())


@dataclass
class OrderItem:
    asin: str | None
    title: str
    price: float | None
    quantity: int | None
    return_window: ReturnWindow | None


@dataclass
class Order:
    order_number: str | None
    placed: str | None
    total: float | None
    items: list[OrderItem] = field(default_factory=list)


def parse_orders(
    html: str, today: date, config: AmazonOrdersConfig | None = None
) -> list[Order]:
    """Order history page -> Orders. amazon-orders does the parsing; return windows are read
    from the item box text because its return selector misses Amazon's current layout."""
    config = config or AmazonOrdersConfig()
    orders = []
    for o in AmazonOrders.parse_order_history(html, config):
        items = []
        for it in o.items:
            window = None
            if it.return_eligible_date:
                window = parse_return_window(
                    f"Return by {it.return_eligible_date:%B %d, %Y}", today
                )
            for line in it.parsed.get_text("\n", strip=True).splitlines():
                window = parse_return_window(line, today) or window
            items.append(
                OrderItem(
                    it.asin, (it.title or "").strip(), it.price, it.quantity, window
                )
            )
        orders.append(
            Order(
                o.order_number,
                o.order_placed_date.isoformat() if o.order_placed_date else None,
                o.grand_total,
                items,
            )
        )
    return orders


def next_orders_page(html: str, config: AmazonOrdersConfig | None = None) -> str | None:
    config = config or AmazonOrdersConfig()
    a = _soup(html).select_one(config.selectors.NEXT_PAGE_LINK_SELECTOR)
    if not a or not a.get("href"):
        return None
    href = a["href"]
    return href if href.startswith("http") else config.constants.BASE_URL + href


@dataclass
class Product:
    title: str
    price: float | None
    availability: str
    can_add_to_cart: bool


def parse_product(html: str) -> Product:
    s = _soup(html)
    title = _text(s.select_one("span#productTitle"))
    price = money(_text(s.select_one("#corePrice_feature_div .a-offscreen")))
    availability = _text(s.select_one("#availability"))
    return Product(
        title, price, availability, s.select_one("#add-to-cart-button") is not None
    )


@dataclass
class CartItem:
    asin: str
    title: str
    quantity: int
    price: float | None


@dataclass
class Cart:
    items: list[CartItem]
    subtotal: float | None


def parse_cart(html: str) -> Cart:
    s = _soup(html)
    items = []
    for row in s.select("#sc-active-cart [data-asin][data-itemtype='active']"):
        title = _text(row.select_one("span.a-truncate-full")) or _text(
            row.select_one(".sc-product-title")
        )
        price = row.get("data-price")
        items.append(
            CartItem(
                row["data-asin"],
                title,
                int(row.get("data-quantity") or 1),
                float(price) if price else None,
            )
        )
    return Cart(items, money(_text(s.select_one("#sc-subtotal-amount-buybox"))))


@dataclass
class CheckoutSummary:
    items: list[str]
    lines: dict[
        str, float | None
    ]  # "Items:", "Shipping & handling:", ... as shown by Amazon
    total: float | None
    deliver_to: str
    paying_with: str
    arriving: str
    can_place: bool


def parse_checkout(html: str) -> CheckoutSummary:
    s = _soup(html)
    lines: dict[str, float | None] = {}
    for li in s.select("#subtotals-marketplace-table li"):
        term = _text(li.select_one(".order-summary-line-term"))
        if term:
            lines[term] = money(_text(li.select_one(".order-summary-line-definition")))
    total = next(
        (v for k, v in lines.items() if k.lower().startswith("order total")), None
    )
    name = (
        _text(s.select_one("#deliver-to-customer-text"))
        .removeprefix("Delivering to")
        .strip()
    )
    address = _text(s.select_one("#deliver-to-address-text"))
    return CheckoutSummary(
        items=[_text(t) for t in s.select(".lineitem-title-text")],
        lines=lines,
        total=total,
        deliver_to=", ".join(x for x in (name, address) if x),
        paying_with=_text(s.select_one("#payment-option-text-default"))
        .removeprefix("Paying with")
        .strip(),
        arriving=_text(s.select_one("h2.address-promise-text")),
        can_place=s.select_one("input[name='placeYourOrder1']") is not None,
    )


def summaries_match(approved: CheckoutSummary, current: CheckoutSummary) -> str | None:
    """None when the order about to be placed is the one the human approved, else the reason."""
    if current.total is None or current.total != approved.total:
        return f"order total changed from ${approved.total} to ${current.total}"
    if sorted(current.items) != sorted(approved.items):
        return "the items in the order changed"
    if current.deliver_to != approved.deliver_to:
        return "the delivery address changed"
    if current.paying_with != approved.paying_with:
        return "the payment method changed"
    return None


def find_order_number(text: str) -> str | None:
    m = _ORDER_NUMBER_RX.search(text or "")
    return m.group(0) if m else None
