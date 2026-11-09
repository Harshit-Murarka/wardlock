"""Refund handles (SPEC §4): Wardlock computes every amount; the agent can only pick options."""

import json
import secrets
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from wardlock.db import init_db, make_engine, make_session_factory
from wardlock.handles import (
    MAX_HANDLES_PER_REQUEST,
    HandleConflict,
    HandleStateError,
    SessionInvalid,
    consume_handles,
    goodwill_minor,
    load_handles,
    mint_handles,
    release_handles,
    remaining_refundable_minor,
    reserve_handles,
)
from wardlock.models import (
    AgentSession,
    Customer,
    Decision,
    Handle,
    HandleKind,
    HandleState,
    LedgerEntry,
    Order,
    OrderStatus,
    SessionStatus,
    Verdict,
)
from wardlock.sessions import expire_session

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
GOODWILL = [10, 25]


def make_order(ref, customer, items, shipping_minor, capture_id, placed=date(2026, 9, 1)):
    items_json = [{"item_ref": r, "name": n, "unit_minor": u, "quantity": q} for r, n, u, q in items]
    return Order(
        customer_id=customer, order_ref=ref, status=OrderStatus.DELIVERED, placed_at=placed,
        paypal_order_id=f"PPORDER-{ref}", capture_id=capture_id, currency="USD",
        captured_minor=sum(u * q for _r, _n, u, q in items) + shipping_minor,
        items_json=items_json, shipping_minor=shipping_minor, captured_at=NOW - timedelta(days=5),
    )


@pytest.fixture
def db():
    """The real demo shapes from seed/orders.json, with fake capture ids."""
    engine = make_engine("sqlite://")
    init_db(engine)
    with make_session_factory(engine)() as session:
        for customer_id, name in [("cust_ana", "Ana"), ("cust_ben", "Ben"), ("cust_chen", "Chen")]:
            session.add(Customer(id=customer_id, first_name=name, email=f"{name.lower()}@example.com"))
        session.add_all([
            make_order("A-1001", "cust_ana", [("bell", "Bike bell", 400, 1), ("cage", "Bottle cage", 799, 2)],
                       650, "CAPA1001", placed=date(2026, 9, 20)),
            make_order("A-1003", "cust_ana", [("pump", "Floor pump", 2499, 1)], 500, "CAPA1003",
                       placed=date(2026, 8, 15)),
            make_order("B-2001", "cust_ben", [("seatpost", "Carbon seatpost", 17900, 1)], 1000, "CAPB2001"),
            make_order("B-2003", "cust_ben", [("lube", "Chain lube", 1250, 1)], 0, "CAPB2003"),
            make_order("C-3002", "cust_chen", [("gloves", "Cycling gloves", 1999, 3)], 0, "CAPC3002"),
        ])
        session.flush()
        yield session
    engine.dispose()


def new_session(db, customer="cust_ana", lifetime=timedelta(minutes=30)):
    session = AgentSession(ticket_id="T-1", customer_id=customer, ticket_body="...",
                           token_hash=secrets.token_hex(32), created_at=NOW, expires_at=NOW + lifetime)
    db.add(session)
    db.flush()
    return session


def mint(db, session):
    return mint_handles(db, session, goodwill_percentages=GOODWILL, now=NOW)


def options_for(result, order_ref):
    return next(o for o in result if o.order_ref == order_ref).refund_options


def summary(options):
    return [(o.kind.value, o.label, str(o.amount)) for o in options]


def option(options, kind, label=None):
    return next(o for o in options if o.kind == kind and (label is None or o.label == label))


def state_of(db, handle_id):
    return db.scalar(select(Handle.state).where(Handle.handle_id == handle_id))


def refund_in_ledger(db, session, capture_id, amount_minor, status="COMPLETED"):
    """A refund PayPal confirmed (as P5 will record it)."""
    decision = Decision(session_id=session.id, tool="request_refund", request_json={}, verdict=Verdict.ALLOW,
                        capture_id=capture_id, amount_minor=amount_minor, currency="USD",
                        idempotency_key=secrets.token_hex(8), policy_version="1")
    db.add(decision)
    db.flush()
    db.add(LedgerEntry(decision_id=decision.id, capture_id=capture_id, paypal_refund_id=secrets.token_hex(8),
                       amount_minor=amount_minor, currency="USD", status=status))
    db.flush()


