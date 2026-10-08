"""Mint, resolve, reserve, consume, release and expire refund handles (SPEC §4).

A handle is one refund option, such as "Bike bell x1, 4.00 USD". Wardlock computes every
amount from trusted order data, and the agent can only pick handles. That is how the
agent never types a money amount (guarantee G2, SPEC §0 invariant 2).

None of these functions commit. The caller owns the transaction, so P5 can write the
decision row and reserve the handles together.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from wardlock.models import (
    AgentSession,
    Handle,
    HandleKind,
    HandleState,
    LedgerEntry,
    Order,
    new_handle_id,
    utcnow,
)
from wardlock.money import from_minor

MAX_HANDLES_PER_REQUEST = 10
# Ledger rows in these states moved no money, so they don't reduce what is refundable.
NOT_REFUNDED_STATUSES = ("FAILED", "CANCELLED")


class SessionInvalid(Exception):
    """The session is closed or expired, so it may not mint handles."""


class HandleConflict(Exception):
    """Some handles were not AVAILABLE in this session. Nothing was reserved."""


class HandleStateError(Exception):
    """A handle was not in the state this step needs. This points to a bug, not an attack."""


@dataclass(frozen=True)
class RefundOption:
    """What the agent sees for one handle. Deliberately has no PayPal IDs (invariant 7)."""

    handle: str
    kind: HandleKind
    label: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class OrderOptions:
    order_ref: str
    placed_at: date
    status: str
    refund_options: list[RefundOption]


# ---------------------------------------------------------------- amounts


def goodwill_minor(item_total_minor: int, pct: int) -> int:
    """pct% of the item total, rounded half-up to whole minor units: 1998 x 25% = 499.5 -> 500."""
    if not isinstance(pct, int) or isinstance(pct, bool) or not 1 <= pct <= 100:
        raise ValueError("goodwill percentage must be a whole number from 1 to 100")
    exact = Decimal(item_total_minor) * pct / 100  # Decimal: no float rounding errors
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def remaining_refundable_minor(db: Session, capture_id: str) -> int:
    """How much of a capture can still be refunded, in minor units."""
    order = db.scalar(select(Order).where(Order.capture_id == capture_id))
    if order is None:
        raise LookupError("unknown capture")
    return _remaining(db, order)


def _remaining(db: Session, order: Order) -> int:
    # captured - already refunded (ledger) - in flight (RESERVED handles, e.g. held refunds).
    # Counting in-flight amounts stops two held refunds from adding up to more than the capture.
    refunded = db.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(
            LedgerEntry.capture_id == order.capture_id,
            LedgerEntry.status.not_in(NOT_REFUNDED_STATUSES),
        )
    )
    in_flight = db.scalar(
        select(func.coalesce(func.sum(Handle.amount_minor), 0)).where(
            Handle.capture_id == order.capture_id,
            Handle.state == HandleState.RESERVED,
        )
    )
    return max(order.captured_minor - refunded - in_flight, 0)


# ---------------------------------------------------------------- minting


def mint_handles(
    db: Session,
    session: AgentSession,
    *,
    goodwill_percentages: Sequence[int],
    now: datetime | None = None,
) -> list[OrderOptions]:
    """Create fresh refund options for every order of THIS session's customer (guarantee G1)."""
    now = now or utcnow()
    if not session.is_live(now):
        raise SessionInvalid("session is not active")
    # Options from an earlier call may show stale amounts, so they stop working now.
    expire_available_handles(db, session.id)

    orders = db.scalars(
        select(Order)
        .where(Order.customer_id == session.customer_id)
        .order_by(Order.placed_at.desc(), Order.order_ref)
    ).all()
    result = [_mint_for_order(db, session, order, goodwill_percentages, now) for order in orders]
    db.flush()
    return result


def _mint_for_order(
    db: Session, session: AgentSession, order: Order, goodwill_percentages: Sequence[int], now: datetime
) -> OrderOptions:
    remaining = _remaining(db, order)
    options = []
    for kind, item_ref, pct, label, amount in _candidates(db, order, goodwill_percentages, remaining):
        if amount > remaining:
            continue  # this option could never succeed, so don't offer it
        handle = Handle(
            handle_id=new_handle_id(),
            session_id=session.id,
            order_id=order.id,
            capture_id=order.capture_id,
            kind=kind,
            item_ref=item_ref,
            goodwill_pct=pct,
            label=label,
            amount_minor=amount,
            currency=order.currency,
            created_at=now,
        )
        db.add(handle)
        options.append(RefundOption(handle.handle_id, kind, label, from_minor(amount, order.currency), order.currency))
    return OrderOptions(order.order_ref, order.placed_at, order.status.value, options)


Candidate = tuple[HandleKind, str | None, int | None, str, int]


