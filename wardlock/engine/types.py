"""The engine's inputs and output: plain frozen dataclasses, no database objects.

The caller (P5's executor) gathers every fact from the database and passes it in;
the engine never fetches anything itself. That keeps every rule testable with plain
fixtures and makes a decision reproducible from its inputs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from wardlock.engine.codes import Code
from wardlock.enums import HandleKind, HandleState, ReasonCode, SessionStatus, Verdict

Tool = Literal["request_refund", "request_refund_raw"]


@dataclass(frozen=True)
class RefundRequest:
    """What the agent asked for. Everything in it is untrusted."""

    tool: Tool
    reason_code: ReasonCode
    handle_ids: tuple[str, ...] = ()
    # Compat tool only (SPEC §3.4). request_refund never accepts these (invariant 2).
    raw_capture_id: str | None = None
    raw_amount: Decimal | None = None  # None means the agent sent no amount
    raw_currency: str | None = None


@dataclass(frozen=True)
class SessionView:
    session_id: str
    customer_id: str
    status: SessionStatus
    expires_at: datetime
    refunds_so_far: int  # successful refunds already made in this session (ledger)
    # Total already refunded AUTOMATICALLY (verdict ALLOW) in this session, in the policy
    # currency. Refunds a person approved from a hold don't count. There's no default on
    # purpose: forgetting it would silently weaken the limit.
    auto_refunded_minor: int


@dataclass(frozen=True)
class HandleView:
    handle_id: str
    session_id: str
    capture_id: str
    kind: HandleKind
    state: HandleState
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class OrderView:
    capture_id: str
    customer_id: str  # re-read from the orders table at decision time (check 6)
    currency: str
    captured_at: datetime
    remaining_minor: int  # captured - refunded - in flight (handles.remaining_refundable_minor)
    # Compat only: amount in minor units -> the handle kinds that produce exactly that amount.
    derivable: Mapping[int, frozenset[HandleKind]] = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionInputs:
    session: SessionView | None  # None: the session could not be found
    handles: Mapping[str, HandleView]  # the requested handles that exist, from ANY session
    orders: Mapping[str, OrderView]  # keyed by capture_id, for every capture involved
    dependency_errors: tuple[str, ...] = ()  # anything that failed while gathering these facts


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason_codes: tuple[Code, ...]  # in SPEC §5 table order
    amount_minor: int | None
    currency: str | None
    capture_id: str | None
    handle_ids: tuple[str, ...]  # unique, in request order: what the executor reserves
    policy_version: str
    # True until apply_reason_check() has run on an ALLOW. The executor must refuse to
    # execute a decision that still needs it.
    needs_reason_check: bool
