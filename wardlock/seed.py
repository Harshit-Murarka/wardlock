"""Seed the PayPal sandbox with demo orders and record them in the database (SPEC §7).

Every order needs a manual approval in the browser, which makes seeding slow to redo.
So this module is resumable and idempotent:
- progress is saved to a state file after every PayPal step;
- re-running never creates a second PayPal order or a duplicate database row;
- restore_db() rebuilds the database rows from the state file without calling PayPal.

Seed files are parsed with parse_float=Decimal, so no amount ever becomes a float
(SPEC §0 invariant 3).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from wardlock.config import SANDBOX_BASE_URL
from wardlock.models import Customer, Order, OrderStatus
from wardlock.money import to_minor, to_paypal_value
from wardlock.paypal.client import PayPalClient
from wardlock.paypal.errors import PayPalRequestError

BRAND_NAME = "Wardlock Demo Bike Shop"
# Browser mode: the buyer lands here after approving. The script does not need the
# redirect, so a placeholder page is enough.
RETURN_URL = "https://example.com/wardlock/approved"
CANCEL_URL = "https://example.com/wardlock/cancelled"

# Card mode: one of PayPal's PUBLISHED sandbox test cards (US Visa), from
# developer.paypal.com/tools/sandbox/card-testing (checked Oct 2026). It only works in
# the sandbox and only moves fake money. If it gets declined, generate another one in the
# dashboard (Testing Tools > Credit card generator) and pass --card-number.
DEFAULT_SANDBOX_CARD_NUMBER = "4005519200000004"
CARDHOLDER_NAME = "Demo Buyer"
BILLING_ADDRESS = {
    "address_line_1": "1 Example Way",
    "admin_area_2": "San Jose",
    "admin_area_1": "CA",
    "postal_code": "95131",
    "country_code": "US",
}

# Same shape check the client uses for PayPal ids, applied to ids PayPal sends back.
_PAYPAL_ID = re.compile(r"[A-Za-z0-9-]{1,64}")


class SeedError(ValueError):
    """The seed files or the saved state are inconsistent. The message says what to fix."""


@dataclass(frozen=True)
class SandboxCard:
    """A sandbox test card that pays seed orders directly, with no browser approval."""

    number: str = DEFAULT_SANDBOX_CARD_NUMBER
    # PayPal accepts any future month for test cards.
    expiry: str = field(default_factory=lambda: f"{date.today().year + 3}-12")
    security_code: str = "123"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9]{13,19}", self.number):
            raise SeedError("card number must be 13-19 digits")
        if not re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", self.expiry):
            raise SeedError("card expiry must look like 2029-12")
        if not re.fullmatch(r"[0-9]{3,4}", self.security_code):
            raise SeedError("card security code must be 3 or 4 digits")


# ---------------------------------------------------------------- seed files


class SeedCustomer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^cust_[a-z0-9_]{1,40}$")
    first_name: str = Field(min_length=1, max_length=100)
    # Fake addresses only: real customer data never goes into seed files.
    email: str = Field(pattern=r"^[a-z0-9._+-]+@example\.com$")


class SeedItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_ref: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    name: str = Field(min_length=1, max_length=127)
    unit_amount: Decimal = Field(gt=0)
    quantity: int = Field(ge=1, le=99)


class SeedOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order_ref: str = Field(pattern=r"^[A-Z]-\d{4}$")
    customer_id: str
    placed_at: date
    status: OrderStatus
    currency: str = "USD"
    items: list[SeedItem] = Field(min_length=1)
    shipping: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def _amounts_are_exact(self) -> SeedOrder:
        refs = [item.item_ref for item in self.items]
        if len(set(refs)) != len(refs):
            raise ValueError("item_ref values must be unique within an order")
        # to_minor raises for an unknown currency or for more decimals than it allows.
        to_minor(self.shipping, self.currency)
        for item in self.items:
            to_minor(item.unit_amount, self.currency)
        return self

    @property
    def item_total(self) -> Decimal:
        return sum((item.unit_amount * item.quantity for item in self.items), Decimal(0))

    @property
    def total(self) -> Decimal:
        return self.item_total + self.shipping

    def content_hash(self) -> str:
        """Short fingerprint of everything that affects the PayPal order."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class SeedData:
    customers: list[SeedCustomer]
    orders: list[SeedOrder]


def load_seed(customers_path: Path, orders_path: Path) -> SeedData:
    try:
        customers = [SeedCustomer.model_validate(c) for c in _read_json_list(customers_path)]
        orders = [SeedOrder.model_validate(o) for o in _read_json_list(orders_path)]
    except ValueError as exc:  # includes pydantic.ValidationError
        raise SeedError(f"invalid seed file: {exc}") from None

    _require_unique([c.id for c in customers], "customer id")
    _require_unique([c.email for c in customers], "customer email")
    _require_unique([o.order_ref for o in orders], "order_ref")
    known = {c.id for c in customers}
    for order in orders:
        if order.customer_id not in known:
            raise SeedError(f"{order.order_ref}: unknown customer_id {order.customer_id!r}")
    return SeedData(customers, orders)


