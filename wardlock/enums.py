"""Plain enums shared by the database models, the decision engine and the agent tools.

They live here, not in models.py, so the pure engine can use them without importing
SQLAlchemy (SPEC §5: the engine has no database dependency at all).
"""

import enum


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


class ReasonCode(enum.StrEnum):
    """Why the agent says a refund is due (SPEC §3.2). policy.yaml maps each to the allowed handle kinds."""

    DAMAGED = "DAMAGED"
    WRONG_ITEM = "WRONG_ITEM"
    NOT_RECEIVED = "NOT_RECEIVED"
    LATE_DELIVERY = "LATE_DELIVERY"
    GOODWILL = "GOODWILL"
