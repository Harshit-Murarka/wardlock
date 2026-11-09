"""The executor end to end (SPEC §6): engine + reason check + database + a fake PayPal (respx)."""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import func, select

from support import fresh_engine

from wardlock.db import init_db, make_engine, make_session_factory
from wardlock.engine.policy import load_policy
from wardlock.enums import HandleKind, HandleState, HoldStatus, OrderStatus, ReasonCode, Verdict
from wardlock.executor import Executor, clean_note
from wardlock.handles import mint_handles
from wardlock.models import Customer, Decision, Handle, Hold, LedgerEntry, Order
from wardlock.paypal.client import PayPalClient
from wardlock.reason_check import ReasonCheckResult
from wardlock.sessions import open_session

SANDBOX = "https://api-m.sandbox.paypal.com"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
R = ReasonCode


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class FakePayPal:
    """The refund endpoint, with PayPal's idempotency: a repeated request id returns the same refund."""

    def __init__(self, factory):
        self.factory = factory
        self.calls = []  # (capture, request_id, body)
        self.mode = "ok"  # ok | timeout | 422 | 401 | PENDING | FAILED
        self.refunds = {}  # request id -> refund JSON
        self.key_saved_before_call = []
        self.before_answer = None  # e.g. a webhook writing the ledger row first

    def refund(self, request, capture):
        body = json.loads(request.content)
        request_id = request.headers["PayPal-Request-Id"]
        self.calls.append((capture, request_id, body))
        with self.factory() as db:  # invariant 4: is the decision with this key already committed?
            self.key_saved_before_call.append(
                db.scalar(select(Decision).where(Decision.idempotency_key == request_id)) is not None)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("slow")
        if self.mode == "422":
            return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg1",
                                             "details": [{"issue": "REFUND_AMOUNT_EXCEEDED"}]})
        if self.mode == "401":
            return httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"})
        if request_id in self.refunds:
            return httpx.Response(200, json=self.refunds[request_id])
        status = self.mode if self.mode in ("PENDING", "FAILED") else "COMPLETED"
        refund = {"id": f"RF{len(self.refunds) + 1}", "status": status, "amount": body["amount"],
                  "custom_id": body.get("custom_id")}
        self.refunds[request_id] = refund
        if self.before_answer:
            self.before_answer(refund)
        return httpx.Response(201, json=refund)

    def keys(self):
        return [request_id for _capture, request_id, _body in self.calls]


class FakeChecker:
    def __init__(self):
        self.calls = []
        self.result = ReasonCheckResult(True, "The ticket describes this damage.")
        self.before_return = None

    async def __call__(self, reason, labels, ticket_body):
        self.calls.append((reason, list(labels), ticket_body))
        if self.before_return:
            self.before_return()
        return self.result


def make_order(ref, customer, items, shipping, capture):
    return Order(
        customer_id=customer, order_ref=ref, status=OrderStatus.DELIVERED, placed_at=date(2026, 9, 20),
        paypal_order_id=f"PO{capture}", capture_id=capture, currency="USD",
        captured_minor=sum(u * q for _r, _n, u, q in items) + shipping,
        items_json=[{"item_ref": r, "name": n, "unit_minor": u, "quantity": q} for r, n, u, q in items],
        shipping_minor=shipping, captured_at=NOW - timedelta(days=5),
    )


