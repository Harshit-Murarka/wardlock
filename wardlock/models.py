"""SQLAlchemy models for every table in SPEC §9.

- Money is always integer minor units (cents), never float (SPEC §0 invariant 3).
- Timestamps are timezone-aware UTC (see UTCDateTime).
- Primary keys are short prefixed random strings such as "dec_x7Q...". They are easy
  to read in the console and impossible to guess.
"""

from __future__ import annotations

import enum
import secrets
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Dialect,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def new_handle_id() -> str:
    # SPEC §4: "h_" + token_urlsafe(16). An agent cannot guess a valid handle.
    return "h_" + secrets.token_urlsafe(16)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores UTC and always returns timezone-aware UTC datetimes.

    SQLite has no timezone support and hands back naive datetimes. Comparing a naive
    datetime with an aware one raises TypeError, which would break the 180-day check
    and session expiry. Naive input is rejected, because its timezone is unknown.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime: pass a timezone-aware UTC datetime (models.utcnow())")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:  # SQLite: stored as UTC wall time, see process_bind_param
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


# ---------------------------------------------------------------- enums


class OrderStatus(enum.StrEnum):
    PROCESSING = "PROCESSING"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"


class SessionStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"


class HandleKind(enum.StrEnum):
    LINE_ITEM = "LINE_ITEM"
    SHIPPING = "SHIPPING"
    GOODWILL_PCT = "GOODWILL_PCT"
    FULL_REMAINING = "FULL_REMAINING"


class HandleState(enum.StrEnum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"


class Verdict(enum.StrEnum):
    ALLOW = "ALLOW"
    HOLD = "HOLD"
    BLOCK = "BLOCK"


class HoldStatus(enum.StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"


class AlertKind(enum.StrEnum):
    OUT_OF_BAND_REFUND = "OUT_OF_BAND_REFUND"
    CAPTURE_REVERSED = "CAPTURE_REVERSED"


def _enum(enum_class: type[enum.StrEnum]) -> Enum:
    # Stored as VARCHAR plus a CHECK constraint, which works on both SQLite and Postgres.
    # A misspelled state is rejected by Python (validate_strings) and by the database.
    return Enum(
        enum_class,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=32,
        name=f"ck_{enum_class.__name__.lower()}",
    )


# ---------------------------------------------------------------- tables


class Base(DeclarativeBase):
    pass


def _parent():
    """A many-to-one link to the parent row.

    These relationships are not used for reading. SQLAlchemy relies on them to INSERT
    parents before children when both are saved in one flush; without them it may
    insert a hold before its decision and break the foreign key. lazy="raise" makes an
    accidental attribute access fail loudly instead of running a hidden query.
    """
    return relationship(lazy="raise")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # from seed data, e.g. "cust_ana"
    first_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(254), unique=True)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("captured_minor > 0", name="ck_orders_captured_positive"),
        CheckConstraint("shipping_minor >= 0", name="ck_orders_shipping_not_negative"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ord"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    customer: Mapped[Customer] = _parent()
    order_ref: Mapped[str] = mapped_column(String(32), unique=True)  # customer-facing, e.g. "A-1001"
    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus))
    placed_at: Mapped[date] = mapped_column(Date)  # display only; refund windows use captured_at
    paypal_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    capture_id: Mapped[str] = mapped_column(String(64), unique=True)  # never shown to the agent
    currency: Mapped[str] = mapped_column(String(3))
    captured_minor: Mapped[int] = mapped_column(Integer)
    # [{"item_ref": "bell", "name": "Bike bell", "unit_minor": 400, "quantity": 1}]
    items_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    shipping_minor: Mapped[int] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime())


