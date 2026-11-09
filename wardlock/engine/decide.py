"""decide(): run every check, collect all reason codes, return ALLOW / HOLD / BLOCK (SPEC §5).

Pure: the same inputs always give the same decision. The LLM reason check (#12) is I/O,
so it runs outside decide(), and apply_reason_check() can only make a decision stricter.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from wardlock.engine.checks import COMPAT_CHECKS, HANDLE_CHECKS, Facts
from wardlock.engine.codes import BLOCK_CODES, HOLD_CODES, Code
from wardlock.engine.policy import Policy
from wardlock.engine.types import Decision, DecisionInputs, RefundRequest
from wardlock.enums import Verdict
from wardlock.money import to_minor


def decide(request: RefundRequest, inputs: DecisionInputs, policy: Policy, now: datetime) -> Decision:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    facts = _facts(request, inputs, policy, now)
    checks = COMPAT_CHECKS if facts.is_compat else HANDLE_CHECKS

    codes: list[Code] = []
    for check in checks:
        codes.extend(code for code in check(facts) if code not in codes)
    # Defensive: an ALLOW must always know exactly what it would refund. If it doesn't,
    # something upstream is wrong, so fail closed (invariant 5).
    if not codes and (facts.amount_minor is None or facts.order is None):
        codes.append(Code.DEPENDENCY_UNAVAILABLE)

    verdict = _verdict(codes)
    return Decision(
        verdict=verdict,
        reason_codes=tuple(codes),
        amount_minor=facts.amount_minor,
        currency=facts.currency,
        capture_id=facts.order.capture_id if facts.order else None,
        handle_ids=facts.unique_ids,
        policy_version=policy.version,
        needs_reason_check=verdict == Verdict.ALLOW,
    )


def apply_reason_check(decision: Decision, consistent: bool | None) -> Decision:
    """Check #12. The LLM may only turn ALLOW into HOLD, never approve or unblock (invariant 6).

    `consistent` must be exactly True to keep an ALLOW. False, None (timeout or error) or
    anything else means HOLD.
    """
    if decision.verdict != Verdict.ALLOW or consistent is True:
        return replace(decision, needs_reason_check=False)
    return replace(
        decision,
        verdict=Verdict.HOLD,
        reason_codes=decision.reason_codes + (Code.REASON_NOT_SUPPORTED_BY_TICKET,),
        needs_reason_check=False,
    )


def apply_forced_review(decision: Decision) -> Decision:
    """Config B of the eval (SPEC §14, "blanket approval"): every ALLOW becomes a HOLD.

    Like the reason check, it can only escalate. BLOCK and HOLD are returned unchanged.
    """
    if decision.verdict != Verdict.ALLOW:
        return decision
    return replace(decision, verdict=Verdict.HOLD,
                   reason_codes=decision.reason_codes + (Code.MANUAL_REVIEW_REQUIRED,))


def _verdict(codes: list[Code]) -> Verdict:
    if any(code in BLOCK_CODES for code in codes):
        return Verdict.BLOCK
    if any(code in HOLD_CODES for code in codes):
        return Verdict.HOLD
    return Verdict.ALLOW


def _facts(request: RefundRequest, inputs: DecisionInputs, policy: Policy, now: datetime) -> Facts:
    is_compat = request.tool == "request_refund_raw"
    unique_ids = tuple(dict.fromkeys(request.handle_ids))  # removes repeats, keeps order
    handles = tuple(inputs.handles[h] for h in unique_ids if h in inputs.handles)

    if is_compat:
        captures = (request.raw_capture_id,) if request.raw_capture_id else ()
    else:
        captures = tuple(dict.fromkeys(h.capture_id for h in handles))
    order = inputs.orders.get(captures[0]) if len(captures) == 1 else None
    # A compat order only counts once it is proven to be the session customer's.
    if is_compat and order is not None and (inputs.session is None or order.customer_id != inputs.session.customer_id):
        order = None

    if is_compat:
        amount, currency = _raw_amount(request, order), (order.currency if order else None)
        kinds = order.derivable.get(amount, frozenset()) if order and amount is not None else frozenset()
    else:
        amount = sum(h.amount_minor for h in handles) if handles else None  # unique handles only
        currency = handles[0].currency if handles else None
        kinds = frozenset(h.kind for h in handles)

    return Facts(
        request=request, inputs=inputs, policy=policy, now=now, is_compat=is_compat,
        handles=handles, unique_ids=unique_ids, captures=captures, order=order,
        amount_minor=amount, currency=currency, kinds=kinds,
    )


def _raw_amount(request: RefundRequest, order) -> int | None:
    """The compat amount in minor units, or None if it is missing or invalid for this order."""
    if order is None or request.raw_amount is None or request.raw_currency != order.currency:
        return None
    try:
        minor = to_minor(request.raw_amount, order.currency)
    except (ValueError, TypeError):
        return None
    return minor if minor > 0 else None