def _read_json_list(path: Path) -> list[Any]:
    data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    if not isinstance(data, list):
        raise SeedError(f"{path} must contain a JSON list")
    return data


def _require_unique(values: Sequence[str], what: str) -> None:
    duplicates = sorted({v for v in values if values.count(v) > 1})
    if duplicates:
        raise SeedError(f"duplicate {what}: {', '.join(duplicates)}")


def select_orders(data: SeedData, order_refs: Sequence[str] | None) -> SeedData:
    """Keep only the named orders (all of them when order_refs is empty)."""
    if not order_refs:
        return data
    unknown = sorted(set(order_refs) - {o.order_ref for o in data.orders})
    if unknown:
        raise SeedError(f"unknown order_ref: {', '.join(unknown)}")
    return SeedData(data.customers, [o for o in data.orders if o.order_ref in order_refs])


def build_order_payload(order: SeedOrder, card: SandboxCard | None = None) -> dict[str, Any]:
    """The PayPal Orders v2 create-order body for one seed order.

    With a card, PayPal takes the payment in this single call (order COMPLETED).
    Without one, the buyer must approve the order in a browser first.
    """
    currency = order.currency

    def money(value: Decimal) -> dict[str, str]:
        return {"currency_code": currency, "value": to_paypal_value(value, currency)}

    # PayPal checks that amount == item_total + shipping, and that item_total == the sum of
    # unit_amount x quantity. We compute both exactly from the seed data.
    breakdown = {"item_total": money(order.item_total)}
    if order.shipping > 0:
        breakdown["shipping"] = money(order.shipping)

    return {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": order.order_ref,
                "custom_id": order.order_ref,
                "description": f"Wardlock demo order {order.order_ref}",
                "amount": {**money(order.total), "breakdown": breakdown},
                "items": [
                    {
                        "name": item.name,
                        "sku": item.item_ref,
                        "quantity": str(item.quantity),  # PayPal wants a string here
                        "unit_amount": money(item.unit_amount),
                    }
                    for item in order.items
                ],
            }
        ],
        "payment_source": _payment_source(card),
    }


def _payment_source(card: SandboxCard | None) -> dict[str, Any]:
    if card is not None:
        return {
            "card": {
                "number": card.number,
                "expiry": card.expiry,
                "security_code": card.security_code,
                "name": CARDHOLDER_NAME,
                "billing_address": dict(BILLING_ADDRESS),
            }
        }
    return {
        "paypal": {
            "experience_context": {
                "brand_name": BRAND_NAME,
                "shipping_preference": "NO_SHIPPING",
                "user_action": "PAY_NOW",
                "return_url": RETURN_URL,
                "cancel_url": CANCEL_URL,
            }
        }
    }


# ---------------------------------------------------------------- state file


class OrderState(BaseModel):
    content_hash: str
    create_attempt: int = 0
    paypal_order_id: str | None = None
    approve_url: str | None = None
    capture_id: str | None = None
    captured_minor: int | None = None
    currency: str | None = None
    captured_at: datetime | None = None


class SeedState(BaseModel):
    orders: dict[str, OrderState] = {}


def load_state(path: Path) -> SeedState:
    if not path.exists():
        return SeedState()
    return SeedState.model_validate_json(path.read_text(encoding="utf-8"))