@pytest.fixture
async def env():
    engine = fresh_engine()
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as db, db.begin():
        db.add_all([Customer(id="cust_ana", first_name="Ana", email="ana@example.com"),
                    Customer(id="cust_ben", first_name="Ben", email="ben@example.com")])
        db.flush()
        db.add_all([
            make_order("A-1001", "cust_ana", [("bell", "Bike bell", 400, 1), ("cage", "Bottle cage", 799, 2)],
                       650, "CAPA1001"),
            make_order("A-1002", "cust_ana", [("helmet", "Commuter helmet", 17900, 1)], 1000, "CAPA1002"),
            make_order("B-2001", "cust_ben", [("seatpost", "Carbon seatpost", 17900, 1)], 1000, "CAPB2001"),
        ])
    fake, checker, clock = FakePayPal(factory), FakeChecker(), Clock()

    async def no_sleep(_seconds):
        pass

    with respx.mock(base_url=SANDBOX, assert_all_called=False) as router:
        router.post("/v1/oauth2/token").respond(200, json={"access_token": "tok", "expires_in": 3600})
        router.post(path__regex=r"^/v2/payments/captures/(?P<capture>[A-Z0-9]+)/refund$").mock(side_effect=fake.refund)
        async with PayPalClient(SANDBOX, "id", "secret", sleep=no_sleep) as paypal:
            executor = Executor(factory, paypal, load_policy(), checker, hold_timeout=timedelta(minutes=15),
                                clock=clock)
            yield SimpleNamespace(factory=factory, fake=fake, checker=checker, clock=clock, executor=executor)
    engine.dispose()


def open_ana(env, customer="cust_ana"):
    with env.factory() as db, db.begin():
        return open_session(db, ticket_id="T-1", customer_id=customer, ticket_body="My bike bell arrived scratched.",
                            ttl_minutes=30, now=NOW).session_id


def mint(env, session_id):
    """{label: handle_id} of the session's fresh options."""
    with env.factory() as db, db.begin():
        from wardlock.models import AgentSession
        result = mint_handles(db, db.get(AgentSession, session_id), goodwill_percentages=[10, 25], now=env.clock.now)
        return {o.label: o.handle for order in result for o in order.refund_options}


def one(env, model, **where):
    with env.factory() as db:
        query = select(model)
        for column, value in where.items():
            query = query.where(getattr(model, column) == value)
        return db.scalars(query).all()


def state(env, handle_id):
    with env.factory() as db:
        return db.get(Handle, handle_id).state


def hold_of(env, hold_id):
    with env.factory() as db:
        return db.get(Hold, hold_id)


async def held_helmet(env):
    """The 189.00 helmet refund, held because it is above the 50.00 limit."""
    session = open_ana(env)
    options = mint(env, session)
    outcome = await env.executor.request_refund(session, [options["Full remaining refund"] if False else
                                                          _helmet_full(env, session)], R.NOT_RECEIVED)
    assert outcome.status == "HELD"
    return session, outcome


def _helmet_full(env, session):
    with env.factory() as db:
        return db.scalar(select(Handle.handle_id).where(
            Handle.session_id == session, Handle.capture_id == "CAPA1002",
            Handle.kind == HandleKind.FULL_REMAINING, Handle.state == HandleState.AVAILABLE))


# ---------------------------------------------------------------- the happy path


async def test_bell_refund_is_allowed_recorded_and_consumed(env):
    session = open_ana(env)
    bell = mint(env, session)["Bike bell x1"]

    outcome = await env.executor.request_refund(session, [bell], R.DAMAGED, "Sorry about the bell!")

    assert (outcome.status, outcome.amount, outcome.currency, outcome.hold_id) == ("ALLOWED", Decimal("4.00"), "USD", None)
    assert outcome.refund_ref.startswith("rf_")
    assert outcome.message_for_agent == "Refund of 4.00 USD completed."
    [decision] = one(env, Decision)
    capture, request_id, body = env.fake.calls[0]
    assert (capture, request_id) == ("CAPA1001", decision.idempotency_key)
    assert body == {"amount": {"value": "4.00", "currency_code": "USD"},
                    "note_to_payer": "Sorry about the bell!", "custom_id": decision.id}
    [ledger] = one(env, LedgerEntry)
    assert (ledger.amount_minor, ledger.status, ledger.decision_id) == (400, "COMPLETED", decision.id)
    assert state(env, bell) == HandleState.CONSUMED
    assert env.checker.calls == [(R.DAMAGED, ["Bike bell x1"], "My bike bell arrived scratched.")]
    assert decision.details_json["reason_check"]["consistent"] is True
    assert decision.latency_ms >= 0


async def test_the_decision_and_key_are_committed_before_paypal_is_called(env):
    """Invariant 4: inside PayPal's handler, the decision with this exact key is already in the database."""
    session = open_ana(env)
    await env.executor.request_refund(session, [mint(env, session)["Bike bell x1"]], R.DAMAGED)
    assert env.fake.key_saved_before_call == [True]


