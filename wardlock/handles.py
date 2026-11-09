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
    options = []
    for kind, item_ref, pct, label, amount in refund_candidates(db, order, goodwill_percentages):
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


def refund_candidates(db: Session, order: Order, goodwill_percentages: Sequence[int]) -> list[Candidate]:
    """Every option Wardlock would offer on this order right now: (kind, item_ref, pct, label, amount).

    Options above what is left to refund are dropped, because they could never succeed.
    Minting and the compat tool both use this, so they always agree on what is refundable.
    """
    remaining = _remaining(db, order)
    return [c for c in _candidates(db, order, goodwill_percentages, remaining) if c[4] <= remaining]


def derivable_amounts(db: Session, order: Order, goodwill_percentages: Sequence[int]) -> dict[int, frozenset[HandleKind]]:
    """Compat (SPEC §3.4): each amount Wardlock would offer -> the kinds that produce exactly it."""
    result: dict[int, frozenset[HandleKind]] = {}
    for kind, _item_ref, _pct, _label, amount in refund_candidates(db, order, goodwill_percentages):
        result[amount] = result.get(amount, frozenset()) | {kind}
    return result


def reserve_new_handle(
    db: Session,
    session: AgentSession,
    order: Order,
    candidate: Candidate,
    now: datetime | None = None,
) -> Handle:
    """Create a handle that is RESERVED from the start, for a compat refund (SPEC §6 step 10).

    It copies the matching candidate (kind, item, label), so a raw 4.00 for the bell marks
    the bell as spent, and the amount counts as in flight like any other reserved handle.
    """
    kind, item_ref, pct, label, amount = candidate
    _lock_orders(db, {order.id})
    if _already_spent(db, order.id, kind, item_ref, amount_minor=amount):
        raise HandleConflict("this item was already refunded or is being refunded")
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
        state=HandleState.RESERVED,
        created_at=now or utcnow(),
    )
    db.add(handle)
    db.flush()
    return handle


def _candidates(db: Session, order: Order, goodwill_percentages: Sequence[int], remaining: int) -> list[Candidate]:
    """Every option this order could offer: (kind, item_ref, goodwill_pct, label, amount_minor)."""
    used = _used_minor(db, order.id)
    spent = _spent_kinds(db, order.id)
    candidates: list[Candidate] = []

    for item in order.items_json:
        unit, quantity = item["unit_minor"], item["quantity"]
        if unit <= 0:
            continue
        # Units not yet refunded (or being refunded) in any session. A line-item handle's
        # amount is always unit x units, so the amounts used tell us the units used.
        available = (unit * quantity - used.get(item["item_ref"], 0)) // unit
        for units in _unit_counts(available):
            label = f"{item['name']} x{units}" if units == quantity else f"{item['name']} x{units} (of {quantity})"
            candidates.append((HandleKind.LINE_ITEM, item["item_ref"], None, label, unit * units))

    if order.shipping_minor > 0 and HandleKind.SHIPPING not in spent:
        candidates.append((HandleKind.SHIPPING, None, None, "Shipping", order.shipping_minor))

    # At most one goodwill gesture per order, ever.
    if HandleKind.GOODWILL_PCT not in spent:
        item_total = sum(item["unit_minor"] * item["quantity"] for item in order.items_json)
        for pct in sorted(set(goodwill_percentages)):
            amount = goodwill_minor(item_total, pct)
            if amount > 0:  # 10% of a few cents can round to nothing
                candidates.append((HandleKind.GOODWILL_PCT, None, pct, f"{pct}% goodwill", amount))

    if remaining > 0:
        candidates.append((HandleKind.FULL_REMAINING, None, None, "Full remaining refund", remaining))
    return candidates


MAX_UNIT_OPTIONS = 5  # above this many units, offer only "1" and "all that is left"


def _unit_counts(available: int) -> list[int]:
    """How many units to offer options for: 1, 2, ... up to what is left (short lists stay short)."""
    if available <= 0:
        return []
    if available <= MAX_UNIT_OPTIONS:
        return list(range(1, available + 1))
    return [1, available]