def refund_with_handle(db, session, chosen):
    """Spend one handle the way P5 will: reserve, PayPal confirms, consume and record."""
    reserve_handles(db, session.id, [chosen.handle])
    consume_handles(db, [chosen.handle])
    refund_in_ledger(db, session, db.get(Handle, chosen.handle).capture_id, int(chosen.amount * 100))


# ---------------------------------------------------------------- minting


def test_mints_exactly_the_spec_example_for_a_1001(db):
    result = mint(db, new_session(db))
    assert summary(options_for(result, "A-1001")) == [
        ("LINE_ITEM", "Bike bell x1", "4.00"),
        ("LINE_ITEM", "Bottle cage x1 (of 2)", "7.99"),  # one unit of a multi-unit line
        ("LINE_ITEM", "Bottle cage x2", "15.98"),
        ("SHIPPING", "Shipping", "6.50"),
        ("GOODWILL_PCT", "10% goodwill", "2.00"),
        ("GOODWILL_PCT", "25% goodwill", "5.00"),  # 4.995 rounded half-up
        ("FULL_REMAINING", "Full remaining refund", "26.48"),
    ]


def test_only_the_session_customers_orders_get_options(db):
    """G1: Ana's session never sees Ben's $189 order, the injection target in the demo."""
    ana = [o.order_ref for o in mint(db, new_session(db, "cust_ana"))]
    ben = [o.order_ref for o in mint(db, new_session(db, "cust_ben"))]
    assert ana == ["A-1001", "A-1003"]  # newest first
    assert ben == ["B-2001", "B-2003"]  # same date, so ordered by order_ref
    assert not {"B-2001", "B-2003"} & set(ana)


def test_no_paypal_ids_reach_the_agent(db):
    """Invariant 7: the agent never sees a capture or PayPal order id."""
    text = json.dumps([asdict(o) for o in mint(db, new_session(db))], default=str)
    assert "CAPA1001" not in text and "PPORDER" not in text


def test_three_times_19_99_is_exactly_59_97(db):
    """T18: the float version of this sum is 59.97000000000001."""
    options = options_for(mint(db, new_session(db, "cust_chen")), "C-3002")
    assert str(option(options, HandleKind.LINE_ITEM, "Cycling gloves x3").amount) == "59.97"
    assert [str(o.amount) for o in options if o.kind == HandleKind.LINE_ITEM] == ["19.99", "39.98", "59.97"]
    assert str(option(options, HandleKind.FULL_REMAINING).amount) == "59.97"
    assert [str(o.amount) for o in options if o.kind == HandleKind.GOODWILL_PCT] == ["6.00", "14.99"]


def test_free_shipping_order_has_no_shipping_option(db):
    options = options_for(mint(db, new_session(db, "cust_ben")), "B-2003")
    assert HandleKind.SHIPPING not in {o.kind for o in options}


@pytest.mark.parametrize(
    ("item_total", "pct", "expected"),
    [(1998, 25, 500), (1998, 10, 200), (5997, 25, 1499), (5, 10, 1), (4, 10, 0), (10000, 100, 10000)],
)
def test_goodwill_rounds_half_up_to_whole_cents(item_total, pct, expected):
    assert goodwill_minor(item_total, pct) == expected


@pytest.mark.parametrize("pct", [0, 101, -10, True, 2.5, "10"])
def test_goodwill_percentage_must_be_a_whole_number_from_1_to_100(pct):
    with pytest.raises(ValueError):
        goodwill_minor(1998, pct)


def test_goodwill_that_rounds_to_nothing_is_not_offered(db):
    db.add(make_order("A-1009", "cust_ana", [("sticker", "Sticker", 4, 1)], 0, "CAPA1009", placed=date(2026, 10, 1)))
    db.flush()
    options = options_for(mint(db, new_session(db)), "A-1009")
    # 10% of 0.04 rounds to 0.00 and is skipped; 25% is 0.01.
    assert summary(options) == [
        ("LINE_ITEM", "Sticker x1", "0.04"),
        ("GOODWILL_PCT", "25% goodwill", "0.01"),
        ("FULL_REMAINING", "Full remaining refund", "0.04"),
    ]