# ---------------------------------------------------------------- T9 / T16: fail closed


async def test_T9_paypal_timeouts_hold_with_the_same_key_throughout(env):
    session = open_ana(env)
    bell = mint(env, session)["Bike bell x1"]
    env.fake.mode = "timeout"

    outcome = await env.executor.request_refund(session, [bell], R.DAMAGED)

    assert outcome.status == "HELD" and "DEPENDENCY_UNAVAILABLE" in outcome.reason_codes
    [decision] = one(env, Decision)
    assert env.fake.keys() == [decision.idempotency_key] * 4  # 1 try + 3 retries, one key
    hold = hold_of(env, outcome.hold_id)
    assert hold.outcome_unknown is True
    assert hold.frozen_request_json["idempotency_key"] == decision.idempotency_key
    assert state(env, bell) == HandleState.RESERVED
    assert one(env, LedgerEntry) == []


@pytest.mark.parametrize("result", [ReasonCheckResult(None, error="timeout"), ReasonCheckResult(False, "no damage")])
async def test_T16_a_failed_or_negative_reason_check_holds_without_calling_paypal(env, result):
    session = open_ana(env)
    bell = mint(env, session)["Bike bell x1"]
    env.checker.result = result

    outcome = await env.executor.request_refund(session, [bell], R.DAMAGED)

    assert (outcome.status, outcome.reason_codes) == ("HELD", ("REASON_NOT_SUPPORTED_BY_TICKET",))
    assert env.fake.calls == []
    assert hold_of(env, outcome.hold_id).outcome_unknown is False
    assert state(env, bell) == HandleState.RESERVED


async def test_the_reason_check_only_runs_for_allows(env):
    session = open_ana(env)
    mint(env, session)
    await env.executor.request_refund(session, [_helmet_full(env, session)], R.NOT_RECEIVED)  # HOLD: over limit
    await env.executor.request_refund(session, ["h_made_up"], R.DAMAGED)  # BLOCK
    assert env.checker.calls == []


# ---------------------------------------------------------------- approving holds (T17)


async def test_T17_approving_executes_the_frozen_request_with_the_same_key(env):
    _session, held = await held_helmet(env)
    [decision] = one(env, Decision)

    result = await env.executor.approve_hold(held.hold_id, "merchant@example.com")

    assert (result.executed, result.status) == (True, "EXECUTED")
    capture, request_id, body = env.fake.calls[0]
    assert (capture, request_id, body["amount"]["value"]) == ("CAPA1002", decision.idempotency_key, "189.00")
    [ledger] = one(env, LedgerEntry)
    assert (ledger.amount_minor, result.refund_ref) == (18900, ledger.id)
    assert hold_of(env, held.hold_id).approver == "merchant@example.com"


async def test_approving_twice_refunds_once(env):
    _session, held = await held_helmet(env)
    first = await env.executor.approve_hold(held.hold_id, "merchant")
    second = await env.executor.approve_hold(held.hold_id, "merchant")
    assert (first.executed, second.executed) == (True, False)
    assert "already decided" in second.message
    assert len(env.fake.calls) == 1


async def test_approving_after_timeouts_replays_the_same_key(env):
    session = open_ana(env)
    env.fake.mode = "timeout"
    held = await env.executor.request_refund(session, [mint(env, session)["Bike bell x1"]], R.DAMAGED)
    env.fake.mode = "ok"

    result = await env.executor.approve_hold(held.hold_id, "merchant")

    assert result.executed is True
    assert len(set(env.fake.keys())) == 1  # 4 timed-out tries + the approval: one key
    assert len(one(env, LedgerEntry)) == 1


async def test_approval_while_paypal_is_down_goes_back_to_pending(env):
    _session, held = await held_helmet(env)
    env.fake.mode = "timeout"
    down = await env.executor.approve_hold(held.hold_id, "merchant")
    assert (down.executed, down.status) == (False, "PENDING")
    assert hold_of(env, held.hold_id).outcome_unknown is True

    env.fake.mode = "ok"
    up = await env.executor.approve_hold(held.hold_id, "merchant")
    assert up.executed is True
    assert len(set(env.fake.keys())) == 1


