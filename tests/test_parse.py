from datetime import date
from pathlib import Path

from cartclaw import parse

FIX = Path(__file__).parent / "fixtures"


def html(name: str) -> str:
    return (FIX / f"{name}.html").read_text(encoding="utf-8")


def test_return_window_closed_text():
    w = parse.parse_return_window(
        "Return window closed on June 11, 2026", date(2026, 10, 2)
    )
    assert (w.status, w.date) == ("closed", "2026-06-11")


def test_return_window_open_until_future_date():
    w = parse.parse_return_window(
        "Return or replace items: Eligible through October 30, 2026", date(2026, 10, 2)
    )
    assert (w.status, w.date) == ("open", "2026-10-30")


def test_return_window_past_date_without_closed_word_is_closed():
    w = parse.parse_return_window(
        "Eligible for return through September 1, 2026", date(2026, 10, 2)
    )
    assert w.status == "closed"


def test_return_window_absent():
    assert parse.parse_return_window("Buy it again", date(2026, 10, 2)) is None


def test_orders_fixture():
    orders = parse.parse_orders(html("orders"), date(2026, 10, 2))
    assert len(orders) == 1
    first = orders[0]
    assert first.order_number == "111-0000000-0000001"
    assert first.placed == "2026-05-12"
    assert first.total == 33.05
    assert {i.asin for i in first.items} == {"B07V3946XL", "B001F71XAI"}
    assert all(
        i.return_window and i.return_window.status == "closed"
        for o in orders
        for i in o.items
    )
    assert first.items[0].return_window.date == "2026-06-11"


def test_product_fixture():
    p = parse.parse_product(html("product"))
    assert p.title.startswith("Keurig 3-Month Brewer Maintenance Kit")
    assert p.price == 14.98
    assert p.availability == "In Stock"
    assert p.can_add_to_cart


def test_cart_fixture():
    c = parse.parse_cart(html("cart"))
    assert [(i.asin, i.quantity, i.price) for i in c.items] == [
        ("B07V3946XL", 1, 14.98)
    ]
    assert c.items[0].title.startswith("Keurig")
    assert c.subtotal == 14.98


def test_empty_cart():
    assert (
        parse.parse_cart("<html><body>Your Amazon Cart is empty</body></html>").items
        == []
    )


def test_checkout_fixture():
    s = parse.parse_checkout(html("checkout"))
    assert s.total == 16.07
    assert s.lines["Items:"] == 14.98
    assert s.lines["Estimated tax to be collected:"] == 1.09
    assert s.items and s.items[0].startswith("Keurig")
    assert s.deliver_to.startswith("Pat Example, 1 MAIN ST")
    assert s.paying_with == "Visa 0000"
    assert s.arriving == "Arriving Oct 3, 2026"
    assert s.can_place


def test_summaries_match_identical():
    s = parse.parse_checkout(html("checkout"))
    assert parse.summaries_match(s, parse.parse_checkout(html("checkout"))) is None


def test_summaries_mismatch_total_blocks():
    approved = parse.parse_checkout(html("checkout"))
    current = parse.parse_checkout(html("checkout").replace("$16.07", "$99.07"))
    assert "total changed" in parse.summaries_match(approved, current)


def test_summaries_mismatch_address_blocks():
    approved = parse.parse_checkout(html("checkout"))
    current = parse.parse_checkout(html("checkout").replace("1 MAIN ST", "9 OTHER RD"))
    assert "address" in parse.summaries_match(approved, current)


def test_summaries_unreadable_total_blocks():
    approved = parse.parse_checkout(html("checkout"))
    current = parse.parse_checkout("<html><body></body></html>")
    assert parse.summaries_match(approved, current)


def test_signin_and_captcha_detection():
    assert parse.is_signin("https://www.amazon.com/ap/signin?openid.return_to=x")
    assert not parse.is_signin("https://www.amazon.com/your-orders/orders")
    assert parse.is_captcha('<form action="/errors/validateCaptcha">')
    assert not parse.is_captcha(html("product"))


def test_find_order_number():
    assert (
        parse.find_order_number("Order # 111-2222222-3333333 placed")
        == "111-2222222-3333333"
    )
    assert parse.find_order_number("no number") is None
