import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from cartclaw import parse, server
from cartclaw.approval import Approval, Desk, render
from cartclaw.lifecycle import (
    INITIAL,
    STATE_LABELS,
    TERMINAL,
    TRANSITIONS,
    ApprovalState,
    IllegalTransition,
    assert_transition,
    can_transition,
    next_states,
)

ROOT = Path(__file__).parents[1]
SUMMARY = parse.parse_checkout(
    (ROOT / "tests" / "fixtures" / "checkout.html").read_text(encoding="utf-8")
)
CARDS = [
    parse.PaymentOption("Visa ending in 0000", "0000", False),
    parse.PaymentOption("Discover ending in 1111", "1111", False),
]
ORDER = "113-0000000-0000000"

# Written by hand from the stored values in approval.py, not derived from the module.
STATES = [
    "pending",
    "switching",
    "placing",
    "placed",
    "rejected",
    "expired",
    "superseded",
    "failed",
]
LEGAL = [
    ("pending", "switching"),
    ("pending", "placing"),
    ("pending", "rejected"),
    ("pending", "expired"),
    ("pending", "superseded"),
    ("switching", "pending"),
    ("switching", "superseded"),
    ("placing", "placed"),
    ("placing", "failed"),
]
ILLEGAL = [(c, r) for c in STATES for r in STATES if c != r and (c, r) not in LEGAL]
TERMINAL_STATES = {"placed", "rejected", "expired", "superseded", "failed"}


# --- the table ---------------------------------------------------------------------------


@pytest.mark.parametrize("current,requested", ILLEGAL)
def test_illegal_transition_is_refused(current, requested):
    cur, req = ApprovalState(current), ApprovalState(requested)
    assert not can_transition(cur, req)
    with pytest.raises(IllegalTransition) as info:
        assert_transition(cur, req)
    err = info.value
    assert (err.current, err.requested) == (cur, req)
    assert err.allowed == next_states(cur)


@pytest.mark.parametrize("current,requested", LEGAL)
def test_legal_transition_is_accepted(current, requested):
    cur, req = ApprovalState(current), ApprovalState(requested)
    assert can_transition(cur, req)
    assert assert_transition(cur, req) is req


@pytest.mark.parametrize("state", STATES)
def test_repeat_of_the_current_state_is_a_no_op(state):
    s = ApprovalState(state)
    assert can_transition(s, s)
    assert assert_transition(s, s) is s
    assert s not in next_states(s)  # allowed_next never offers a repeat


def test_refusal_message_and_contract():
    err = IllegalTransition(ApprovalState.PLACED, ApprovalState.PLACING)
    assert str(err) == "illegal_transition: placed -> placing (allowed: none)"
    assert err.as_dict() == {
        "error": "illegal_transition",
        "current": "placed",
        "requested": "placing",
        "allowed": [],
    }
    assert json.loads(json.dumps(err.as_dict())) == err.as_dict()
    err = IllegalTransition(ApprovalState.SWITCHING, ApprovalState.REJECTED)
    assert err.as_dict()["allowed"] == ["pending", "superseded"]


def test_states_are_exactly_the_stored_values():
    assert [s.value for s in ApprovalState] == STATES
    assert all(ApprovalState(v) == v and f"{ApprovalState(v)}" == v for v in STATES)


def test_table_covers_every_state_and_only_states():
    assert set(TRANSITIONS) == set(ApprovalState)
    assert all(
        isinstance(t, ApprovalState) for nxt in TRANSITIONS.values() for t in nxt
    )
    assert {(s.value, t.value) for s, nxt in TRANSITIONS.items() for t in nxt} == set(
        LEGAL
    )


def test_initial_state():
    assert INITIAL is ApprovalState.PENDING
    assert Approval("i", "n", SUMMARY, None, 0.0).status is INITIAL


def test_every_state_is_reachable_from_initial():
    seen, todo = {INITIAL}, [INITIAL]
    while todo:
        for t in TRANSITIONS[todo.pop()]:
            if t not in seen:
                seen.add(t)
                todo.append(t)
    assert seen == set(ApprovalState)


def test_terminal_states_have_no_way_out():
    assert TERMINAL == {s for s in TRANSITIONS if not TRANSITIONS[s]}
    assert {s.value for s in TERMINAL} == TERMINAL_STATES
    assert all(next_states(s) == [] for s in TERMINAL)