# ---------------------------------------------------------------- the approval re-check (SPEC §6.9)


async def test_approval_is_refused_if_the_order_changed_owner(env):
    session, held = await held_helmet(env)
    with env.factory() as db, db.begin():
        db.scalar(select(Order).where(Order.capture_id == "CAPA1002")).customer_id = "cust_ben"

    result = await env.executor.approve_hold(held.hold_id, "merchant")

    assert (result.executed, result.status) == (False, "BLOCKED")
    assert "NOT_OWNER" in result.reason_codes
    assert env.fake.calls == []
    assert state(env, _reserved_of(env, held.hold_id)) == HandleState.AVAILABLE


async def test_approval_is_refused_after_the_180_day_window(env):
    _session, held = await held_helmet(env)
    env.clock.now = NOW + timedelta(days=180)  # the capture is now 185 days old
    result = await env.executor.approve_hold(held.hold_id, "merchant")
    assert "REFUND_WINDOW_EXPIRED" in result.reason_codes and result.executed is False
    assert state(env, _reserved_of(env, held.hold_id)) == HandleState.EXPIRED  # the session is long over


async def test_approval_is_refused_if_the_handle_was_used_elsewhere(env):
    _session, held = await held_helmet(env)
    with env.factory() as db, db.begin():
        db.get(Handle, _reserved_of(env, held.hold_id)).state = HandleState.CONSUMED
    result = await env.executor.approve_hold(held.hold_id, "merchant")
    assert "HANDLE_ALREADY_USED" in result.reason_codes and env.fake.calls == []


async def test_approval_waits_while_the_order_data_is_missing(env):
    _session, held = await held_helmet(env)
    with env.factory() as db, db.begin():
        db.scalar(select(Order).where(Order.capture_id == "CAPA1002")).capture_id = "MOVED"
    result = await env.executor.approve_hold(held.hold_id, "merchant")
    assert (result.status, result.reason_codes) == ("PENDING", ("DEPENDENCY_UNAVAILABLE",))
    assert env.fake.calls == []


async def test_an_unknown_outcome_hold_skips_the_recheck_and_resends_the_same_key(env):
    session = open_ana(env)
    env.fake.mode = "timeout"
    held = await env.executor.request_refund(session, [mint(env, session)["Bike bell x1"]], R.DAMAGED)
    with env.factory() as db, db.begin():
        db.scalar(select(Order).where(Order.capture_id == "CAPA1001")).customer_id = "cust_ben"
    env.fake.mode = "ok"
    result = await env.executor.approve_hold(held.hold_id, "merchant")
    assert result.executed is True  # PayPal may already have refunded: re-sending is the only safe finish


def _reserved_of(env, hold_id):
    return hold_of(env, hold_id).frozen_request_json["handle_ids"][0]


# ---------------------------------------------------------------- deny and expire


async def test_deny_releases_the_handles(env):
    _session, held = await held_helmet(env)
    result = env.executor.deny_hold(held.hold_id, "merchant")
    assert (result.status, env.fake.calls) == ("DENIED", [])
    assert state(env, _reserved_of(env, held.hold_id)) == HandleState.AVAILABLE


async def test_an_unknown_outcome_hold_cannot_be_denied(env):
    session = open_ana(env)
    env.fake.mode = "timeout"
    held = await env.executor.request_refund(session, [mint(env, session)["Bike bell x1"]], R.DAMAGED)
    result = env.executor.deny_hold(held.hold_id, "merchant")
    assert result.status == "PENDING" and "cannot be denied" in result.message
    assert state(env, _reserved_of(env, held.hold_id)) == HandleState.RESERVED