_IN_USE = (HandleState.RESERVED, HandleState.CONSUMED)


def _used_minor(db: Session, order_id: str, exclude: Sequence[str] = ()) -> dict[str, int]:
    """item_ref -> amount of its line-item handles that are being refunded or were refunded."""
    rows = db.execute(
        select(Handle.item_ref, func.sum(Handle.amount_minor)).where(
            Handle.order_id == order_id,
            Handle.kind == HandleKind.LINE_ITEM,
            Handle.state.in_(_IN_USE),
            Handle.handle_id.not_in(list(exclude)),
        ).group_by(Handle.item_ref)
    ).all()
    return {item_ref: int(total) for item_ref, total in rows}


def _spent_kinds(db: Session, order_id: str) -> set[HandleKind]:
    """Shipping and goodwill can be refunded once per order: which of them already are."""
    return set(db.scalars(
        select(Handle.kind).where(
            Handle.order_id == order_id,
            Handle.kind.in_((HandleKind.SHIPPING, HandleKind.GOODWILL_PCT)),
            Handle.state.in_(_IN_USE),
        )
    ))


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
    handles = db.scalars(select(Handle).where(Handle.handle_id.in_(ids))).all()
    _lock_orders(db, {h.order_id for h in handles})
    # Requested amounts per thing, so two handles for the same item are checked together.
    requested: dict[tuple[str, HandleKind, str | None], int] = {}
    for h in handles:
        key = (h.order_id, h.kind, h.item_ref)
        requested[key] = requested.get(key, 0) + h.amount_minor
    if any(_already_spent(db, order_id, kind, item_ref, exclude=ids, amount_minor=amount)
           for (order_id, kind, item_ref), amount in requested.items()):
        # The item (beyond its quantity), the shipping or a goodwill gesture was refunded, or is
        # being refunded, through ANOTHER handle: a second session's copy, or a compat refund.
        raise HandleConflict("this item was already refunded or is being refunded")
    changed = _move(db, ids, HandleState.AVAILABLE, HandleState.RESERVED, Handle.session_id == session_id)
    if len(changed) != len(ids):
        _move(db, changed, HandleState.RESERVED, HandleState.AVAILABLE)  # undo exactly what we changed
        raise HandleConflict("some handles are not available in this session")


def _lock_orders(db: Session, order_ids: set[str]) -> None:
    """Serialise reservations on the same order, so two requests can't both pass the "already
    spent" check. Postgres takes a row lock (FOR UPDATE). SQLite has no row locks, but it lets
    only one writer at a time, and callers have already written their decision row."""
    if order_ids:
        db.execute(select(Order.id).where(Order.id.in_(order_ids)).with_for_update()).all()


def _already_spent(
    db: Session, order_id: str, kind: HandleKind, item_ref: str | None, exclude: Sequence[str] = (),
    amount_minor: int = 0,
) -> bool:
    """True if refunding `amount_minor` more of this thing on this order would be too much.

    - A line item: everything already used (RESERVED or CONSUMED handles, excluding `exclude`)
      plus this amount may not exceed unit x quantity. So one cage of two can be refunded, then
      the other, but never a third.
    - Shipping and goodwill: at most once per order (at most one goodwill gesture).
    - FULL_REMAINING and CUSTOM_AMOUNT have no item of their own; the remaining-amount check covers them.
    """
    if kind == HandleKind.LINE_ITEM:
        order = db.get(Order, order_id)
        item = next((i for i in (order.items_json if order else []) if i["item_ref"] == item_ref), None)
        if item is None:
            return True  # an item the order doesn't have: never refundable
        used = _used_minor(db, order_id, exclude).get(item_ref, 0)
        return used + amount_minor > item["unit_minor"] * item["quantity"]
    if kind not in (HandleKind.SHIPPING, HandleKind.GOODWILL_PCT):
        return False
    query = select(Handle.handle_id).where(
        Handle.order_id == order_id,
        Handle.kind == kind,
        Handle.state.in_(_IN_USE),
        Handle.handle_id.not_in(list(exclude)),
    )
    return db.scalar(query.limit(1)) is not None


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