def test_every_open_state_can_finish():
    for start in set(ApprovalState) - TERMINAL:
        seen, todo = {start}, [start]
        while todo:
            for t in TRANSITIONS[todo.pop()]:
                if t not in seen:
                    seen.add(t)
                    todo.append(t)
        assert seen & TERMINAL, start


def test_only_the_place_result_leaves_placing():
    assert TRANSITIONS[ApprovalState.PLACING] == {
        ApprovalState.PLACED,
        ApprovalState.FAILED,
    }


def test_only_open_requests_can_be_superseded():
    assert {s for s in ApprovalState if ApprovalState.SUPERSEDED in next_states(s)} == {
        ApprovalState.PENDING,
        ApprovalState.SWITCHING,
    }


def test_next_states_keep_declaration_order():
    order = list(ApprovalState)
    for s in ApprovalState:
        got = next_states(s)
        assert got == sorted(got, key=order.index) and set(got) == TRANSITIONS[s]


def test_every_state_has_a_label():
    assert set(STATE_LABELS) == set(ApprovalState)


# --- documented states equal the table ----------------------------------------------------


async def test_checkout_status_description_lists_every_state():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    desc = tools["checkout_status"].description
    for s in STATES:
        assert re.search(rf"\b{s}\b", desc), s
    assert "terminal" in desc and "allowed_next" in desc


def test_llms_txt_lists_every_state():
    text = (ROOT / "site" / "llms.txt").read_text(encoding="utf-8")
    line = next(x for x in text.splitlines() if x.startswith("- checkout_status:"))
    missing = [s for s in STATES if not re.search(rf"\b{s}\b", line)]
    assert missing == []
    assert "terminal" in line and "allowed_next" in line


# --- the desk ---------------------------------------------------------------------------


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(
    url: str, data: dict | None = None, origin: str | None = None, accept: str = ""
) -> tuple[int, str, str]:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body)
    if origin:
        req.add_header("Origin", origin)
    if accept:
        req.add_header("Accept", accept)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=5) as r:
            return r.status, r.headers["Content-Type"], r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers["Content-Type"], e.read().decode(errors="replace")


async def post(desk, a, decision, card="", accept=""):
    data = {"n": a.nonce, "decision": decision}
    if card:
        data["card"] = card
    url = f"{desk.base}/a/{a.id}/decide"
    return await asyncio.to_thread(fetch, url, data, desk.base, accept)


async def until(cond) -> bool:
    for _ in range(200):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.fixture
async def gated():
    # place and change_card wait on a gate, so a test can act while either is running.
    g = SimpleNamespace(
        place_gate=asyncio.Event(),
        switch_gate=asyncio.Event(),
        placed=[],
        switched=[],
        switch_done=[],
        fail=False,
    )

    async def place(a):
        g.placed.append(a.id)
        await g.place_gate.wait()
        if g.fail:
            raise RuntimeError("Not ordered: order total changed")
        return ORDER

    async def change_card(a, last4):
        g.switched.append(last4)
        await g.switch_gate.wait()
        g.switch_done.append(last4)
        return replace(SUMMARY, paying_with=f"Discover {last4}"), None, CARDS

    desk = Desk(
        place,
        asyncio.get_running_loop(),
        open_page=lambda u: None,
        change_card=change_card,
    )
    yield desk, g
    g.place_gate.set()
    g.switch_gate.set()
    await asyncio.sleep(0.05)
    desk.close()


async def reach(desk, g, state: str) -> Approval:
    """A fresh approval brought to state through the same paths a user and agent use."""
    if state == "expired":
        desk._ttl = 0
    a = desk.create(SUMMARY, b"", CARDS)
    if state == "pending":
        return a
    if state == "placing":
        assert (await post(desk, a, "approve"))[0] == 303
        assert await until(lambda: g.placed)
    elif state == "switching":
        assert (await post(desk, a, "card", "1111"))[0] == 303
        assert await until(lambda: g.switched)
    elif state in ("placed", "failed"):
        g.fail = state == "failed"
        g.place_gate.set()
        assert (await post(desk, a, "approve"))[0] == 303
        assert await until(lambda: a.status == state)
    elif state == "rejected":
        assert (await post(desk, a, "reject"))[0] == 303
    elif state == "expired":
        await asyncio.sleep(0.01)
        desk.get(a.id)
    elif state == "superseded":
        desk.create(SUMMARY, b"", CARDS)
    assert a.status == state
    return a