async def test_stale_holds_expire_but_fresh_and_unknown_outcome_ones_do_not(env):
    _session, stale = await held_helmet(env)
    env.fake.mode = "timeout"
    other = open_ana(env)
    unknown = await env.executor.request_refund(other, [mint(env, other)["Bike bell x1"]], R.DAMAGED)

    env.clock.now = NOW + timedelta(minutes=10)
    assert env.executor.expire_stale_holds() == []  # not old enough yet
    env.clock.now = NOW + timedelta(minutes=16)
    assert env.executor.expire_stale_holds() == [stale.hold_id]

    assert hold_of(env, stale.hold_id).status == HoldStatus.EXPIRED
    assert state(env, _reserved_of(env, stale.hold_id)) == HandleState.AVAILABLE
    assert hold_of(env, unknown.hold_id).status == HoldStatus.PENDING


# ---------------------------------------------------------------- PayPal outcomes


async def test_a_paypal_rejection_blocks_and_frees_the_handles(env):
    session = open_ana(env)
    bell = mint(env, session)["Bike bell x1"]
    env.fake.mode = "422"
    outcome = await env.executor.request_refund(session, [bell], R.DAMAGED)
    assert (outcome.status, outcome.reason_codes) == ("BLOCKED", ("PAYPAL_REJECTED",))
    assert state(env, bell) == HandleState.AVAILABLE
    [decision] = one(env, Decision)
    assert decision.details_json["paypal"]["issues"] == ["REFUND_AMOUNT_EXCEEDED"]
    assert one(env, LedgerEntry) == []


async def test_an_auth_failure_holds_with_a_known_outcome(env):
    session = open_ana(env)
    env.fake.mode = "401"
    outcome = await env.executor.request_refund(session, [mint(env, session)["Bike bell x1"]], R.DAMAGED)
    assert outcome.status == "HELD"
    assert hold_of(env, outcome.hold_id).outcome_unknown is False  # PayPal processed nothing
    assert env.executor.deny_hold(outcome.hold_id, "merchant").status == "DENIED"


async def test_a_pending_refund_counts_as_made(env):
    session = open_ana(env)
    env.fake.mode = "PENDING"
    outcome = await env.executor.request_refund(session, [mint(env, session)["Bike bell x1"]], R.DAMAGED)
    assert outcome.status == "ALLOWED" and "being processed" in outcome.message_for_agent
    assert [l.status for l in one(env, LedgerEntry)] == ["PENDING"]


async def test_a_failed_refund_blocks_and_frees_the_handles(env):
    session = open_ana(env)
    bell = mint(env, session)["Bike bell x1"]
    env.fake.mode = "FAILED"
    outcome = await env.executor.request_refund(session, [bell], R.DAMAGED)
    assert (outcome.status, outcome.reason_codes) == ("BLOCKED", ("PAYPAL_REFUND_FAILED",))
    assert state(env, bell) == HandleState.AVAILABLE
    assert [l.status for l in one(env, LedgerEntry)] == ["FAILED"]  # kept for audit, not counted


# ---------------------------------------------------------------- blocks, races and limits


async def test_an_engine_block_saves_no_key_reserves_nothing_and_calls_nobody(env):
    ana, ben = open_ana(env), open_ana(env, "cust_ben")
    bens_handle = mint(env, ben)["Carbon seatpost x1"]
    outcome = await env.executor.request_refund(ana, [bens_handle], R.DAMAGED)
    # Every code is collected: another session's option, Ben's order, and 179.00 is over the limit.
    assert (outcome.status, outcome.reason_codes) == (
        "BLOCKED", ("HANDLE_OTHER_SESSION", "NOT_OWNER", "OVER_AUTO_LIMIT"))
    [decision] = one(env, Decision, session_id=ana)
    assert decision.idempotency_key is None and env.fake.calls == []
    assert state(env, bens_handle) == HandleState.AVAILABLE


async def test_a_handle_taken_between_decide_and_reserve_is_blocked(env):
    session = open_ana(env)
    bell = mint(env, session)["Bike bell x1"]

    def someone_else_takes_it():
        with env.factory() as db, db.begin():
            db.get(Handle, bell).state = HandleState.RESERVED

    env.checker.before_return = someone_else_takes_it
    outcome = await env.executor.request_refund(session, [bell], R.DAMAGED)
    assert (outcome.status, outcome.reason_codes) == ("BLOCKED", ("HANDLE_ALREADY_USED",))
    assert one(env, Decision)[0].idempotency_key is None and env.fake.calls == []


