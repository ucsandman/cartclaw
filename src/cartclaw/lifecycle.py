"""The checkout approval lifecycle as one table. Every write to Approval.status goes through
it, the approval page draws its buttons from it, and agents read terminal and allowed_next
from it."""

from __future__ import annotations

from enum import StrEnum


class ApprovalState(StrEnum):
    PENDING = "pending"
    SWITCHING = "switching"  # the user is changing the card; the order is not placed
    PLACING = "placing"
    PLACED = "placed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    FAILED = "failed"


INITIAL = ApprovalState.PENDING

_S = ApprovalState
TRANSITIONS: dict[ApprovalState, frozenset[ApprovalState]] = {
    # user: Use this card / Place order / Don't buy; system: TTL; agent: newer request
    _S.PENDING: frozenset(
        {_S.SWITCHING, _S.PLACING, _S.REJECTED, _S.EXPIRED, _S.SUPERSEDED}
    ),
    # system: the card switch finished or failed; agent: newer request
    _S.SWITCHING: frozenset({_S.PENDING, _S.SUPERSEDED}),
    # system only: Amazon placed the order, or anything went wrong
    _S.PLACING: frozenset({_S.PLACED, _S.FAILED}),
    _S.PLACED: frozenset(),
    _S.REJECTED: frozenset(),
    _S.EXPIRED: frozenset(),
    _S.SUPERSEDED: frozenset(),
    _S.FAILED: frozenset(),
}

TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)

# Headlines on the approval page and its <title> (pending keeps its own title).
STATE_LABELS: dict[ApprovalState, str] = {
    _S.PENDING: "Approve this Amazon order?",
    _S.SWITCHING: "Switching card…",
    _S.PLACING: "Placing your order…",
    _S.PLACED: "Order placed",
    _S.REJECTED: "Not ordered",
    _S.EXPIRED: "Not ordered",
    _S.SUPERSEDED: "Not ordered",
    _S.FAILED: "Not ordered",
}


def next_states(current: ApprovalState) -> list[ApprovalState]:
    """The states current can move to, in declaration order. Empty for a terminal state."""
    return [s for s in ApprovalState if s in TRANSITIONS[current]]


class IllegalTransition(ValueError):
    def __init__(self, current: ApprovalState, requested: ApprovalState) -> None:
        self.current = ApprovalState(current)
        self.requested = ApprovalState(requested)
        self.allowed = next_states(self.current)
        allowed = ", ".join(self.allowed) or "none"
        super().__init__(
            f"illegal_transition: {self.current} -> {self.requested} (allowed: {allowed})"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "error": "illegal_transition",
            "current": self.current.value,
            "requested": self.requested.value,
            "allowed": [s.value for s in self.allowed],
        }


def can_transition(current: ApprovalState, requested: ApprovalState) -> bool:
    """True for a table edge, and for requested == current: a repeat is a no-op, not an error."""
    return requested == current or requested in TRANSITIONS[current]


def assert_transition(
    current: ApprovalState, requested: ApprovalState
) -> ApprovalState:
    """Return requested, or raise IllegalTransition. Callers skip side effects when it
    equals current."""
    if not can_transition(current, requested):
        raise IllegalTransition(current, requested)
    return ApprovalState(requested)