# What each page control asks for: approve -> placing, reject -> rejected, card -> switching.
ASKS = {"approve": "placing", "reject": "rejected", "card": "switching"}
REFUSED_POSTS = [
    (state, decision)
    for state in STATES
    for decision, requested in ASKS.items()
    if state != requested and (state, requested) not in LEGAL
]


@pytest.mark.parametrize("state,decision", REFUSED_POSTS)
async def test_post_that_the_table_refuses_is_409_and_changes_nothing(
    gated, state, decision
):
    desk, g = gated
    a = await reach(desk, g, state)
    before = (len(g.placed), len(g.switched), a.summary, a.order_number)
    code, _, _ = await post(desk, a, decision, "1111" if decision == "card" else "")
    assert code == 409
    assert a.status == state
    assert (len(g.placed), len(g.switched), a.summary, a.order_number) == before


@pytest.mark.parametrize(
    "state", ["placing", "placed", "rejected", "expired", "superseded", "failed"]
)
async def test_newer_request_leaves_a_finished_or_placing_request_alone(gated, state):
    desk, g = gated
    a = await reach(desk, g, state)
    new = desk.create(SUMMARY, b"", CARDS)
    assert a.status == state and new.status == "pending"


@pytest.mark.parametrize("state", ["placing", "switching"])
async def test_ttl_never_expires_a_running_request(gated, state):
    desk, g = gated
    a = await reach(desk, g, state)
    desk._ttl = 0
    await asyncio.sleep(0.01)
    assert desk.get(a.id).status == state


async def test_late_card_switch_does_not_revive_a_superseded_request(gated):
    desk, g = gated
    a = await reach(desk, g, "switching")
    desk.create(SUMMARY, b"", CARDS)
    assert a.status == "superseded"
    g.switch_gate.set()
    assert await until(lambda: g.switch_done)
    await asyncio.sleep(0.02)
    assert a.status == "superseded" and a.summary == SUMMARY


async def test_a_new_request_starts_pending(gated):
    desk, _ = gated
    a = desk.create(SUMMARY, b"", CARDS)
    assert a.status is INITIAL and a.status == "pending"


async def test_approve_places_then_records_the_order(gated):
    desk, g = gated
    a = await reach(desk, g, "placing")
    g.place_gate.set()
    assert await until(lambda: a.status == "placed")
    assert a.order_number == ORDER and g.placed == [a.id]


async def test_place_error_marks_failed_with_detail(gated):
    desk, g = gated
    a = await reach(desk, g, "failed")
    assert "total changed" in a.detail and a.order_number is None


async def test_reject_and_expiry_from_pending(gated):
    desk, g = gated
    assert (await reach(desk, g, "rejected")).status == "rejected"
    assert (await reach(desk, g, "expired")).status == "expired"
    assert g.placed == []


async def test_card_switch_goes_back_to_pending(gated):
    desk, g = gated
    a = await reach(desk, g, "switching")
    g.switch_gate.set()
    assert await until(lambda: a.status == "pending")
    assert a.summary.paying_with == "Discover 1111"


@pytest.mark.parametrize("state", ["pending", "switching"])
async def test_newer_request_supersedes_an_open_one(gated, state):
    desk, g = gated
    a = await reach(desk, g, state)
    desk.create(SUMMARY, b"", CARDS)
    assert a.status == "superseded"


async def test_second_place_click_is_a_no_op(gated):
    desk, g = gated
    a = await reach(desk, g, "placing")
    assert (await post(desk, a, "approve"))[0] == 303
    code, ctype, body = await post(desk, a, "approve", accept="application/json")
    assert code == 200 and ctype == "application/json"
    got = json.loads(body)
    assert got["status"] == "placing" and got["terminal"] is False
    assert got["allowed_next"] == ["placed", "failed"]
    g.place_gate.set()
    assert await until(lambda: a.status == "placed")
    assert g.placed == [a.id]  # placed once


async def test_second_reject_click_is_a_no_op(gated):
    desk, g = gated
    a = desk.create(SUMMARY, b"", CARDS)
    code, ctype, body = await post(desk, a, "reject", accept="application/json")
    assert code == 200 and ctype == "application/json"
    assert json.loads(body) == {
        "status": "rejected",
        "detail": "",
        "order_number": None,
        "terminal": True,
        "allowed_next": [],
    }
    assert (await post(desk, a, "reject"))[0] == 303
    assert a.status == "rejected" and g.placed == []