async def test_an_item_refunded_in_one_session_cannot_be_refunded_from_another(env):
    """Two support sessions both offer the bell. Once one refunds it, the other's copy is dead."""
    first, second = open_ana(env), open_ana(env)
    first_bell, second_bell = mint(env, first)["Bike bell x1"], mint(env, second)["Bike bell x1"]
    assert (await env.executor.request_refund(first, [first_bell], R.DAMAGED)).status == "ALLOWED"

    outcome = await env.executor.request_refund(second, [second_bell], R.DAMAGED)

    assert (outcome.status, outcome.reason_codes) == ("BLOCKED", ("HANDLE_ALREADY_USED",))
    assert len(env.fake.calls) == 1


async def test_the_session_velocity_comes_from_the_ledger(env):
    session = open_ana(env)
    for label in ("Bike bell x1", "Bottle cage x2"):
        reason = R.DAMAGED
        assert (await env.executor.request_refund(session, [mint(env, session)[label]], reason)).status == "ALLOWED"
    third = await env.executor.request_refund(session, [mint(env, session)["Shipping"]], R.WRONG_ITEM)
    assert (third.status, third.reason_codes) == ("HELD", ("SESSION_VELOCITY",))


async def test_two_refunds_cannot_add_up_past_the_limit(env):
    """Salami slicing through the real database: 10.00 + 44.75 = 54.75 is over 50.00."""
    session = open_ana(env)
    options = mint(env, session)
    with env.factory() as db:
        helmet = {h.label: h.handle_id for h in db.scalars(select(Handle).where(
            Handle.session_id == session, Handle.capture_id == "CAPA1002"))}
    assert (await env.executor.request_refund(session, [helmet["Shipping"]], R.WRONG_ITEM)).status == "ALLOWED"
    second = await env.executor.request_refund(session, [helmet["25% goodwill"]], R.DAMAGED)
    assert (second.status, second.reason_codes) == ("HELD", ("OVER_AUTO_LIMIT",))
    assert options  # the A-1001 options were minted too, but are not involved


# ---------------------------------------------------------------- compat (SPEC §3.4, §6.10)


async def test_a_compat_refund_marks_the_matching_item_as_used(env):
    session = open_ana(env)
    outcome = await env.executor.request_refund_raw(session, "CAPA1001", Decimal("4.00"), "USD", R.DAMAGED)
    assert outcome.status == "ALLOWED"
    [handle] = one(env, Handle, session_id=session)
    assert (handle.kind, handle.item_ref, handle.state) == (HandleKind.LINE_ITEM, "bell", HandleState.CONSUMED)
    assert "Bike bell x1" not in mint(env, session)  # never offered again


async def test_compat_then_a_handle_cannot_refund_the_same_item_twice(env):
    session = open_ana(env)
    bell = mint(env, session)["Bike bell x1"]
    assert (await env.executor.request_refund_raw(session, "CAPA1001", Decimal("4.00"), "USD", R.DAMAGED)).status == "ALLOWED"
    outcome = await env.executor.request_refund(session, [bell], R.DAMAGED)
    assert (outcome.status, outcome.reason_codes) == ("BLOCKED", ("HANDLE_ALREADY_USED",))
    assert len(env.fake.calls) == 1


async def test_a_non_derivable_compat_amount_is_held_and_can_be_approved(env):
    session = open_ana(env)
    held = await env.executor.request_refund_raw(session, "CAPA1001", Decimal("3.00"), "USD", R.DAMAGED)
    assert (held.status, held.reason_codes) == ("HELD", ("AMOUNT_NOT_DERIVABLE",))
    [handle] = one(env, Handle, session_id=session)
    assert (handle.kind, handle.state, handle.amount_minor) == (HandleKind.CUSTOM_AMOUNT, HandleState.RESERVED, 300)

    result = await env.executor.approve_hold(held.hold_id, "merchant")
    assert result.executed is True and env.fake.calls[0][2]["amount"]["value"] == "3.00"


