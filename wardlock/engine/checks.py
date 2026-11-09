"""One small function per check in the SPEC §5 table.

Every check reads the precomputed Facts and returns the codes it raises (an empty list
when the check passes). A check whose inputs are missing returns nothing rather than
guessing: for example, the remaining-amount check skips a request that spans two
captures, which is BLOCKed by the combination check anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from wardlock.engine.codes import Code
from wardlock.engine.policy import Policy
from wardlock.engine.types import DecisionInputs, HandleView, OrderView, RefundRequest
from wardlock.enums import HandleKind, HandleState, SessionStatus
from wardlock.money import to_minor

# The largest amount any money column can store (Postgres INTEGER, about 21.4 million USD).
# A raw amount above it is invalid: it could not even be written to the audit log.
MAX_AMOUNT_MINOR = 2_147_483_647

MAX_HANDLES_PER_REQUEST = 10
# PayPal only allows refunds within 180 days of the capture. This is PayPal's rule,
# not a merchant policy, so it is a constant here and not in policy.yaml.
REFUND_WINDOW = timedelta(days=180)


@dataclass(frozen=True)
class Facts:
    """Everything derived once from the request and inputs, shared by all checks."""

    request: RefundRequest
    inputs: DecisionInputs
    policy: Policy
    now: datetime
    is_compat: bool
    handles: tuple[HandleView, ...]  # requested handles that exist, unique, in request order
    unique_ids: tuple[str, ...]  # unique requested ids, in request order
    captures: tuple[str, ...]  # distinct captures involved, in order
    order: OrderView | None  # the order, when exactly one capture is involved and known
    amount_minor: int | None
    currency: str | None
    kinds: frozenset[HandleKind]  # kinds requested (compat: kinds that produce the raw amount)


# ---------------------------------------------------------------- shared checks


def check_session(f: Facts) -> list[Code]:
    """#1 The session exists, is ACTIVE and has not expired."""
    s = f.inputs.session
    if s is None or s.status != SessionStatus.ACTIVE or f.now >= s.expires_at:
        return [Code.SESSION_INVALID]
    return []


def check_tool_inputs(f: Facts) -> list[Code]:
    """Each tool takes only its own inputs. request_refund never takes an amount (invariant 2)."""
    r = f.request
    has_raw = r.raw_capture_id is not None or r.raw_amount is not None or r.raw_currency is not None
    if (not f.is_compat and has_raw) or (f.is_compat and r.handle_ids):
        return [Code.INVALID_COMBINATION]
    return []


def check_owner(f: Facts) -> list[Code]:
    """#6 Every order involved belongs to the session's customer, re-read at decision time (G1)."""
    s = f.inputs.session
    if s is None:
        return []
    if f.is_compat and not f.captures:
        return [Code.NOT_OWNER]  # no capture given at all: nothing here is provably theirs
    for capture in f.captures:
        order = f.inputs.orders.get(capture)
        if order is None:
            # Compat: an unknown capture counts as "not yours", so its existence is never revealed.
            # Handles: missing order data is a dependency problem (check #13).
            if f.is_compat:
                return [Code.NOT_OWNER]
            continue
        if order.customer_id != s.customer_id:
            return [Code.NOT_OWNER]
    return []


def check_refundable(f: Facts) -> list[Code]:
    """#7 The amount is no more than what is left to refund on the capture."""
    if f.order is None or f.amount_minor is None:
        return []
    return [Code.EXCEEDS_REFUNDABLE] if f.amount_minor > f.order.remaining_minor else []


def check_window(f: Facts) -> list[Code]:
    """#8 Every capture involved is at most 180 days old."""
    for capture in f.captures:
        order = f.inputs.orders.get(capture)
        if order is not None and f.now - order.captured_at > REFUND_WINDOW:
            return [Code.REFUND_WINDOW_EXPIRED]
    return []


def check_reason_kinds(f: Facts) -> list[Code]:
    """#9 The stated reason allows these kinds of refund (policy matrix)."""
    if not f.kinds:
        return []
    allowed = f.policy.reason_allows.get(f.request.reason_code, frozenset())
    if f.is_compat:
        # A raw amount may match several kinds; one allowed kind is enough.
        return [] if f.kinds & allowed else [Code.REASON_KIND_MISMATCH]
    return [] if f.kinds <= allowed else [Code.REASON_KIND_MISMATCH]