async def test_second_card_click_runs_one_switch(gated):
    desk, g = gated
    a = await reach(desk, g, "switching")
    assert (await post(desk, a, "card", "1111"))[0] == 303
    assert a.status == "switching" and g.switched == ["1111"]


# --- surfaces ---------------------------------------------------------------------------


async def test_refusal_as_json(gated):
    desk, g = gated
    a = await reach(desk, g, "placed")
    code, ctype, body = await post(desk, a, "approve", accept="application/json")
    assert code == 409 and ctype == "application/json"
    assert json.loads(body) == {
        "error": "illegal_transition",
        "current": "placed",
        "requested": "placing",
        "allowed": [],
    }
    assert g.placed == [a.id]


async def test_refusal_during_a_card_switch_as_json(gated):
    desk, g = gated
    a = await reach(desk, g, "switching")
    code, ctype, body = await post(desk, a, "reject", accept="application/json")
    assert code == 409 and ctype == "application/json"
    assert json.loads(body) == {
        "error": "illegal_transition",
        "current": "switching",
        "requested": "rejected",
        "allowed": ["pending", "superseded"],
    }


async def test_refusal_in_a_browser_keeps_the_page_text(gated):
    desk, g = gated
    a = await reach(desk, g, "placed")
    code, ctype, body = await post(desk, a, "approve")
    assert code == 409 and ctype.startswith("text/html")
    assert "This request is already placed." in body


@pytest.mark.parametrize("state", STATES)
def test_page_buttons_follow_the_table(state):
    s = ApprovalState(state)
    a = Approval("i", "n", SUMMARY, None, 1e12, cards=CARDS, order_number=ORDER)
    a.status = s
    page = render(a, "http://127.0.0.1:1/a/i?n=n").decode()
    nxt = next_states(s)
    assert ('value="approve">Place order' in page) == (ApprovalState.PLACING in nxt)
    assert ("Don't buy</button>" in page) == (ApprovalState.REJECTED in nxt)
    assert ("Use this card" in page) == (ApprovalState.SWITCHING in nxt)
    assert STATE_LABELS[s] in page
    assert ('http-equiv="refresh"' in page) == (state in ("placing", "switching"))


@pytest.mark.parametrize("state", STATES)
def test_agent_view_adds_terminal_and_allowed_next(state):
    a = Approval("i", "n", SUMMARY, None, 0.0, order_number=ORDER)
    a.status = ApprovalState(state)
    view = server._approval_view(a)
    assert list(view)[:7] == [
        "approval_id",
        "status",
        "detail",
        "order_number",
        "total",
        "items",
        "paying_with",
    ]
    assert view["terminal"] is (state in TERMINAL_STATES)
    assert view["allowed_next"] == [s.value for s in next_states(a.status)]
    wire = json.loads(json.dumps(view))
    assert type(wire["status"]) is str and wire["status"] == state


async def test_status_json_reports_terminal_and_allowed_next(gated):
    desk, g = gated
    for state, terminal, allowed in (
        (
            "pending",
            False,
            ["switching", "placing", "rejected", "expired", "superseded"],
        ),
        ("placed", True, []),
    ):
        a = await reach(desk, g, state)
        url = desk.url(a).replace("?", "/status?", 1)
        code, ctype, body = await asyncio.to_thread(fetch, url)
        got = json.loads(body)
        assert code == 200 and ctype == "application/json"
        assert got["status"] == state
        assert got["terminal"] is terminal and got["allowed_next"] == allowed


async def test_checkout_status_tool_reports_terminal_and_allowed_next(
    gated, monkeypatch
):
    desk, _ = gated
    monkeypatch.setattr(server, "_desk", desk)
    a = desk.create(SUMMARY, b"", CARDS)
    result = await server.mcp.call_tool("checkout_status", {"approval_id": a.id})
    got = result.structured_content
    assert got["status"] == "pending" and got["terminal"] is False
    assert got["allowed_next"] == [
        "switching",
        "placing",
        "rejected",
        "expired",
        "superseded",
    ]
    assert '"status":"pending"' in result.content[0].text.replace(" ", "")