async def test_compat_on_another_customers_capture_is_blocked(env):
    session = open_ana(env)
    outcome = await env.executor.request_refund_raw(session, "CAPB2001", Decimal("189.00"), "USD", R.NOT_RECEIVED)
    assert (outcome.status, outcome.reason_codes) == ("BLOCKED", ("NOT_OWNER",))
    assert env.fake.calls == [] and one(env, Handle) == []


# ---------------------------------------------------------------- recovery, races, details


async def test_a_crash_after_paypal_is_recovered_into_an_unknown_outcome_hold(env, monkeypatch):
    session = open_ana(env)
    bell = mint(env, session)["Bike bell x1"]

    def crash(*_args):
        raise RuntimeError("the process died before writing the ledger")

    monkeypatch.setattr(env.executor, "_write_success", crash)
    with pytest.raises(RuntimeError):
        await env.executor.request_refund(session, [bell], R.DAMAGED)
    monkeypatch.undo()

    env.clock.now = NOW + timedelta(minutes=4)
    assert env.executor.recover_stuck() == []  # a request may still be in flight
    env.clock.now = NOW + timedelta(minutes=6)
    [decision_id] = env.executor.recover_stuck()
    [hold] = one(env, Hold)
    assert hold.outcome_unknown is True and hold.decision_id == decision_id

    result = await env.executor.approve_hold(hold.id, "merchant")
    assert result.executed is True
    assert env.fake.calls[0][1] == env.fake.calls[1][1]  # the same key: PayPal replays, no second refund
    assert len(env.fake.refunds) == 1 and len(one(env, LedgerEntry)) == 1


async def test_a_ledger_row_written_by_the_webhook_first_is_not_duplicated(env):
    session = open_ana(env)
    bell = mint(env, session)["Bike bell x1"]

    def webhook_wins(refund):
        with env.factory() as db, db.begin():
            db.add(LedgerEntry(decision_id=refund["custom_id"], capture_id="CAPA1001", paypal_refund_id=refund["id"],
                               amount_minor=400, currency="USD", status="COMPLETED"))

    env.fake.before_answer = webhook_wins
    outcome = await env.executor.request_refund(session, [bell], R.DAMAGED)
    [ledger] = one(env, LedgerEntry)
    assert (outcome.status, outcome.refund_ref) == ("ALLOWED", ledger.id)
    assert state(env, bell) == HandleState.CONSUMED


async def test_the_note_is_one_line_and_at_most_200_characters(env):
    session = open_ana(env)
    note = "Sorry!\nWe are\tsending it back.  " + "x" * 300
    await env.executor.request_refund(session, [mint(env, session)["Bike bell x1"]], R.DAMAGED, note)
    sent = env.fake.calls[0][2]["note_to_payer"]
    assert sent.startswith("Sorry! We are sending it back. x") and len(sent) == 200 and "\n" not in sent
    assert clean_note(None) == "" and clean_note("  ") == ""


async def test_an_unknown_session_id_is_blocked_without_a_decision_row(env):
    outcome = await env.executor.request_refund("ses_nope", ["h_x"], R.DAMAGED)
    assert (outcome.status, outcome.reason_codes, outcome.decision_id) == ("BLOCKED", ("SESSION_INVALID",), None)
    assert one(env, Decision) == []


async def test_an_expired_session_is_blocked_and_recorded(env):
    session = open_ana(env)
    bell = mint(env, session)["Bike bell x1"]
    env.clock.now = NOW + timedelta(minutes=31)
    outcome = await env.executor.request_refund(session, [bell], R.DAMAGED)
    assert (outcome.status, outcome.reason_codes) == ("BLOCKED", ("SESSION_INVALID",))
    assert len(one(env, Decision)) == 1 and env.fake.calls == []


async def test_a_refund_a_person_approved_does_not_use_up_the_automatic_limit(env):
    """The 189.00 helmet was approved by the merchant. The 4.00 bell afterwards is still automatic."""
    session, held = await held_helmet(env)
    assert (await env.executor.approve_hold(held.hold_id, "merchant")).executed is True
    outcome = await env.executor.request_refund(session, [mint(env, session)["Bike bell x1"]], R.DAMAGED)
    assert outcome.status == "ALLOWED"
