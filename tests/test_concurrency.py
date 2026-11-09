"""Concurrent refunds of the same thing must refund it once (run this on Postgres too).

Each request comes from its own conversation with its own copy of the option, so only the
"an item is spent once" rule (row lock + quantity check) stands between them and a double refund.
"""

import asyncio
from decimal import Decimal

from support import NOW

from wardlock.enums import ReasonCode
from wardlock.handles import mint_handles
from wardlock.sessions import open_session, resolve_session

RACERS = 8


def copies(wl, label, n=RACERS):
    """n separate conversations for Ana, each holding its own handle for `label`."""
    out = []
    for i in range(n):
        with wl.factory() as db, db.begin():
            opened = open_session(db, ticket_id=f"T-R{i}", customer_id="cust_ana", ticket_body="Bell scratched.",
                                  ttl_minutes=30, now=NOW)
            session = resolve_session(db, opened.token, NOW)
            orders = mint_handles(db, session, goodwill_percentages=[10, 25], now=NOW)
            handle = next(o.handle for order in orders for o in order.refund_options if o.label == label)
        out.append((opened.session_id, handle))
    return out


async def race(wl, racers, reason=ReasonCode.DAMAGED):
    return await asyncio.gather(*(wl.executor.request_refund(s, [h], reason) for s, h in racers))


async def test_eight_conversations_racing_for_one_bell_refund_it_once(wl):
    outcomes = await race(wl, copies(wl, "Bike bell x1"))
    allowed = [o for o in outcomes if o.status == "ALLOWED"]
    assert len(allowed) == 1 and allowed[0].amount == Decimal("4.00")
    assert all(o.status == "BLOCKED" for o in outcomes if o is not allowed[0])
    assert len(wl.fake.calls) == 1


async def test_racing_for_single_cages_refunds_at_most_the_two_that_exist(wl):
    outcomes = await race(wl, copies(wl, "Bottle cage x1 (of 2)"))
    assert sum(o.status == "ALLOWED" for o in outcomes) == 2
    assert [body["amount"]["value"] for _c, body in wl.fake.calls] == ["7.99", "7.99"]


async def test_racing_full_refunds_never_exceed_the_capture(wl):
    outcomes = await race(wl, copies(wl, "Full remaining refund"), ReasonCode.NOT_RECEIVED)
    assert sum(o.status == "ALLOWED" for o in outcomes) == 1
    assert sum(Decimal(body["amount"]["value"]) for _c, body in wl.fake.calls) == Decimal("26.48")


import threading  # noqa: E402

import pytest  # noqa: E402
from support import TEST_DATABASE_URL  # noqa: E402

from wardlock.handles import HandleConflict, reserve_handles  # noqa: E402


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="needs real parallel transactions (Postgres)")
@pytest.mark.parametrize("label", ["Bike bell x1", "Bottle cage x2", "Shipping"])
def test_truly_parallel_reservations_lock_the_order_row(wl, label, monkeypatch):
    """8 threads, 8 connections, one moment: the FOR UPDATE row lock must let exactly one through.

    A short pause between the "already spent?" check and the reservation widens the race
    window, so without the lock several threads really do get through (checked by mutation).
    """
    import time

    from wardlock import handles

    real_move = handles._move

    def slow_move(db, ids, from_state, to_state, *extra):
        if to_state == handles.HandleState.RESERVED:
            time.sleep(0.2)
        return real_move(db, ids, from_state, to_state, *extra)

    monkeypatch.setattr(handles, "_move", slow_move)
    racers = copies(wl, label)
    start, results = threading.Barrier(len(racers)), []

    def reserve(session_id, handle):
        start.wait()
        try:
            with wl.factory() as db, db.begin():
                reserve_handles(db, session_id, [handle])
            results.append("reserved")
        except HandleConflict:
            results.append("conflict")

    threads = [threading.Thread(target=reserve, args=r) for r in racers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results) == ["conflict"] * (len(racers) - 1) + ["reserved"]