class AgentSession(Base):
    """One support conversation, bound to ONE verified customer (SPEC §10).

    Named AgentSession so it is not confused with SQLAlchemy's database Session.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ses"))
    ticket_id: Mapped[str] = mapped_column(String(64))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    customer: Mapped[Customer] = _parent()
    # UNTRUSTED customer text. Used only by the reason check and the replay page,
    # never for ownership or amounts.
    ticket_body: Mapped[str] = mapped_column(Text)
    # SHA-256 hex of the session token. The token itself is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[SessionStatus] = mapped_column(_enum(SessionStatus), default=SessionStatus.ACTIVE)

    def is_live(self, now: datetime) -> bool:
        """True while the session may still be used: ACTIVE and not past its expiry."""
        return self.status == SessionStatus.ACTIVE and now < self.expires_at


class Handle(Base):
    """A refund option the agent may pick (SPEC §4). The amount was computed by Wardlock."""

    __tablename__ = "handles"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_handles_amount_positive"),
        CheckConstraint(
            "goodwill_pct IS NULL OR (goodwill_pct > 0 AND goodwill_pct <= 100)",
            name="ck_handles_goodwill_pct_range",
        ),
    )

    handle_id: Mapped[str] = mapped_column(String(40), primary_key=True, default=new_handle_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    agent_session: Mapped[AgentSession] = _parent()
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    order: Mapped[Order] = _parent()
    capture_id: Mapped[str] = mapped_column(String(64))  # re-checked against orders at decision time
    kind: Mapped[HandleKind] = mapped_column(_enum(HandleKind))
    item_ref: Mapped[str | None] = mapped_column(String(32))  # LINE_ITEM only
    goodwill_pct: Mapped[int | None] = mapped_column(Integer)  # GOODWILL_PCT only
    label: Mapped[str] = mapped_column(String(200))  # shown to the agent, e.g. "Bike bell x1"
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    state: Mapped[HandleState] = mapped_column(_enum(HandleState), default=HandleState.AVAILABLE)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Decision(Base):
    """One refund tool call and the engine's verdict (SPEC §5–6)."""

    __tablename__ = "decisions"
    __table_args__ = (
        CheckConstraint("amount_minor IS NULL OR amount_minor > 0", name="ck_decisions_amount_positive"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("dec"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    agent_session: Mapped[AgentSession] = _parent()
    tool: Mapped[str] = mapped_column(String(40))  # request_refund | request_refund_raw
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON)  # what the agent sent (untrusted)
    verdict: Mapped[Verdict] = mapped_column(_enum(Verdict))
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    capture_id: Mapped[str | None] = mapped_column(String(64))  # needed by webhook reconciliation
    amount_minor: Mapped[int | None] = mapped_column(Integer)  # None if blocked before computing
    currency: Mapped[str | None] = mapped_column(String(3))
    # The PayPal-Request-Id. Saved BEFORE PayPal is called (invariant 4). NULL for BLOCKs;
    # NULLs do not clash with the unique constraint.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    policy_version: Mapped[str] = mapped_column(String(32))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Hold(Base):
    """A refund waiting for the merchant (SPEC §6). Approving runs the frozen request exactly."""

    __tablename__ = "holds"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("hold"))
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), unique=True)
    decision: Mapped[Decision] = _parent()
    # capture_id, amount, currency, idempotency_key and note: exactly what will be sent.
    frozen_request_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[HoldStatus] = mapped_column(_enum(HoldStatus), default=HoldStatus.PENDING)
    approver: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)  # for HOLD_TIMEOUT
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class LedgerEntry(Base):
    """A refund that PayPal confirmed (SPEC §6, §8)."""

    __tablename__ = "ledger"
    __table_args__ = (CheckConstraint("amount_minor > 0", name="ck_ledger_amount_positive"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("rf"))
    # Unique: one refund per decision, and one row per PayPal refund. Whichever of the
    # executor and the webhook handler writes second is rejected (SPEC §8).
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), unique=True)
    decision: Mapped[Decision] = _parent()
    capture_id: Mapped[str] = mapped_column(String(64), index=True)
    paypal_refund_id: Mapped[str] = mapped_column(String(64), unique=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20))  # PayPal refund status, e.g. COMPLETED
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(100), primary_key=True)  # duplicates are ignored
    event_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    verified: Mapped[bool] = mapped_column(Boolean)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("alr"))
    kind: Mapped[AlertKind] = mapped_column(_enum(AlertKind))
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
