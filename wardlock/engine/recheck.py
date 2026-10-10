"""Re-check a hold right before a merchant's approval executes it (SPEC §6 step 9). Pure.

A hold can wait for minutes, and in that time the order may be refunded elsewhere,
reassigned, or pass 180 days. The re-check can only REFUSE an approval, never change what it
would do: the frozen amount and idempotency key are not even inputs here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from wardlock.engine.checks import REFUND_WINDOW
from wardlock.engine.codes import Code
from wardlock.engine.types import OrderView
from wardlock.enums import HandleState


@dataclass(frozen=True)
class HoldFacts:
    session_customer_id: str
    amount_minor: int
    order: OrderView | None  # fresh; None when the order can no longer be found
    # remaining_minor counts this hold's own RESERVED handles as "in flight". They are the
    # very refund being approved, so they are added back before comparing.
    own_reserved_minor: int
    handle_states: tuple[HandleState, ...]  # this hold's handles, read fresh


def recheck_approval(facts: HoldFacts, now: datetime) -> tuple[Code, ...]:
    """The reasons this approval must not run now. Empty means it may run."""
    if facts.order is None:
        return (Code.DEPENDENCY_UNAVAILABLE,)  # cannot verify anything: wait until the data is back
    codes = []
    if not facts.handle_states or any(state != HandleState.RESERVED for state in facts.handle_states):
        codes.append(Code.HANDLE_ALREADY_USED)
    if facts.order.customer_id != facts.session_customer_id:
        codes.append(Code.NOT_OWNER)
    if facts.amount_minor > facts.order.remaining_minor + facts.own_reserved_minor:
        codes.append(Code.EXCEEDS_REFUNDABLE)
    if now - facts.order.captured_at > REFUND_WINDOW:
        codes.append(Code.REFUND_WINDOW_EXPIRED)
    return tuple(codes)