def test_minting_again_expires_the_old_options(db):
    session = new_session(db)
    first = mint(db, session)
    second = mint(db, session)
    old_ids = [o.handle for order in first for o in order.refund_options]
    new_ids = [o.handle for order in second for o in order.refund_options]
    assert not set(old_ids) & set(new_ids)
    assert {state_of(db, h) for h in old_ids} == {HandleState.EXPIRED}
    assert {state_of(db, h) for h in new_ids} == {HandleState.AVAILABLE}


def test_after_a_bell_refund_the_bell_is_gone_and_full_remaining_shrinks(db):
    session = new_session(db)
    refund_with_handle(db, session, option(options_for(mint(db, session), "A-1001"), HandleKind.LINE_ITEM, "Bike bell x1"))

    assert summary(options_for(mint(db, session), "A-1001")) == [
        ("LINE_ITEM", "Bottle cage x1 (of 2)", "7.99"),
        ("LINE_ITEM", "Bottle cage x2", "15.98"),
        ("SHIPPING", "Shipping", "6.50"),
        ("GOODWILL_PCT", "10% goodwill", "2.00"),
        ("GOODWILL_PCT", "25% goodwill", "5.00"),
        ("FULL_REMAINING", "Full remaining refund", "22.48"),
    ]


def test_a_refund_in_flight_in_another_session_blocks_that_order(db):
    first = new_session(db)
    full = option(options_for(mint(db, first), "A-1001"), HandleKind.FULL_REMAINING)
    reserve_handles(db, first.id, [full.handle])  # e.g. held for the merchant

    result = mint(db, new_session(db))
    assert options_for(result, "A-1001") == []  # all 26.48 is already in flight
    assert options_for(result, "A-1003") != []


def test_options_above_the_remaining_amount_are_not_offered(db):
    session = new_session(db)
    refund_in_ledger(db, session, "CAPA1001", 2000)  # 6.48 of 26.48 left
    assert summary(options_for(mint(db, session), "A-1001")) == [
        ("LINE_ITEM", "Bike bell x1", "4.00"),
        ("GOODWILL_PCT", "10% goodwill", "2.00"),
        ("GOODWILL_PCT", "25% goodwill", "5.00"),
        ("FULL_REMAINING", "Full remaining refund", "6.48"),
    ]


def test_only_one_goodwill_gesture_per_order(db):
    session = new_session(db)
    refund_with_handle(db, session, option(options_for(mint(db, session), "A-1001"), HandleKind.GOODWILL_PCT, "10% goodwill"))
    result = mint(db, new_session(db))
    assert HandleKind.GOODWILL_PCT not in {o.kind for o in options_for(result, "A-1001")}
    assert HandleKind.GOODWILL_PCT in {o.kind for o in options_for(result, "A-1003")}


def test_failed_and_cancelled_refunds_do_not_count_as_refunded(db):
    session = new_session(db)
    refund_in_ledger(db, session, "CAPA1001", 2648, status="FAILED")
    refund_in_ledger(db, session, "CAPA1001", 2648, status="CANCELLED")
    assert remaining_refundable_minor(db, "CAPA1001") == 2648
    refund_in_ledger(db, session, "CAPA1001", 400, status="COMPLETED")
    refund_in_ledger(db, session, "CAPA1001", 100, status="PENDING")  # money is on its way: counts
    assert remaining_refundable_minor(db, "CAPA1001") == 2148
    with pytest.raises(LookupError):
        remaining_refundable_minor(db, "NOT-A-CAPTURE")


@pytest.mark.parametrize("ended", ["expired", "closed"])
def test_an_ended_session_cannot_mint(db, ended):
    session = new_session(db, lifetime=timedelta(minutes=-1) if ended == "expired" else timedelta(minutes=30))
    if ended == "closed":
        session.status = SessionStatus.CLOSED
    with pytest.raises(SessionInvalid):
        mint(db, session)


