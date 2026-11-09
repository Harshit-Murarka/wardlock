"""Per-unit refund options: one cage of two can be refunded, then the other, but never a third.

A line-item handle covers `units` units, and its amount is always unit x units, so the amounts
already used tell Wardlock how many units are left (no extra column needed).
"""

from decimal import Decimal

import pytest
from support import NOW, make_order

from wardlock.enums import HandleKind, HandleState, ReasonCode
from wardlock.handles import HandleConflict, derivable_amounts, mint_handles, reserve_handles
from wardlock.models import Handle, Order
from wardlock.sessions import open_session, resolve_session


def new_session(wl, customer="cust_ana"):
    with wl.factory() as db, db.begin():
        opened = open_session(db, ticket_id="T-U", customer_id=customer, ticket_body="One cage is broken.",
                              ttl_minutes=30, now=NOW)
    return opened.session_id, opened.token


def options(wl, token, order_ref="A-1001"):
    """{label: handle} for one order, freshly minted for this session."""
    with wl.factory() as db, db.begin():
        session = resolve_session(db, token, NOW)
        orders = mint_handles(db, session, goodwill_percentages=[10, 25], now=NOW)
    return {o.label: o.handle for order in orders if order.order_ref == order_ref for o in order.refund_options}


def cage_labels(opts):
    return sorted(label for label in opts if label.startswith("Bottle cage"))


async def refund(wl, session_id, handles, reason=ReasonCode.DAMAGED):
    return await wl.executor.request_refund(session_id, handles, reason)


# ---------------------------------------------------------------- what is offered


async def test_one_unit_then_the_other_then_nothing(wl):
    session_id, token = new_session(wl)
    first = await refund(wl, session_id, [options(wl, token)["Bottle cage x1 (of 2)"]])
    assert (first.status, first.amount) == ("ALLOWED", Decimal("7.99"))

    left = options(wl, token)
    assert cage_labels(left) == ["Bottle cage x1 (of 2)"]  # only the second cage remains

    second = await refund(wl, session_id, [left["Bottle cage x1 (of 2)"]])
    assert (second.status, second.amount) == ("ALLOWED", Decimal("7.99"))

    _other_session, other_token = new_session(wl)  # not even another conversation sees a third cage
    assert cage_labels(options(wl, other_token)) == []
    assert [body["amount"]["value"] for _capture, body in wl.fake.calls] == ["7.99", "7.99"]


async def test_after_the_full_line_is_refunded_no_unit_option_remains(wl):
    session_id, token = new_session(wl)
    outcome = await refund(wl, session_id, [options(wl, token)["Bottle cage x2"]])
    assert outcome.status == "ALLOWED"
    assert cage_labels(options(wl, token)) == []


# ---------------------------------------------------------------- spending more units than exist


def test_two_options_for_the_same_item_cannot_be_reserved_together(wl):
    session_id, token = new_session(wl)
    opts = options(wl, token)
    with wl.factory() as db, db.begin(), pytest.raises(HandleConflict):
        reserve_handles(db, session_id, [opts["Bottle cage x1 (of 2)"], opts["Bottle cage x2"]])  # 3 cages of 2


def test_another_sessions_copies_cannot_overspend_the_units(wl):
    first_id, first_token = new_session(wl)
    second_id, second_token = new_session(wl)
    first, second = options(wl, first_token), options(wl, second_token)

    with wl.factory() as db, db.begin():
        reserve_handles(db, first_id, [first["Bottle cage x2"]])  # e.g. held for the merchant
    with wl.factory() as db, db.begin(), pytest.raises(HandleConflict):
        reserve_handles(db, second_id, [second["Bottle cage x1 (of 2)"]])


def test_one_unit_each_from_two_sessions_is_fine_but_a_third_is_not(wl):
    sessions = [new_session(wl) for _ in range(3)]
    copies = [options(wl, token)["Bottle cage x1 (of 2)"] for _id, token in sessions]
    for (session_id, _token), handle in zip(sessions[:2], copies[:2], strict=True):
        with wl.factory() as db, db.begin():
            reserve_handles(db, session_id, [handle])
    with wl.factory() as db, db.begin(), pytest.raises(HandleConflict):
        reserve_handles(db, sessions[2][0], [copies[2]])


async def test_the_engine_refuses_two_options_for_one_item(wl):
    session_id, token = new_session(wl)
    opts = options(wl, token)
    outcome = await refund(wl, session_id, [opts["Bottle cage x1 (of 2)"], opts["Bottle cage x2"]])
    assert (outcome.status, outcome.reason_codes) == ("BLOCKED", ("INVALID_COMBINATION",))
    mixed = await refund(wl, session_id, [opts["Bike bell x1"], opts["Bottle cage x1 (of 2)"]])  # two items: fine
    assert (mixed.status, mixed.amount) == ("ALLOWED", Decimal("11.99"))
    assert wl.fake.calls and wl.fake.calls[-1][1]["amount"]["value"] == "11.99"


# ---------------------------------------------------------------- compat amounts


async def test_a_raw_amount_for_one_unit_is_derivable_and_stops_at_the_quantity(wl):
    with wl.factory() as db:
        order = db.query(Order).filter_by(order_ref="A-1001").one()
        assert derivable_amounts(db, order, [10, 25])[799] == frozenset({HandleKind.LINE_ITEM})

    session_id, _token = new_session(wl)
    raw = [await wl.executor.request_refund_raw(session_id, "CAPA1001", Decimal("7.99"), "USD", ReasonCode.DAMAGED)
           for _ in range(3)]
    statuses = [r.status for r in raw]
    assert statuses[:2] == ["ALLOWED", "ALLOWED"]
    assert statuses[2] != "ALLOWED"  # a third cage does not exist
    assert [body["amount"]["value"] for _capture, body in wl.fake.calls] == ["7.99", "7.99"]


def test_handles_record_the_units_they_cover_through_their_amount(wl):
    with wl.factory() as db, db.begin():  # Ben's three water bottles (not in the shared fixture)
        db.add(make_order("B-2002", "cust_ben", [("bottle", "Water bottle", 999, 3)], 450, "CAPB2002"))
    _session_id, token = new_session(wl, "cust_ben")
    opts = options(wl, token, "B-2002")
    assert sorted(label for label in opts if label.startswith("Water bottle")) == [
        "Water bottle x1 (of 3)", "Water bottle x2 (of 3)", "Water bottle x3"]
    with wl.factory() as db:
        amounts = {h.label: h.amount_minor for h in db.query(Handle).filter(Handle.state == HandleState.AVAILABLE)}
    assert (amounts["Water bottle x1 (of 3)"], amounts["Water bottle x2 (of 3)"], amounts["Water bottle x3"]) == (
        999, 1998, 2997)


def test_an_option_for_an_item_the_order_no_longer_has_is_refused(wl):
    """If the order's items change after options were minted, an option for a vanished item never pays."""
    session_id, token = new_session(wl)
    cage = options(wl, token)["Bottle cage x1 (of 2)"]
    with wl.factory() as db, db.begin():
        order = db.query(Order).filter_by(order_ref="A-1001").one()
        order.items_json = [i for i in order.items_json if i["item_ref"] != "cage"]
    with wl.factory() as db, db.begin(), pytest.raises(HandleConflict):
        reserve_handles(db, session_id, [cage])