def save_state(state: SeedState, path: Path) -> None:
    """Write via a temp file and rename, so a crash can never leave a half-written state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".seed-state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(state.model_dump_json(indent=2))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def check_no_edits_after_capture(data: SeedData, state: SeedState) -> None:
    for order in data.orders:
        saved = state.orders.get(order.order_ref)
        if saved and saved.capture_id and saved.content_hash != order.content_hash():
            raise SeedError(
                f"{order.order_ref} was edited after it was captured in the sandbox. "
                "Undo the edit, or give the changed order a new order_ref."
            )


# ---------------------------------------------------------------- the seeding run


@dataclass
class SeedReport:
    created: list[str] = field(default_factory=list)
    captured: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    rows_written: list[str] = field(default_factory=list)


ApprovalPrompt = Callable[[list[tuple[str, str]]], None]


async def run_seed(
    data: SeedData,
    paypal: PayPalClient,
    state_path: Path,
    session_factory: sessionmaker[Session],
    wait_for_approval: ApprovalPrompt,
    card: SandboxCard | None = None,
) -> SeedReport:
    """Create missing orders, get them paid, capture them, and write database rows.

    With `card`, PayPal takes each payment during the create call, so nobody has to
    approve anything. Without it, the buyer approves each order in a browser
    (wait_for_approval).
    """
    # Test card numbers only work in the sandbox. Never send one anywhere else.
    if card is not None and paypal.base_url != SANDBOX_BASE_URL:
        raise SeedError("sandbox test cards are only ever sent to the PayPal sandbox")
    state = load_state(state_path)
    check_no_edits_after_capture(data, state)
    report = SeedReport()

    for order in data.orders:
        saved = state.orders.get(order.order_ref)
        if saved and saved.capture_id:
            continue
        if saved is None or saved.paypal_order_id is None or saved.content_hash != order.content_hash():
            state.orders[order.order_ref] = await _create_order(paypal, order, saved, card)
            save_state(state, state_path)
            report.created.append(order.order_ref)

    pending = [o for o in data.orders if not state.orders[o.order_ref].capture_id]
    # Only browser-mode orders have an approval link. Card orders are already paid.
    to_approve = [(o.order_ref, url) for o in pending if (url := state.orders[o.order_ref].approve_url)]
    if to_approve:
        wait_for_approval(to_approve)

    for order in pending:
        try:
            await _settle_order(paypal, order, state.orders[order.order_ref], report)
        except PayPalRequestError as exc:
            # One refused order (e.g. a declined sandbox card) should not stop the others.
            report.problems.append(f"{order.order_ref}: PayPal refused: {exc.name} {exc.issues}")
        save_state(state, state_path)

    report.rows_written = write_rows(data, state, session_factory)
    return report


def restore_db(data: SeedData, state_path: Path, session_factory: sessionmaker[Session]) -> list[str]:
    """Rebuild database rows from the state file. No PayPal calls."""
    state = load_state(state_path)
    check_no_edits_after_capture(data, state)
    return write_rows(data, state, session_factory)


async def _create_order(
    paypal: PayPalClient, order: SeedOrder, saved: OrderState | None, card: SandboxCard | None
) -> OrderState:
    attempt = (saved.create_attempt + 1) if saved else 1
    # Deterministic: re-sending after a timeout returns the same PayPal order. With a
    # card, this call takes the payment, so the same id is what prevents a double charge
    # (PayPal keeps it for 6 hours). The content hash and attempt number make edited or
    # expired orders get a fresh one.
    request_id = f"seed-{order.order_ref}-{order.content_hash()}-{attempt}"
    response = await paypal.create_order(build_order_payload(order, card), request_id)

    order_id = response.get("id")
    if not isinstance(order_id, str) or not _PAYPAL_ID.fullmatch(order_id):
        raise SeedError(f"{order.order_ref}: PayPal's response had no usable order id")
    approve_url = None
    if card is None:
        approve_url = _approval_link(response)
        if not approve_url:
            raise SeedError(f"{order.order_ref}: PayPal's response had no approval link")
    return OrderState(
        content_hash=order.content_hash(),
        create_attempt=attempt,
        paypal_order_id=order_id,
        approve_url=approve_url,
    )


def _approval_link(response: dict[str, Any]) -> str | None:
    # With payment_source.paypal the link is "payer-action"; older flows call it "approve".
    for link in response.get("links") or []:
        if isinstance(link, dict) and link.get("rel") in ("payer-action", "approve"):
            href = link.get("href")
            if isinstance(href, str) and href.startswith("https://"):
                return href
    return None


async def _settle_order(paypal: PayPalClient, order: SeedOrder, saved: OrderState, report: SeedReport) -> None:
    """Capture the order if the buyer approved it, then record the capture in `saved`."""
    ref = order.order_ref
    assert saved.paypal_order_id is not None
    try:
        paypal_order = await paypal.get_order(saved.paypal_order_id)
    except PayPalRequestError as exc:
        if exc.status_code != 404:
            raise
        _forget_paypal_order(saved)
        report.problems.append(f"{ref}: PayPal no longer knows this order (expired?). Run again for a new link.")
        return

    # VERIFY: order status values, from PayPal's Orders v2 docs (not listed in the OpenAPI spec).
    status = paypal_order.get("status")
    paid_by_card = saved.approve_url is None
    if status == "PAYER_ACTION_REQUIRED" and paid_by_card:
        # The card wants 3D Secure, which nobody can complete from a script. Forget this
        # order so the next run creates a fresh one, e.g. with a different --card-number.
        _forget_paypal_order(saved)
        report.problems.append(
            f"{ref}: PayPal wants extra verification (3D Secure) for this card. "
            "Run again with another sandbox test card (--card-number)."
        )
        return
    if status in ("CREATED", "SAVED", "PAYER_ACTION_REQUIRED"):
        # Browser mode: not approved yet. Card mode: PayPal may still be processing (202).
        report.waiting.append(ref)
        return
    if status == "VOIDED":
        _forget_paypal_order(saved)
        report.problems.append(f"{ref}: order was voided. Run again for a new link.")
        return
    if status == "APPROVED":
        paypal_order = await paypal.capture_order(saved.paypal_order_id, f"seed-capture-{saved.paypal_order_id}")
    elif status != "COMPLETED":  # COMPLETED: a previous run already captured it
        report.problems.append(f"{ref}: unexpected order status {status!r}")
        return

    capture = _first_capture(paypal_order)
    problem = _record_capture(order, saved, capture)
    if problem and paid_by_card and capture and capture.get("status") in ("DECLINED", "FAILED"):
        # A declined test card never recovers on its own. Free the order so the next
        # run can create a new one, e.g. with another --card-number.
        _forget_paypal_order(saved)
        problem += " Run again with another sandbox test card (--card-number)."
    if problem:
        report.problems.append(f"{ref}: {problem}")
    else:
        report.captured.append(ref)


def _forget_paypal_order(saved: OrderState) -> None:
    saved.paypal_order_id = None
    saved.approve_url = None


def _first_capture(paypal_order: dict[str, Any]) -> dict[str, Any] | None:
    try:
        capture = paypal_order["purchase_units"][0]["payments"]["captures"][0]
    except (KeyError, IndexError, TypeError):
        return None
    return capture if isinstance(capture, dict) else None


def _record_capture(order: SeedOrder, saved: OrderState, capture: dict[str, Any] | None) -> str | None:
    """Store the capture if it is complete and for exactly the seed total. Returns a problem or None."""
    if capture is None:
        return "no capture yet (PayPal may still be processing it). Run again later."
    if capture.get("status") != "COMPLETED":
        return f"capture status is {capture.get('status')!r}, not COMPLETED. Run again later."

    capture_id = capture.get("id")
    money = capture.get("amount")
    if not isinstance(money, dict):
        return "capture has no readable amount"
    try:
        amount = Decimal(money.get("value"))
        captured_at = datetime.fromisoformat(capture["create_time"])
    except (InvalidOperation, TypeError, ValueError, KeyError):
        return "capture is missing a readable amount or create_time"
    if not isinstance(capture_id, str) or not _PAYPAL_ID.fullmatch(capture_id):
        return "capture has no usable id"
    if money.get("currency_code") != order.currency or amount != order.total:
        return f"captured {amount} {money.get('currency_code')} but expected {order.total} {order.currency}"
    if captured_at.tzinfo is None:
        return "capture create_time has no timezone"

    saved.capture_id = capture_id
    saved.captured_minor = to_minor(order.total, order.currency)
    saved.currency = order.currency
    saved.captured_at = captured_at.astimezone(timezone.utc)
    return None


# ---------------------------------------------------------------- database rows


def write_rows(data: SeedData, state: SeedState, session_factory: sessionmaker[Session]) -> list[str]:
    """Upsert customers and insert one row per captured order. Safe to run any number of times."""
    written: list[str] = []
    with session_factory() as db, db.begin():
        for c in data.customers:
            existing = db.get(Customer, c.id)
            if existing:
                existing.first_name, existing.email = c.first_name, c.email
            else:
                db.add(Customer(id=c.id, first_name=c.first_name, email=c.email))
        db.flush()

        for order in data.orders:
            saved = state.orders.get(order.order_ref)
            if not saved or not saved.capture_id:
                continue
            row = db.scalar(select(Order).where(Order.order_ref == order.order_ref))
            if row is not None:
                if row.capture_id != saved.capture_id:
                    raise SeedError(f"{order.order_ref}: database has a different capture than the state file")
                continue
            db.add(_order_row(order, saved))
            written.append(order.order_ref)
    return written


def _order_row(order: SeedOrder, saved: OrderState) -> Order:
    cur = order.currency
    items = [
        {"item_ref": i.item_ref, "name": i.name, "unit_minor": to_minor(i.unit_amount, cur), "quantity": i.quantity}
        for i in order.items
    ]
    shipping_minor = to_minor(order.shipping, cur)
    # Belt and braces: the stored parts must add up to what PayPal captured.
    if sum(i["unit_minor"] * i["quantity"] for i in items) + shipping_minor != saved.captured_minor:
        raise SeedError(f"{order.order_ref}: items + shipping do not add up to the captured amount")
    assert saved.paypal_order_id and saved.capture_id and saved.captured_at and saved.captured_minor
    return Order(
        customer_id=order.customer_id,
        order_ref=order.order_ref,
        status=order.status,
        placed_at=order.placed_at,
        paypal_order_id=saved.paypal_order_id,
        capture_id=saved.capture_id,
        currency=cur,
        captured_minor=saved.captured_minor,
        items_json=items,
        shipping_minor=shipping_minor,
        captured_at=saved.captured_at,
    )