def check_auto_limit(f: Facts) -> list[Code]:
    """#10 The session's automatic total stays within the limit, in the limit's own currency.

    Counting earlier automatic refunds stops "salami slicing": two 49.99 refunds would
    each pass a per-refund check, but together they are 99.98.
    """
    if f.amount_minor is None:
        return []
    earlier = f.inputs.session.auto_refunded_minor if f.inputs.session is not None else 0
    if f.currency != f.policy.currency or f.amount_minor + earlier > f.policy.auto_approve_limit_minor:
        return [Code.OVER_AUTO_LIMIT]
    return []


def check_velocity(f: Facts) -> list[Code]:
    """#11 The session has made fewer refunds than the policy allows."""
    s = f.inputs.session
    if s is not None and s.refunds_so_far >= f.policy.max_refunds_per_session:
        return [Code.SESSION_VELOCITY]
    return []


def check_dependencies(f: Facts) -> list[Code]:
    """#13 Every fact could be gathered. A missing order for a known handle counts too (fail closed)."""
    if f.inputs.dependency_errors:
        return [Code.DEPENDENCY_UNAVAILABLE]
    if not f.is_compat and any(c not in f.inputs.orders for c in f.captures):
        return [Code.DEPENDENCY_UNAVAILABLE]
    return []


# ---------------------------------------------------------------- handle checks (request_refund)


def check_handles_exist(f: Facts) -> list[Code]:
    """#2 At least one handle, and every handle exists."""
    ids = f.request.handle_ids
    if not ids or any(h not in f.inputs.handles for h in ids):
        return [Code.UNKNOWN_HANDLE]
    return []


def check_handles_session(f: Facts) -> list[Code]:
    """#3 Every handle was minted in THIS session."""
    s = f.inputs.session
    if s is not None and any(h.session_id != s.session_id for h in f.handles):
        return [Code.HANDLE_OTHER_SESSION]
    return []


def check_handles_unused(f: Facts) -> list[Code]:
    """#4 No handle is reserved, consumed or expired."""
    if any(h.state != HandleState.AVAILABLE for h in f.handles):
        return [Code.HANDLE_ALREADY_USED]
    return []


def check_combination(f: Facts) -> list[Code]:
    """#5 One capture per request, a full refund alone, at most one goodwill, no repeats,
    and at most one option per line item (pick "x2", not "x1" twice)."""
    ids = f.request.handle_ids
    kinds = [h.kind for h in f.handles]
    items = [h.item_ref for h in f.handles if h.kind == HandleKind.LINE_ITEM]
    broken = (
        len(ids) != len(set(ids))  # the same handle twice would double its amount
        or len(ids) > MAX_HANDLES_PER_REQUEST
        or len(f.captures) > 1
        or len({h.currency for h in f.handles}) > 1
        or (HandleKind.FULL_REMAINING in kinds and len(kinds) > 1)
        or kinds.count(HandleKind.GOODWILL_PCT) > 1
        or len(items) != len(set(items))
    )
    return [Code.INVALID_COMBINATION] if broken else []


# ---------------------------------------------------------------- compat checks (request_refund_raw)


def check_compat_amount(f: Facts) -> list[Code]:
    """Compat: an amount was given and is a valid positive amount in the capture's currency."""
    r = f.request
    if r.raw_amount is None:
        return [Code.MISSING_AMOUNT]
    try:
        valid = 0 < to_minor(r.raw_amount, r.raw_currency) <= MAX_AMOUNT_MINOR  # raises for bad values
    except (ValueError, TypeError):
        valid = False
    order = f.inputs.orders.get(r.raw_capture_id or "")
    if not valid or (order is not None and r.raw_currency != order.currency):
        return [Code.INVALID_AMOUNT]
    return []


def check_compat_derivable(f: Facts) -> list[Code]:
    """Compat: the amount equals exactly one option Wardlock would offer for this capture."""
    if f.order is None or f.amount_minor is None:
        return []
    return [] if f.amount_minor in f.order.derivable else [Code.AMOUNT_NOT_DERIVABLE]


# Run in SPEC §5 table order, so decisions list their codes in that order.
HANDLE_CHECKS = (
    check_session, check_tool_inputs, check_handles_exist, check_handles_session, check_handles_unused,
    check_combination, check_owner, check_refundable, check_window, check_reason_kinds,
    check_auto_limit, check_velocity, check_dependencies,
)
COMPAT_CHECKS = (
    check_session, check_tool_inputs, check_owner, check_compat_amount, check_refundable, check_window,
    check_reason_kinds, check_auto_limit, check_velocity, check_compat_derivable, check_dependencies,
)
