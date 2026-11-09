"""Run refund requests end to end, and settle holds (SPEC §6).

For each request:
    gather facts -> decide (pure engine) -> reason check (only on ALLOW) ->
    save the decision with its idempotency key and reserve the handles, COMMIT ->
    call PayPal -> record the outcome.

The decision and key are saved before PayPal is called (SPEC §0 invariant 4), so a crash or
a timeout can never lose track of a refund. Any retry or later approval re-sends the SAME
key, and PayPal never refunds twice for one key.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from wardlock.engine.codes import EXPLANATIONS, Code
from wardlock.engine.decide import apply_forced_review, apply_reason_check, decide
from wardlock.engine.policy import Policy
from wardlock.engine.recheck import HoldFacts, recheck_approval
from wardlock.engine.types import Decision as EngineDecision
from wardlock.engine.types import DecisionInputs, HandleView, OrderView, RefundRequest, SessionView
from wardlock.enums import HandleKind, HandleState, HoldStatus, ReasonCode, Verdict
from wardlock.handles import (
    MAX_HANDLES_PER_REQUEST,
    NOT_REFUNDED_STATUSES,
    HandleConflict,
    _remaining,
    derivable_amounts,
    load_handles,
    refund_candidates,
    reserve_handles,
    reserve_new_handle,
)
from wardlock.models import AgentSession, Decision, Handle, Hold, LedgerEntry, Order, utcnow
from wardlock.money import from_minor, to_minor
from wardlock.paypal.client import PayPalClient, Refund
from wardlock.paypal.errors import PayPalAuthError, PayPalRequestError
from wardlock.reason_check import ReasonCheckResult

logger = logging.getLogger(__name__)

MAX_NOTE_CHARS = 200  # SPEC §3.2 note_to_customer
# A request still unresolved after this long means the process died mid-call. It is longer
# than the PayPal client's worst case (4 attempts, backoff and a token fetch).
STUCK_AFTER = timedelta(minutes=5)
SUCCESS_STATUSES = ("COMPLETED", "PENDING")  # PENDING: money is on its way, so it counts as refunded

ReasonChecker = Callable[[ReasonCode, Sequence[str], str], Awaitable[ReasonCheckResult]]
Status = Literal["ALLOWED", "HELD", "BLOCKED"]


@dataclass(frozen=True)
class RefundOutcome:
    """What the agent hears back (SPEC §3.2). Deliberately has no PayPal IDs (invariant 7)."""

    status: Status
    decision_id: str | None
    amount: Decimal | None
    currency: str | None
    refund_ref: str | None  # ledger id (rf_...), when money moved
    hold_id: str | None
    reason_codes: tuple[str, ...]
    message_for_agent: str


@dataclass(frozen=True)
class HoldOutcome:
    hold_id: str
    status: str  # the hold's status after the action
    executed: bool  # True only when this call made PayPal refund
    refund_ref: str | None
    reason_codes: tuple[str, ...]
    message: str


@dataclass
class _Gathered:
    inputs: DecisionInputs
    session: AgentSession | None
    ticket_body: str = ""
    labels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Saved:
    """A committed decision that is about to be sent to PayPal."""

    decision_id: str
    verdict: Verdict
    outcome: RefundOutcome | None  # set when nothing more needs to happen
    frozen: dict[str, Any] = field(default_factory=dict)


class Executor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        paypal: PayPalClient,
        policy: Policy,
        reason_checker: ReasonChecker,
        *,
        hold_timeout: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] = utcnow,
        force_hold: bool = False,
    ) -> None:
        self._force_hold = force_hold  # eval config B only: a person approves every refund
        self._sessions = session_factory
        self._paypal = paypal
        self._policy = policy
        self._reason_checker = reason_checker
        self._hold_timeout = hold_timeout
        self._clock = clock

    # ================================================================ agent requests

    async def request_refund(
        self, session_id: str, handle_ids: Sequence[str], reason_code: ReasonCode, note: str = ""
    ) -> RefundOutcome:
        request = RefundRequest("request_refund", reason_code, tuple(handle_ids))
        return await self._run(session_id, request, note)

    async def request_refund_raw(
        self,
        session_id: str,
        capture_id: str | None,
        amount: Decimal | None,
        currency: str | None,
        reason_code: ReasonCode,
        note: str = "",
    ) -> RefundOutcome:
        request = RefundRequest(
            "request_refund_raw", reason_code, (),
            raw_capture_id=capture_id, raw_amount=amount, raw_currency=currency,
        )
        return await self._run(session_id, request, note)

    async def _run(self, session_id: str, request: RefundRequest, note: str) -> RefundOutcome:
        started = time.monotonic()
        now = self._clock()
        note = clean_note(note)

        with self._sessions() as db:
            gathered = self._gather(db, session_id, request)
        if gathered.session is None:
            # Nothing to attach a decision row to (decisions need a session). Callers resolve
            # the token first, so this only happens with a bad session id.
            return RefundOutcome("BLOCKED", None, None, None, None, None, (Code.SESSION_INVALID.value,),
                                 EXPLANATIONS[Code.SESSION_INVALID])

        decision = decide(request, gathered.inputs, self._policy, now)
        details: dict[str, Any] = {}
        if decision.needs_reason_check:
            result = await self._reason_checker(request.reason_code, gathered.labels, gathered.ticket_body)
            decision = apply_reason_check(decision, result.consistent)  # can only escalate (invariant 6)
            details["reason_check"] = {"consistent": result.consistent, "rationale": result.rationale,
                                       "error": result.error}
        if self._force_hold:
            decision = apply_forced_review(decision)

        latency_ms = int((time.monotonic() - started) * 1000)
        saved = self._save(gathered.session.id, request, decision, note, details, latency_ms, now)
        if saved.outcome is not None:
            return saved.outcome
        return await self._send_new(saved)

    # ---------------------------------------------------------------- gathering facts

    def _gather(self, db: Session, session_id: str, request: RefundRequest) -> _Gathered:
        session = db.get(AgentSession, session_id)
        if session is None:
            return _Gathered(DecisionInputs(None, {}, {}), None)
        gathered = _Gathered(DecisionInputs(None, {}, {}), session, ticket_body=session.ticket_body)
        errors: list[str] = []
        session_view, handles, orders = None, {}, {}
        try:
            session_view = self._session_view(db, session)
            handles, gathered.labels = self._handle_views(db, request)
            orders = self._order_views(db, session, request, handles)
            if request.tool == "request_refund_raw":
                gathered.labels = self._compat_labels(db, session, request)
        except SQLAlchemyError as exc:
            # A failed read must never turn into an ALLOW: report it, and the engine holds (#13).
            logger.warning("gathering facts failed: %s", type(exc).__name__)
            errors.append(f"database error ({type(exc).__name__})")
        gathered.inputs = DecisionInputs(session_view, handles, orders, tuple(errors))
        return gathered

    def _session_view(self, db: Session, session: AgentSession) -> SessionView:
        rows = db.execute(
            select(LedgerEntry.amount_minor, LedgerEntry.currency, Decision.verdict)
            .join(Decision, LedgerEntry.decision_id == Decision.id)
            .where(Decision.session_id == session.id, LedgerEntry.status.not_in(NOT_REFUNDED_STATUSES))
        ).all()
        auto = sum(amount for amount, currency, verdict in rows
                   if verdict == Verdict.ALLOW and currency == self._policy.currency)
        return SessionView(session.id, session.customer_id, session.status, session.expires_at,
                           refunds_so_far=len(rows), auto_refunded_minor=auto)

    def _handle_views(self, db: Session, request: RefundRequest) -> tuple[dict[str, HandleView], list[str]]:
        ids = request.handle_ids
        if request.tool != "request_refund" or not 1 <= len(set(ids)) <= MAX_HANDLES_PER_REQUEST:
            return {}, []  # the engine reports empty or oversized requests itself
        try:
            rows = load_handles(db, ids)
        except ValueError:
            return {}, []  # e.g. non-string ids: nothing to load, so the engine says UNKNOWN_HANDLE
        views = {h.handle_id: HandleView(h.handle_id, h.session_id, h.capture_id, h.kind, h.state,
                                         h.amount_minor, h.currency, h.item_ref) for h in rows.values()}
        labels = [rows[h].label for h in dict.fromkeys(ids) if h in rows]
        return views, labels

    def _order_views(self, db, session, request, handles) -> dict[str, OrderView]:
        if request.tool == "request_refund_raw":
            captures = {request.raw_capture_id} if isinstance(request.raw_capture_id, str) else set()
        else:
            captures = {h.capture_id for h in handles.values()}
        views = {}
        for capture in captures:
            order = db.scalar(select(Order).where(Order.capture_id == capture))
            if order is None:
                continue
            # Only work out derivable amounts for the session customer's own orders.
            derivable = (derivable_amounts(db, order, self._policy.goodwill_percentages)
                         if request.tool == "request_refund_raw" and order.customer_id == session.customer_id else {})
            views[capture] = OrderView(capture, order.customer_id, order.currency, order.captured_at,
                                       _remaining(db, order), derivable)
        return views

    def _compat_labels(self, db, session, request) -> list[str]:
        order = self._own_order(db, session, request.raw_capture_id)
        amount = _raw_minor(request, order)
        if order is None or amount is None:
            return []
        labels = [c[3] for c in refund_candidates(db, order, self._policy.goodwill_percentages) if c[4] == amount]
        return labels or [f"Custom amount {from_minor(amount, order.currency)} {order.currency}"]

    @staticmethod
    def _own_order(db, session, capture_id) -> Order | None:
        if not isinstance(capture_id, str):
            return None
        order = db.scalar(select(Order).where(Order.capture_id == capture_id))
        return order if order is not None and order.customer_id == session.customer_id else None

    # ---------------------------------------------------------------- saving (before PayPal)

    def _save(self, session_id, request, decision: EngineDecision, note, details, latency_ms, now) -> _Saved:
        """One transaction: decision row, idempotency key, reservation and any hold. Then COMMIT."""
        with self._sessions() as db, db.begin():
            session = db.get(AgentSession, session_id)
            verdict, codes = decision.verdict, list(decision.reason_codes)
            row = Decision(
                session_id=session_id, tool=request.tool, request_json=_request_json(request, note),
                verdict=verdict, reason_codes=[c.value for c in codes], capture_id=decision.capture_id,
                amount_minor=decision.amount_minor, currency=decision.currency,
                policy_version=decision.policy_version, latency_ms=latency_ms, details_json=details,
                created_at=now,  # the executor's clock, so sweeps and tests agree on ages
            )
            db.add(row)
            db.flush()

            if verdict in (Verdict.ALLOW, Verdict.HOLD):
                try:
                    reserved = self._reserve(db, session, request, decision, now)
                except HandleConflict:
                    # Another request took a handle after decide(): block and reserve nothing.
                    verdict = Verdict.BLOCK
                    codes.append(Code.HANDLE_ALREADY_USED)
                    row.verdict, row.reason_codes = verdict, [c.value for c in codes]
                else:
                    # When order data was missing the engine knows no order, but the handles still
                    # name their capture: take it from them, so the hold can run once data is back.
                    capture_id = decision.capture_id or _single_capture(db, reserved)
                    if capture_id is None or decision.amount_minor is None or decision.currency is None:
                        # A request we could never send must not wait for approval: block it (fail closed).
                        _release(db, reserved, now)
                        codes.append(Code.DEPENDENCY_UNAVAILABLE)
                        row.verdict, row.reason_codes = Verdict.BLOCK, [c.value for c in codes]
                        return _Saved(row.id, Verdict.BLOCK, _blocked(row.id, codes))
                    row.capture_id = capture_id
                    row.idempotency_key = str(uuid.uuid4())  # saved BEFORE PayPal is called (invariant 4)
                    row.details_json = {**details, "reserved_handles": reserved}
                    frozen = _frozen(row, decision, reserved, note)
                    if verdict == Verdict.HOLD:
                        hold = Hold(decision_id=row.id, frozen_request_json=frozen, created_at=now)
                        db.add(hold)
                        db.flush()
                        return _Saved(row.id, verdict, _held(row, decision.amount_minor, hold.id, codes))
                    return _Saved(row.id, verdict, None, frozen)

            return _Saved(row.id, verdict, _blocked(row.id, codes))

    def _reserve(self, db, session, request, decision: EngineDecision, now) -> list[str]:
        if request.tool == "request_refund":
            reserve_handles(db, session.id, decision.handle_ids)
            return list(decision.handle_ids)
        # Compat: create a reserved handle copied from the matching option, so the item is
        # marked spent and the amount counts as in flight (SPEC §6 step 10).
        order = self._own_order(db, session, request.raw_capture_id)
        if order is None or decision.amount_minor is None:
            raise HandleConflict("compat order is gone")
        allowed = self._policy.reason_allows.get(request.reason_code, frozenset())
        matches = [c for c in refund_candidates(db, order, self._policy.goodwill_percentages)
                   if c[4] == decision.amount_minor]
        matches.sort(key=lambda c: c[0] not in allowed)  # prefer a kind the reason allows
        candidate = matches[0] if matches else (
            HandleKind.CUSTOM_AMOUNT, None, None,
            f"Custom amount {from_minor(decision.amount_minor, order.currency)} {order.currency}",
            decision.amount_minor,
        )
        return [reserve_new_handle(db, session, order, candidate, now).handle_id]

    # ---------------------------------------------------------------- calling PayPal (new requests)

    async def _send_new(self, saved: _Saved) -> RefundOutcome:
        refund, problem = await self._call_paypal(saved.frozen)
        if refund is not None:
            return self._record_refund(saved.frozen, refund)
        if isinstance(problem, (PayPalRequestError, ValueError)):
            return self._record_rejection(saved.frozen, problem)
        return self._record_unknown(saved.frozen, problem)

    async def _call_paypal(self, frozen: dict[str, Any]) -> tuple[Refund | None, Exception | None]:
        """Send the frozen request exactly. Returns (refund, None) or (None, the problem)."""
        try:
            refund = await self._paypal.refund_capture(
                frozen["capture_id"],
                from_minor(frozen["amount_minor"], frozen["currency"]),
                frozen["currency"],
                frozen["idempotency_key"],
                note=frozen["note"] or None,
                custom_id=frozen["decision_id"],
            )
        except Exception as exc:  # every failure is classified by the caller; none can ALLOW
            logger.warning("PayPal refund for %s failed: %s", frozen["decision_id"], type(exc).__name__)
            return None, exc
        return refund, None

    def _record_refund(self, frozen, refund: Refund) -> RefundOutcome:
        if refund.status in SUCCESS_STATUSES:
            ledger_id = self._write_success(frozen, refund)
            amount = from_minor(frozen["amount_minor"], frozen["currency"])
            word = "completed" if refund.status == "COMPLETED" else "is being processed by PayPal"
            return RefundOutcome("ALLOWED", frozen["decision_id"], amount, frozen["currency"], ledger_id, None, (),
                                 f"Refund of {amount} {frozen['currency']} {word}.")
        if refund.status in NOT_REFUNDED_STATUSES:
            return self._write_failed(frozen, refund)
        return self._record_unknown(frozen, RuntimeError(f"unexpected refund status {refund.status!r}"))

    def _write_success(self, frozen, refund: Refund) -> str:
        """Ledger row + consume handles (+ close a recovery hold). Safe if a webhook got there first."""
        for _attempt in range(2):
            with self._sessions() as db:
                try:
                    with db.begin():
                        ledger = db.scalar(select(LedgerEntry).where(LedgerEntry.decision_id == frozen["decision_id"]))
                        if ledger is None:
                            ledger = LedgerEntry(
                                decision_id=frozen["decision_id"], capture_id=frozen["capture_id"],
                                paypal_refund_id=refund.id, amount_minor=frozen["amount_minor"],
                                currency=frozen["currency"], status=refund.status, created_at=self._clock(),
                            )
                            db.add(ledger)
                            db.flush()
                        _set_reserved(db, frozen["handle_ids"], HandleState.CONSUMED)
                        db.execute(update(Hold).where(Hold.decision_id == frozen["decision_id"],
                                                      Hold.status.in_((HoldStatus.PENDING, HoldStatus.APPROVED)))
                                   .values(status=HoldStatus.EXECUTED, decided_at=self._clock()))
                        return ledger.id
                except IntegrityError:
                    continue  # the webhook handler (P8) wrote the same refund first: read it on retry
        raise RuntimeError("could not record a refund that PayPal confirmed")

    def _write_failed(self, frozen, refund: Refund) -> RefundOutcome:
        with self._sessions() as db, db.begin():
            if db.scalar(select(LedgerEntry).where(LedgerEntry.paypal_refund_id == refund.id)) is None:
                db.add(LedgerEntry(decision_id=frozen["decision_id"], capture_id=frozen["capture_id"],
                                   paypal_refund_id=refund.id, amount_minor=frozen["amount_minor"],
                                   currency=frozen["currency"], status=refund.status, created_at=self._clock()))
            _release(db, frozen["handle_ids"], self._clock())
            _block_decision(db, frozen["decision_id"], Code.PAYPAL_REFUND_FAILED,
                            {"paypal": {"refund_status": refund.status}})
        return _blocked(frozen["decision_id"], [Code.PAYPAL_REFUND_FAILED])

    def _record_rejection(self, frozen, problem: Exception) -> RefundOutcome:
        """PayPal (or the client's own validation) refused: nothing was refunded, so free the handles."""
        with self._sessions() as db, db.begin():
            _release(db, frozen["handle_ids"], self._clock())
            _block_decision(db, frozen["decision_id"], Code.PAYPAL_REJECTED, {"paypal": _problem_details(problem)})
        return _blocked(frozen["decision_id"], [Code.PAYPAL_REJECTED])

    def _record_unknown(self, frozen, problem: Exception) -> RefundOutcome:
        """No usable answer. Keep the handles reserved and hold the frozen request (fail closed)."""
        unknown = not isinstance(problem, PayPalAuthError)  # 401/403: PayPal processed nothing
        with self._sessions() as db, db.begin():
            row = db.get(Decision, frozen["decision_id"])
            codes = list(row.reason_codes)
            if Code.DEPENDENCY_UNAVAILABLE.value not in codes:
                codes.append(Code.DEPENDENCY_UNAVAILABLE.value)
            row.verdict, row.reason_codes = Verdict.HOLD, codes
            row.details_json = {**row.details_json, "paypal": _problem_details(problem)}
            hold = db.scalar(select(Hold).where(Hold.decision_id == row.id))
            if hold is None:
                hold = Hold(decision_id=row.id, frozen_request_json=frozen, outcome_unknown=unknown,
                            created_at=self._clock())
                db.add(hold)
            else:
                hold.outcome_unknown = hold.outcome_unknown or unknown
            db.flush()
            return _held(row, frozen["amount_minor"], hold.id, [Code(c) for c in codes])

    # ================================================================ webhook reconciliation (P8)

    def record_webhook_refund(self, decision_id: str, paypal_refund_id: str, status: str) -> str:
        """SPEC §8 step 2: PayPal made this decision's refund, but Wardlock never recorded it
        (the response was lost, or the webhook beat the ledger write).

        The caller has already matched the event's capture and amount to the decision.
        This does exactly what a normal success does: ledger row, handles CONSUMED, any open
        hold EXECUTED. Returns the ledger row id (an existing one if the executor won the race).
        """
        with self._sessions() as db:
            row = db.get(Decision, decision_id)
            hold = db.scalar(select(Hold).where(Hold.decision_id == decision_id))
            handle_ids = (hold.frozen_request_json.get("handle_ids", []) if hold is not None
                          else row.details_json.get("reserved_handles", []))
            frozen = {"decision_id": row.id, "capture_id": row.capture_id, "amount_minor": row.amount_minor,
                      "currency": row.currency, "handle_ids": list(handle_ids)}
        refund = Refund(id=paypal_refund_id, status=status, amount=from_minor(row.amount_minor, row.currency),
                        currency=row.currency, custom_id=decision_id, raw={})
        return self._write_success(frozen, refund)

    # ================================================================ merchant actions on holds

    async def approve_hold(self, hold_id: str, approver: str) -> HoldOutcome:
        """Execute a held refund exactly as frozen. Approving twice still refunds at most once."""
        now = self._clock()
        with self._sessions() as db, db.begin():
            # Claim the hold atomically: a second approval finds nothing to claim.
            claimed = db.scalars(
                update(Hold).where(Hold.id == hold_id, Hold.status == HoldStatus.PENDING)
                .values(status=HoldStatus.APPROVED, approver=approver, decided_at=now).returning(Hold.id)
            ).all()
            hold = db.get(Hold, hold_id)
            if hold is None:
                return HoldOutcome(hold_id, "UNKNOWN", False, None, (), "There is no such hold.")
            if not claimed:
                return _hold_result(hold, False, None, (), f"This hold was already decided ({hold.status}).")
            frozen = dict(hold.frozen_request_json)
            if not hold.outcome_unknown:
                # Unknown-outcome holds skip this: re-sending the identical request is the only
                # safe way to finish them, and it can never refund twice.
                codes = self._recheck(db, hold, frozen, now)
                if codes:
                    return self._refuse_approval(db, hold, frozen, codes, now)

        refund, problem = await self._call_paypal(frozen)
        if refund is not None and refund.status in SUCCESS_STATUSES:
            ledger_id = self._write_success(frozen, refund)
            with self._sessions() as db:
                return _hold_result(db.get(Hold, hold_id), True, ledger_id, (), "Refund executed.")
        if refund is not None and refund.status in NOT_REFUNDED_STATUSES:
            self._write_failed(frozen, refund)
            return self._close_hold(hold_id, HoldStatus.BLOCKED, (Code.PAYPAL_REFUND_FAILED,))
        if isinstance(problem, (PayPalRequestError, ValueError)):
            self._record_rejection(frozen, problem)
            return self._close_hold(hold_id, HoldStatus.BLOCKED, (Code.PAYPAL_REJECTED,))
        # Unreachable, auth trouble or an odd status: back to PENDING. Approving again later
        # re-sends the same key, which is safe.
        with self._sessions() as db, db.begin():
            hold = db.get(Hold, hold_id)
            hold.status, hold.approver, hold.decided_at = HoldStatus.PENDING, None, None
            if not isinstance(problem, PayPalAuthError):
                hold.outcome_unknown = True
            return _hold_result(hold, False, None, (Code.DEPENDENCY_UNAVAILABLE.value,),
                                "PayPal did not answer. Approve again later; it cannot refund twice.")

    def _recheck(self, db: Session, hold: Hold, frozen: dict[str, Any], now: datetime) -> tuple[Code, ...]:
        decision = db.get(Decision, hold.decision_id)
        session = db.get(AgentSession, decision.session_id)
        order = db.scalar(select(Order).where(Order.capture_id == frozen["capture_id"]))
        handles = db.scalars(select(Handle).where(Handle.handle_id.in_(frozen["handle_ids"]))).all()
        view = (OrderView(order.capture_id, order.customer_id, order.currency, order.captured_at, _remaining(db, order))
                if order is not None else None)
        own = sum(h.amount_minor for h in handles if h.state == HandleState.RESERVED)
        facts = HoldFacts(session.customer_id, frozen["amount_minor"], view, own,
                          tuple(h.state for h in handles) if len(handles) == len(frozen["handle_ids"]) else ())
        return recheck_approval(facts, now)

    def _refuse_approval(self, db: Session, hold: Hold, frozen, codes, now) -> HoldOutcome:
        decision = db.get(Decision, hold.decision_id)
        decision.details_json = {**decision.details_json, "approval_refused": [c.value for c in codes]}
        if codes == (Code.DEPENDENCY_UNAVAILABLE,):
            # The order data is missing: nothing can be verified, so wait until it is back.
            hold.status, hold.approver, hold.decided_at = HoldStatus.PENDING, None, None
            return _hold_result(hold, False, None, tuple(c.value for c in codes),
                                "Cannot approve yet: the order data is missing.")
        hold.status = HoldStatus.BLOCKED
        _release(db, frozen["handle_ids"], now)
        reasons = " ".join(EXPLANATIONS[c] for c in codes)
        return _hold_result(hold, False, None, tuple(c.value for c in codes), f"Approval refused. {reasons}")

    def _close_hold(self, hold_id: str, status: HoldStatus, codes: tuple[Code, ...]) -> HoldOutcome:
        with self._sessions() as db, db.begin():
            hold = db.get(Hold, hold_id)
            hold.status = status
            return _hold_result(hold, False, None, tuple(c.value for c in codes),
                                " ".join(EXPLANATIONS[c] for c in codes))

    def deny_hold(self, hold_id: str, approver: str) -> HoldOutcome:
        now = self._clock()
        with self._sessions() as db, db.begin():
            hold = db.get(Hold, hold_id)
            if hold is None:
                return HoldOutcome(hold_id, "UNKNOWN", False, None, (), "There is no such hold.")
            if hold.outcome_unknown and hold.status == HoldStatus.PENDING:
                # PayPal may already have refunded. Freeing the handles could let the agent ask
                # again under a NEW key and refund twice, so the only safe way out is approval.
                return _hold_result(hold, False, None, (), "This refund may already have gone through at "
                                    "PayPal, so it cannot be denied. Approve it: that can never refund twice.")
            claimed = db.scalars(
                update(Hold).where(Hold.id == hold_id, Hold.status == HoldStatus.PENDING,
                                   Hold.outcome_unknown.is_(False))
                .values(status=HoldStatus.DENIED, approver=approver, decided_at=now).returning(Hold.id)
            ).all()
            if not claimed:
                return _hold_result(hold, False, None, (), f"This hold was already decided ({hold.status}).")
            _release(db, hold.frozen_request_json["handle_ids"], now)
            return _hold_result(hold, False, None, (), "Hold denied. No money moved.")

    # ================================================================ sweeps (run periodically)

    def expire_stale_holds(self) -> list[str]:
        """PENDING holds older than HOLD_TIMEOUT expire and free their handles. Unknown-outcome ones never do."""
        now = self._clock()
        with self._sessions() as db, db.begin():
            expired = db.execute(
                update(Hold)
                .where(Hold.status == HoldStatus.PENDING, Hold.outcome_unknown.is_(False),
                       Hold.created_at < now - self._hold_timeout)
                .values(status=HoldStatus.EXPIRED, decided_at=now)
                .returning(Hold.id, Hold.frozen_request_json)
            ).all()
            for _hold_id, frozen in expired:
                _release(db, frozen["handle_ids"], now)
        return [hold_id for hold_id, _frozen in expired]

    def recover_stuck(self) -> list[str]:
        """An ALLOW with a saved key but no ledger row and no hold, long after it was sent, means
        the process died mid-call. Turn it into an unknown-outcome hold, so it can only be
        finished by re-sending the same key."""
        now = self._clock()
        recovered = []
        with self._sessions() as db, db.begin():
            stuck = db.scalars(
                select(Decision).where(
                    Decision.verdict == Verdict.ALLOW,
                    Decision.idempotency_key.is_not(None),
                    Decision.created_at < now - STUCK_AFTER,
                    ~exists().where(LedgerEntry.decision_id == Decision.id),
                    ~exists().where(Hold.decision_id == Decision.id),
                )
            ).all()
            for row in stuck:
                reserved = row.details_json.get("reserved_handles", [])
                frozen = {"decision_id": row.id, "capture_id": row.capture_id, "amount_minor": row.amount_minor,
                          "currency": row.currency, "idempotency_key": row.idempotency_key,
                          "note": row.request_json.get("note", ""), "handle_ids": reserved}
                row.verdict = Verdict.HOLD
                row.reason_codes = [*row.reason_codes, Code.DEPENDENCY_UNAVAILABLE.value]
                db.add(Hold(decision_id=row.id, frozen_request_json=frozen, outcome_unknown=True, created_at=now))
                recovered.append(row.id)
        return recovered


# ==================================================================== helpers


def clean_note(note: str | None) -> str:
    """One line, trimmed, at most 200 characters: what PayPal's note_to_payer accepts."""
    if not isinstance(note, str):
        return ""
    single_line = re.sub(r"\s+", " ", "".join(ch if ch.isprintable() else " " for ch in note)).strip()
    return single_line[:MAX_NOTE_CHARS]


def _request_json(request: RefundRequest, note: str) -> dict[str, Any]:
    """What the agent sent, stored as plain JSON for the audit log (untrusted)."""
    data: dict[str, Any] = {"reason_code": str(request.reason_code), "note": note}
    if request.tool == "request_refund":
        data["handle_ids"] = [str(h) for h in request.handle_ids]
    else:
        data.update(capture_id=request.raw_capture_id, amount=None if request.raw_amount is None else str(request.raw_amount),
                    currency=request.raw_currency)
    return data


def _frozen(row: Decision, decision: EngineDecision, handle_ids: list[str], note: str) -> dict[str, Any]:
    """Exactly what will be sent to PayPal, now or after approval. Never changes afterwards."""
    return {
        "decision_id": row.id,
        "capture_id": row.capture_id,
        "amount_minor": decision.amount_minor,
        "currency": decision.currency,
        "idempotency_key": row.idempotency_key,
        "note": note,
        "handle_ids": handle_ids,
    }


def _raw_minor(request: RefundRequest, order: Order | None) -> int | None:
    if order is None or request.raw_currency != order.currency or request.raw_amount is None:
        return None
    try:
        minor = to_minor(request.raw_amount, order.currency)
    except (ValueError, TypeError):
        return None
    return minor or None


def _single_capture(db: Session, handle_ids: list[str]) -> str | None:
    captures = set(db.scalars(select(Handle.capture_id).where(Handle.handle_id.in_(handle_ids))))
    return captures.pop() if len(captures) == 1 else None


def _set_reserved(db: Session, handle_ids: list[str], to_state: HandleState) -> None:
    """Move whichever of these handles are still RESERVED (a webhook may have settled some)."""
    if handle_ids:
        db.execute(update(Handle).where(Handle.handle_id.in_(handle_ids), Handle.state == HandleState.RESERVED)
                   .values(state=to_state))


def _release(db: Session, handle_ids: list[str], now: datetime) -> None:
    """RESERVED -> AVAILABLE while the session lives, else EXPIRED (as handles.release_handles does)."""
    if not handle_ids:
        return
    for handle in db.scalars(select(Handle).where(Handle.handle_id.in_(handle_ids),
                                                  Handle.state == HandleState.RESERVED)).all():
        session = db.get(AgentSession, handle.session_id)
        handle.state = HandleState.AVAILABLE if session and session.is_live(now) else HandleState.EXPIRED
    db.flush()


def _block_decision(db: Session, decision_id: str, code: Code, extra: dict[str, Any]) -> None:
    row = db.get(Decision, decision_id)
    row.verdict = Verdict.BLOCK
    row.reason_codes = [*row.reason_codes, code.value]
    row.details_json = {**row.details_json, **extra}


def _problem_details(problem: Exception | None) -> dict[str, Any]:
    if isinstance(problem, PayPalRequestError):
        return {"error": problem.name, "issues": problem.issues, "debug_id": problem.debug_id,
                "status_code": problem.status_code}
    return {"error": type(problem).__name__ if problem else "unknown", "message": str(problem)[:200]}


def _blocked(decision_id: str | None, codes) -> RefundOutcome:
    codes = [Code(c) for c in codes]
    reasons = " ".join(EXPLANATIONS[c] for c in codes)
    return RefundOutcome("BLOCKED", decision_id, None, None, None, None, tuple(c.value for c in codes),
                         f"The refund was not made. {reasons}".strip())


def _held(row: Decision, amount_minor: int | None, hold_id: str, codes) -> RefundOutcome:
    codes = [Code(c) for c in codes]
    amount = from_minor(amount_minor, row.currency) if amount_minor is not None and row.currency else None
    reasons = " ".join(EXPLANATIONS[c] for c in codes)
    return RefundOutcome("HELD", row.id, amount, row.currency, None, hold_id, tuple(c.value for c in codes),
                         f"The refund is waiting for a person to review it. {reasons}".strip())


def _hold_result(hold: Hold, executed: bool, ledger_id: str | None, codes: tuple[str, ...], message: str) -> HoldOutcome:
    return HoldOutcome(hold.id, str(hold.status), executed, ledger_id, codes, message)