# ---------------------------------------------------------------- reserve / consume / release


@pytest.fixture
def ana(db):
    session = new_session(db)
    return session, options_for(mint(db, session), "A-1001")


def test_reserve_is_all_or_nothing(db, ana):
    session, options = ana
    bell, cage = options[0].handle, options[1].handle
    reserve_handles(db, session.id, [bell])

    with pytest.raises(HandleConflict):
        reserve_handles(db, session.id, [cage, bell])  # bell is already reserved

    assert state_of(db, cage) == HandleState.AVAILABLE  # the failed request reserved nothing


def test_a_handle_cannot_be_reserved_twice(db, ana):
    session, options = ana
    reserve_handles(db, session.id, [options[0].handle])
    with pytest.raises(HandleConflict):
        reserve_handles(db, session.id, [options[0].handle])


def test_another_sessions_handle_cannot_be_reserved(db, ana):
    _ana_session, options = ana
    other = new_session(db)
    with pytest.raises(HandleConflict):
        reserve_handles(db, other.id, [options[0].handle])
    assert state_of(db, options[0].handle) == HandleState.AVAILABLE


def test_an_invented_handle_cannot_be_reserved(db, ana):
    session, options = ana
    with pytest.raises(HandleConflict):
        reserve_handles(db, session.id, [options[0].handle, "h_" + "x" * 22])
    assert state_of(db, options[0].handle) == HandleState.AVAILABLE


def test_consume_needs_a_reserved_handle(db, ana):
    session, options = ana
    with pytest.raises(HandleStateError):
        consume_handles(db, [options[0].handle])  # never reserved
    reserve_handles(db, session.id, [options[0].handle])
    consume_handles(db, [options[0].handle])
    assert state_of(db, options[0].handle) == HandleState.CONSUMED


def test_a_failed_consume_changes_nothing(db, ana):
    session, options = ana
    reserve_handles(db, session.id, [options[0].handle])
    with pytest.raises(HandleStateError):
        consume_handles(db, [options[0].handle, options[1].handle])  # options[1] is not reserved
    assert state_of(db, options[0].handle) == HandleState.RESERVED


def test_release_makes_a_handle_available_again_while_the_session_lives(db, ana):
    session, options = ana
    reserve_handles(db, session.id, [options[0].handle])
    release_handles(db, [options[0].handle], now=NOW + timedelta(minutes=5))
    assert state_of(db, options[0].handle) == HandleState.AVAILABLE


def test_release_after_the_session_ended_expires_the_handle(db, ana):
    session, options = ana
    reserve_handles(db, session.id, [options[0].handle])
    release_handles(db, [options[0].handle], now=NOW + timedelta(hours=2))  # session lasted 30 minutes
    assert state_of(db, options[0].handle) == HandleState.EXPIRED


def test_release_needs_a_reserved_handle(db, ana):
    _session, options = ana
    with pytest.raises(HandleStateError):
        release_handles(db, [options[0].handle], now=NOW)


def test_ending_a_session_expires_unused_handles_but_not_reserved_ones(db, ana):
    session, options = ana
    reserve_handles(db, session.id, [options[0].handle])  # e.g. waiting for the merchant
    expire_session(db, session)
    assert state_of(db, options[0].handle) == HandleState.RESERVED
    assert {state_of(db, o.handle) for o in options[1:]} == {HandleState.EXPIRED}
    assert session.status == SessionStatus.EXPIRED


def test_load_handles(db, ana):
    _session, options = ana
    found = load_handles(db, [options[0].handle, options[0].handle, "h_" + "y" * 22])
    assert list(found) == [options[0].handle]  # duplicates collapse, unknown ids are absent
    with pytest.raises(ValueError):
        load_handles(db, [f"h_{i}" for i in range(MAX_HANDLES_PER_REQUEST + 1)])
    with pytest.raises(ValueError):
        load_handles(db, [])
    with pytest.raises(ValueError):
        load_handles(db, options[0].handle)  # a bare string, not a list