def _candidates(db: Session, order: Order, goodwill_percentages: Sequence[int], remaining: int) -> list[Candidate]:
    """Every option this order could offer: (kind, item_ref, goodwill_pct, label, amount_minor)."""
    spent = _spent(db, order.id)
    candidates: list[Candidate] = []

    for item in order.items_json:
        # An item already refunded, or being refunded, in any session is never offered again.
        if (HandleKind.LINE_ITEM, item["item_ref"]) not in spent:
            amount = item["unit_minor"] * item["quantity"]
            candidates.append((HandleKind.LINE_ITEM, item["item_ref"], None, f"{item['name']} x{item['quantity']}", amount))

    if order.shipping_minor > 0 and (HandleKind.SHIPPING, None) not in spent:
        candidates.append((HandleKind.SHIPPING, None, None, "Shipping", order.shipping_minor))

    # At most one goodwill gesture per order, ever.
    if not any(kind == HandleKind.GOODWILL_PCT for kind, _ref in spent):
        item_total = sum(item["unit_minor"] * item["quantity"] for item in order.items_json)
        for pct in sorted(set(goodwill_percentages)):
            amount = goodwill_minor(item_total, pct)
            if amount > 0:  # 10% of a few cents can round to nothing
                candidates.append((HandleKind.GOODWILL_PCT, None, pct, f"{pct}% goodwill", amount))

    if remaining > 0:
        candidates.append((HandleKind.FULL_REMAINING, None, None, "Full remaining refund", remaining))
    return candidates


def _spent(db: Session, order_id: str) -> set[tuple[HandleKind, str | None]]:
    """(kind, item_ref) of every handle on this order that is being refunded or was refunded."""
    rows = db.execute(
        select(Handle.kind, Handle.item_ref).where(
            Handle.order_id == order_id,
            Handle.state.in_((HandleState.RESERVED, HandleState.CONSUMED)),
        )
    ).all()
    return {(kind, item_ref) for kind, item_ref in rows}


# ---------------------------------------------------------------- lookups and state changes


def load_handles(db: Session, handle_ids: Sequence[str]) -> dict[str, Handle]:
    """The handles that exist among these IDs, keyed by ID. Unknown IDs are simply absent."""
    ids = _unique_ids(handle_ids)
    rows = db.scalars(select(Handle).where(Handle.handle_id.in_(ids))).all()
    return {handle.handle_id: handle for handle in rows}


def reserve_handles(db: Session, session_id: str, handle_ids: Sequence[str]) -> None:
    """AVAILABLE -> RESERVED, all or nothing, and only for this session's handles.

    One conditional UPDATE does the check and the change together, so two concurrent
    requests cannot both spend the same handle.
    """
    ids = _unique_ids(handle_ids)
    changed = _move(db, ids, HandleState.AVAILABLE, HandleState.RESERVED, Handle.session_id == session_id)
    if len(changed) != len(ids):
        _move(db, changed, HandleState.RESERVED, HandleState.AVAILABLE)  # undo exactly what we changed
        raise HandleConflict("some handles are not available in this session")


def consume_handles(db: Session, handle_ids: Sequence[str]) -> None:
    """RESERVED -> CONSUMED after PayPal confirmed the refund. All or nothing."""
    ids = _unique_ids(handle_ids)
    changed = _move(db, ids, HandleState.RESERVED, HandleState.CONSUMED)
    if len(changed) != len(ids):
        _move(db, changed, HandleState.CONSUMED, HandleState.RESERVED)
        raise HandleStateError("only RESERVED handles can be consumed")


def release_handles(db: Session, handle_ids: Sequence[str], now: datetime | None = None) -> None:
    """RESERVED -> AVAILABLE after a hold was denied or timed out.

    If the handle's session has ended, it becomes EXPIRED instead: options from a dead
    session must never come back.
    """
    ids = _unique_ids(handle_ids)
    handles = db.scalars(select(Handle).where(Handle.handle_id.in_(ids))).all()
    if len(handles) != len(ids) or any(h.state != HandleState.RESERVED for h in handles):
        raise HandleStateError("only RESERVED handles can be released")
    now = now or utcnow()
    for handle in handles:
        session = db.get(AgentSession, handle.session_id)
        handle.state = HandleState.AVAILABLE if session and session.is_live(now) else HandleState.EXPIRED
    db.flush()


def expire_available_handles(db: Session, session_id: str) -> None:
    """AVAILABLE -> EXPIRED for one session. RESERVED handles are left for their hold to settle."""
    db.execute(
        update(Handle)
        .where(Handle.session_id == session_id, Handle.state == HandleState.AVAILABLE)
        .values(state=HandleState.EXPIRED)
    )


def _move(db: Session, ids: Sequence[str], from_state: HandleState, to_state: HandleState, *extra) -> list[str]:
    """Change the state of those handles that are in from_state. Returns the IDs actually changed."""
    if not ids:
        return []
    stmt = (
        update(Handle)
        .where(Handle.handle_id.in_(ids), Handle.state == from_state, *extra)
        .values(state=to_state)
        .returning(Handle.handle_id)
    )
    return list(db.scalars(stmt).all())


def _unique_ids(handle_ids: Sequence[str]) -> list[str]:
    if isinstance(handle_ids, str) or not all(isinstance(h, str) for h in handle_ids):
        raise ValueError("handle IDs must be a list of strings")
    ids = list(dict.fromkeys(handle_ids))  # removes duplicates, keeps order
    if not 1 <= len(ids) <= MAX_HANDLES_PER_REQUEST:
        raise ValueError(f"between 1 and {MAX_HANDLES_PER_REQUEST} handles are allowed")
    return ids
