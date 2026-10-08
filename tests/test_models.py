"""Model tests on an in-memory SQLite database, using the same engine setup as the app."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError

from wardlock.db import init_db, make_engine, make_session_factory
from wardlock.models import (
    AgentSession,
    Alert,
    AlertKind,
    Customer,
    Decision,
    Handle,
    HandleKind,
    HandleState,
    Hold,
    HoldStatus,
    LedgerEntry,
    Order,
    OrderStatus,
    Verdict,
    WebhookEvent,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = make_engine("sqlite://")
    init_db(engine)
    with make_session_factory(engine)() as session:
        yield session
    engine.dispose()


def add_customer(db, customer_id="cust_ana", email="ana@example.com"):
    db.add(Customer(id=customer_id, first_name="Ana", email=email))
    db.flush()


def make_order(**overrides):
    fields = dict(
        customer_id="cust_ana",
        order_ref="A-1001",
        status=OrderStatus.DELIVERED,
        placed_at=date(2026, 9, 20),
        paypal_order_id="5O190127TN364715T",
        capture_id="2GG279541U471931P",
        currency="USD",
        captured_minor=2648,
        items_json=[{"item_ref": "bell", "name": "Bike bell", "unit_minor": 400, "quantity": 1}],
        shipping_minor=650,
        captured_at=NOW,
    )
    fields.update(overrides)
    return Order(**fields)


def add_session(db):
    session = AgentSession(
        ticket_id="T-0001",
        customer_id="cust_ana",
        ticket_body="My bike bell arrived scratched",
        token_hash="a" * 64,
        expires_at=NOW + timedelta(minutes=30),
    )
    db.add(session)
    db.flush()
    return session


def make_decision(session_id, **overrides):
    fields = dict(
        session_id=session_id,
        tool="request_refund",
        request_json={"refund_handles": ["h_x"], "reason_code": "DAMAGED"},
        verdict=Verdict.ALLOW,
        reason_codes=[],
        capture_id="2GG279541U471931P",
        amount_minor=400,
        currency="USD",
        idempotency_key="0b6f1c9e-6f2a-4c55-9d3e-1f2a3b4c5d6e",
        policy_version="1",
    )
    fields.update(overrides)
    return Decision(**fields)


def test_every_table_round_trips(db):
    add_customer(db)
    order = make_order()
    db.add(order)
    session = add_session(db)
    handle = Handle(
        session_id=session.id, order_id=order.id, capture_id=order.capture_id, kind=HandleKind.LINE_ITEM,
        item_ref="bell", label="Bike bell x1", amount_minor=400, currency="USD",
    )
    decision = make_decision(session.id)
    db.add_all([handle, decision])
    db.flush()
    db.add_all([
        Hold(decision_id=decision.id, frozen_request_json={"amount_minor": 400}),
        LedgerEntry(decision_id=decision.id, capture_id=order.capture_id, paypal_refund_id="1JU08902781691411",
                    amount_minor=400, currency="USD", status="COMPLETED"),
        WebhookEvent(event_id="WH-1", event_type="PAYMENT.CAPTURE.REFUNDED", resource_id="1JU08902781691411",
                     verified=True, payload_json={"id": "WH-1"}),
        Alert(kind=AlertKind.OUT_OF_BAND_REFUND, details_json={"refund_id": "X"}),
    ])
    db.commit()
    db.expire_all()  # force every read below to come from the database

    stored = db.get(Order, order.id)
    assert stored.items_json[0]["unit_minor"] == 400
    assert isinstance(stored.items_json[0]["unit_minor"], int)
    assert stored.status is OrderStatus.DELIVERED
    assert db.get(Handle, handle.handle_id).state is HandleState.AVAILABLE
    assert db.get(Decision, decision.id).verdict is Verdict.ALLOW
    assert db.query(Hold).one().status is HoldStatus.PENDING
    assert db.query(LedgerEntry).one().paypal_refund_id == "1JU08902781691411"
    assert db.query(WebhookEvent).one().verified is True
    assert db.query(Alert).one().acknowledged is False


def test_datetimes_come_back_as_aware_utc(db):
    add_customer(db)
    india = timezone(timedelta(hours=5, minutes=30))
    db.add(make_order(captured_at=datetime(2026, 10, 7, 17, 30, tzinfo=india)))
    db.commit()
    db.expire_all()

    captured_at = db.query(Order).one().captured_at
    assert captured_at == NOW  # 17:30 IST is 12:00 UTC
    assert captured_at.tzinfo == timezone.utc


def test_naive_datetimes_are_rejected(db):
    add_customer(db)
    db.add(make_order(captured_at=datetime(2026, 10, 7, 12, 0)))
    with pytest.raises(StatementError, match="naive datetime"):
        db.flush()


def test_foreign_keys_are_enforced(db):
    db.add(make_order(customer_id="cust_nobody"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_order_ref_and_capture_id_are_unique(db):
    add_customer(db)
    db.add(make_order())
    db.flush()
    db.add(make_order(paypal_order_id="OTHER", capture_id="OTHER"))  # same order_ref
    with pytest.raises(IntegrityError):
        db.flush()


def test_paypal_refund_id_is_unique_in_ledger(db):
    add_customer(db)
    db.add(make_order())
    session = add_session(db)
    first, second = make_decision(session.id), make_decision(session.id, idempotency_key="other-key")
    db.add_all([first, second])
    db.flush()
    for decision in (first, second):
        db.add(LedgerEntry(decision_id=decision.id, capture_id="C", paypal_refund_id="SAME",
                           amount_minor=400, currency="USD", status="COMPLETED"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_idempotency_key_is_unique_but_may_repeat_as_null(db):
    add_customer(db)
    session = add_session(db)
    # BLOCKed decisions never call PayPal, so they have no key; NULLs never clash.
    db.add_all([make_decision(session.id, verdict=Verdict.BLOCK, idempotency_key=None, amount_minor=None)
                for _ in range(2)])
    db.flush()
    db.add_all([make_decision(session.id), make_decision(session.id)])  # same key twice
    with pytest.raises(IntegrityError):
        db.flush()


def test_webhook_event_ids_are_unique(db):
    for _ in range(2):
        db.add(WebhookEvent(event_id="WH-1", event_type="X", verified=False, payload_json={}))
    with pytest.raises(IntegrityError):
        db.flush()


@pytest.mark.parametrize("overrides", [{"shipping_minor": -1}, {"captured_minor": 0}])
def test_order_amounts_are_checked_by_the_database(db, overrides):
    add_customer(db)
    db.add(make_order(**overrides))
    with pytest.raises(IntegrityError):
        db.flush()


def test_handle_amount_must_be_positive(db):
    add_customer(db)
    order = make_order()
    db.add(order)
    session = add_session(db)
    db.add(Handle(session_id=session.id, order_id=order.id, capture_id="C", kind=HandleKind.SHIPPING,
                  label="Shipping", amount_minor=0, currency="USD"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_unknown_enum_values_are_rejected_in_python(db):
    db.add(Alert(kind="NOT_A_KIND", details_json={}))
    with pytest.raises(StatementError):
        db.flush()


def test_unknown_enum_values_are_rejected_by_the_database(db):
    # Even raw SQL that bypasses SQLAlchemy cannot store a made-up state.
    with pytest.raises(IntegrityError):
        db.execute(text(
            "INSERT INTO alerts (id, kind, details_json, created_at, acknowledged) "
            "VALUES ('alr_x', 'NOT_A_KIND', '{}', '2026-10-07 12:00:00', 0)"
        ))


def test_ids_have_readable_prefixes(db):
    add_customer(db)
    order = make_order()
    db.add(order)
    session = add_session(db)
    handle = Handle(session_id=session.id, order_id=order.id, capture_id="C", kind=HandleKind.SHIPPING,
                    label="Shipping", amount_minor=650, currency="USD")
    decision = make_decision(session.id)
    db.add_all([handle, decision])
    db.flush()

    assert order.id.startswith("ord_")
    assert session.id.startswith("ses_")
    assert decision.id.startswith("dec_")
    assert handle.handle_id.startswith("h_") and len(handle.handle_id) == 2 + 22  # token_urlsafe(16)


def test_parents_and_children_can_be_saved_in_one_flush(db):
    """P5 saves a decision with its hold or ledger row in one go. SQLAlchemy must insert
    parents first, which it only does when the models declare their relationships."""
    customer = Customer(id="cust_ana", first_name="Ana", email="ana@example.com")
    order = make_order()
    session = AgentSession(id="ses_x", ticket_id="T-1", customer_id="cust_ana", ticket_body="hi",
                           token_hash="b" * 64, expires_at=NOW + timedelta(minutes=30))
    order.id = "ord_x"
    handle = Handle(session_id="ses_x", order_id="ord_x", capture_id=order.capture_id, kind=HandleKind.SHIPPING,
                    label="Shipping", amount_minor=650, currency="USD")
    decision = make_decision("ses_x", id="dec_x")
    hold = Hold(decision_id="dec_x", frozen_request_json={})
    ledger = LedgerEntry(decision_id="dec_x", capture_id=order.capture_id, paypal_refund_id="R1",
                         amount_minor=650, currency="USD", status="COMPLETED")

    db.add_all([ledger, hold, decision, handle, session, order, customer])  # children first, on purpose
    db.flush()

    assert db.get(Hold, hold.id).decision_id == "dec_x"
