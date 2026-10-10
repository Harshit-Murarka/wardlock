"""Sandbox seeding, run against a fake PayPal Orders API (respx) and an in-memory database."""

import importlib.util
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from wardlock.db import init_db, make_engine, make_session_factory
from wardlock.models import Order
from wardlock.paypal.client import LIVE_BASE_URL, PayPalClient
from wardlock.seed import (
    DEFAULT_SANDBOX_CARD_NUMBER,
    SandboxCard,
    SeedError,
    build_order_payload,
    load_seed,
    load_state,
    restore_db,
    run_seed,
    save_state,
    select_orders,
)

SANDBOX = "https://api-m.sandbox.paypal.com"
REPO = Path(__file__).resolve().parent.parent

CUSTOMERS = [
    {"id": "cust_ana", "first_name": "Ana", "email": "ana@example.com"},
    {"id": "cust_ben", "first_name": "Ben", "email": "ben@example.com"},
]
A_1001 = {
    "order_ref": "A-1001", "customer_id": "cust_ana", "placed_at": "2026-09-20", "status": "DELIVERED",
    "currency": "USD", "shipping": "6.50",
    "items": [
        {"item_ref": "bell", "name": "Bike bell", "unit_amount": "4.00", "quantity": 1},
        {"item_ref": "cage", "name": "Bottle cage", "unit_amount": "7.99", "quantity": 2},
    ],
}
B_2003 = {
    "order_ref": "B-2003", "customer_id": "cust_ben", "placed_at": "2026-10-02", "status": "DELIVERED",
    "currency": "USD", "shipping": "0.00",
    "items": [{"item_ref": "lube", "name": "Chain lube", "unit_amount": "12.50", "quantity": 1}],
}


# ---------------------------------------------------------------- fakes and fixtures


class FakePayPal:
    """Just enough of PayPal's Orders v2 API for the seeding flow."""

    def __init__(self):
        self.orders = {}  # paypal order id -> {"ref", "status", "amount"}
        self.create_request_ids = []
        self.card_numbers_sent = []
        self.capture_request_ids = []
        self.capture_override = {}  # merged into every capture PayPal returns
        self.capture_refusals = {}  # order_ref -> error JSON for a 422 on capture
        self.capture_unfinished = False  # capture answers 202 and does not complete
        self.card_result = "COMPLETED"  # what a card-paid create returns (e.g. 3D Secure needed)

    def create(self, request):
        body = json.loads(request.content)
        unit = body["purchase_units"][0]
        order_id = f"ORDER{len(self.orders) + 1}"
        self.create_request_ids.append(request.headers["PayPal-Request-Id"])
        card = body["payment_source"].get("card")
        if card:
            # Single-step create: PayPal takes the payment right away. No approval link.
            self.card_numbers_sent.append(card["number"])
            self.orders[order_id] = {"ref": unit["reference_id"], "status": self.card_result,
                                     "amount": unit["amount"]}
            return httpx.Response(201, json={"id": order_id, "status": self.card_result, "links": []})
        self.orders[order_id] = {"ref": unit["reference_id"], "status": "PAYER_ACTION_REQUIRED",
                                 "amount": unit["amount"]}
        return httpx.Response(201, json={
            "id": order_id, "status": "PAYER_ACTION_REQUIRED",
            "links": [{"rel": "payer-action", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"}],
        })

    def approve(self, *refs):
        """What the buyer does in the browser."""
        for order in self.orders.values():
            if order["status"] == "PAYER_ACTION_REQUIRED" and (not refs or order["ref"] in refs):
                order["status"] = "APPROVED"

    def get(self, request, order_id):
        if order_id not in self.orders:
            return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
        return httpx.Response(200, json=self.order_json(order_id))

    def capture(self, request, order_id):
        self.capture_request_ids.append(request.headers["PayPal-Request-Id"])
        order = self.orders[order_id]
        if order["ref"] in self.capture_refusals:
            return httpx.Response(422, json=self.capture_refusals[order["ref"]])
        if self.capture_unfinished:
            return httpx.Response(202, json={"id": order_id, "status": "APPROVED"})
        order["status"] = "COMPLETED"
        return httpx.Response(201, json=self.order_json(order_id))

    def order_json(self, order_id):
        order = self.orders[order_id]
        body = {"id": order_id, "status": order["status"]}
        if order["status"] == "COMPLETED":
            capture = {
                "id": f"CAP{order_id}", "status": "COMPLETED",
                "amount": {"currency_code": order["amount"]["currency_code"], "value": order["amount"]["value"]},
                "create_time": "2026-10-07T10:00:00Z",
                **self.capture_override,
            }
            body["purchase_units"] = [{"reference_id": order["ref"], "payments": {"captures": [capture]}}]
        return body


@pytest.fixture
def fake():
    fake = FakePayPal()
    with respx.mock(base_url=SANDBOX, assert_all_called=False) as router:
        router.post("/v1/oauth2/token").respond(200, json={"access_token": "tok", "expires_in": 3600})
        router.post("/v2/checkout/orders").mock(side_effect=fake.create)
        router.get(path__regex=r"^/v2/checkout/orders/(?P<order_id>[A-Z0-9]+)$").mock(side_effect=fake.get)
        router.post(path__regex=r"^/v2/checkout/orders/(?P<order_id>[A-Z0-9]+)/capture$").mock(
            side_effect=fake.capture
        )
        yield fake


def make_db():
    engine = make_engine("sqlite://")
    init_db(engine)
    return engine, make_session_factory(engine)


@pytest.fixture
def db():
    engine, factory = make_db()
    yield factory
    engine.dispose()


@pytest.fixture
def state(tmp_path):
    return tmp_path / "sandbox_state.json"


def seed_data(tmp_path, orders=(A_1001, B_2003), customers=CUSTOMERS):
    (tmp_path / "customers.json").write_text(json.dumps(customers))
    (tmp_path / "orders.json").write_text(json.dumps(list(orders)))
    return load_seed(tmp_path / "customers.json", tmp_path / "orders.json")


async def seed_once(data, fake, state, db, approve=(), card=None):
    """Run the seeder. `approve` lists the orders the 'buyer' approves (True = all)."""
    prompts = []

    def wait_for_approval(pending):
        prompts.append(pending)
        if approve is True:
            fake.approve()
        elif approve:
            fake.approve(*approve)

    async def no_sleep(_seconds):
        pass

    async with PayPalClient(SANDBOX, "id", "secret", sleep=no_sleep) as paypal:
        report = await run_seed(data, paypal, state, db, wait_for_approval, card)
    return report, prompts


def stored_orders(db):
    with db() as session:
        return {o.order_ref: o for o in session.scalars(select(Order))}


# ---------------------------------------------------------------- seed files


def test_real_seed_files_are_valid():
    data = load_seed(REPO / "seed" / "customers.json", REPO / "seed" / "orders.json")
    totals = {o.order_ref: o.total for o in data.orders}

    assert len(data.orders) == 14
    assert totals["A-1001"] == Decimal("26.48")  # the SPEC §3.1 example
    assert totals["C-3002"] == Decimal("59.97")  # 3 x 19.99 with no float error
    assert totals["A-1002"] == totals["B-2001"] == Decimal("189.00")  # demo HOLD scene and injection target
    # QA-only orders: Z-9001 for smoke refunds, Z-9002 for the live executor check.
    assert {o.order_ref for o in data.orders if o.customer_id == "cust_qa"} == {"Z-9001", "Z-9002"}
    assert {o.currency for o in data.orders} == {"USD"}


@pytest.mark.parametrize(
    ("orders", "customers"),
    [
        ([{**A_1001, "customer_id": "cust_nobody"}], CUSTOMERS),
        ([A_1001, A_1001], CUSTOMERS),  # duplicate order_ref
        ([{**A_1001, "items": [A_1001["items"][0], A_1001["items"][0]]}], CUSTOMERS),  # duplicate item_ref
        ([{**A_1001, "shipping": "6.505"}], CUSTOMERS),  # would need rounding
        ([{**A_1001, "items": [{**A_1001["items"][0], "quantity": 0}]}], CUSTOMERS),
        ([{**A_1001, "total": "26.48"}], CUSTOMERS),  # unknown field: totals are computed, never typed
        ([A_1001], [{**CUSTOMERS[0], "email": "ana@gmail.com"}]),  # real-looking email
    ],
)
def test_bad_seed_files_are_rejected(tmp_path, orders, customers):
    with pytest.raises(SeedError):
        seed_data(tmp_path, orders=orders, customers=customers)


def test_json_numbers_are_read_exactly_not_through_float(tmp_path):
    # As a float, 4.0000000000000001 silently becomes 4.0 and would be accepted.
    # Read exactly as a Decimal, it has too many decimal places for USD and is rejected.
    raw = json.dumps([A_1001]).replace('"unit_amount": "4.00"', '"unit_amount": 4.0000000000000001')
    (tmp_path / "customers.json").write_text(json.dumps(CUSTOMERS))
    (tmp_path / "orders.json").write_text(raw)
    with pytest.raises(SeedError):
        load_seed(tmp_path / "customers.json", tmp_path / "orders.json")


def test_order_payload_matches_paypal_orders_v2(tmp_path):
    payload = build_order_payload(seed_data(tmp_path).orders[0])
    unit = payload["purchase_units"][0]

    assert payload["intent"] == "CAPTURE"
    assert unit["reference_id"] == "A-1001"
    assert unit["amount"] == {
        "currency_code": "USD", "value": "26.48",
        "breakdown": {"item_total": {"currency_code": "USD", "value": "19.98"},
                      "shipping": {"currency_code": "USD", "value": "6.50"}},
    }
    assert unit["items"][1] == {
        "name": "Bottle cage", "sku": "cage", "quantity": "2",  # a string, as PayPal requires
        "unit_amount": {"currency_code": "USD", "value": "7.99"},
    }
    context = payload["payment_source"]["paypal"]["experience_context"]
    assert context["shipping_preference"] == "NO_SHIPPING"
    assert context["return_url"].startswith("https://")


def test_free_shipping_leaves_shipping_out_of_the_breakdown(tmp_path):
    payload = build_order_payload(seed_data(tmp_path).orders[1])
    assert payload["purchase_units"][0]["amount"]["breakdown"] == {
        "item_total": {"currency_code": "USD", "value": "12.50"}
    }


def test_dry_run_sends_nothing_and_touches_no_database(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("seed_sandbox", REPO / "scripts" / "seed_sandbox.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script, "get_engine", lambda: pytest.fail("dry run opened the database"))

    with respx.mock():  # no routes: any HTTP request would fail the test
        code = script.main([
            "--dry-run",
            "--customers", str(REPO / "seed" / "customers.json"),
            "--orders", str(REPO / "seed" / "orders.json"),
        ])

    out = capsys.readouterr().out
    assert code == 0
    assert "14 orders are valid (paid with a sandbox test card). Nothing was sent." in out
    # Card details are masked even though they are public test values.
    assert "************0004" in out
    assert DEFAULT_SANDBOX_CARD_NUMBER not in out


# ---------------------------------------------------------------- the seeding run


async def test_full_flow_creates_captures_and_writes_rows(tmp_path, fake, state, db):
    report, prompts = await seed_once(seed_data(tmp_path), fake, state, db, approve=True)

    assert report.created == report.captured == report.rows_written == ["A-1001", "B-2003"]
    assert report.problems == report.waiting == []
    assert [ref for ref, _url in prompts[0]] == ["A-1001", "B-2003"]
    assert re.fullmatch(r"seed-A-1001-[0-9a-f]{8}-1", fake.create_request_ids[0])
    assert fake.capture_request_ids == ["seed-capture-ORDER1", "seed-capture-ORDER2"]

    rows = stored_orders(db)
    a = rows["A-1001"]
    assert (a.capture_id, a.captured_minor, a.shipping_minor, a.customer_id) == ("CAPORDER1", 2648, 650, "cust_ana")
    assert a.items_json == [
        {"item_ref": "bell", "name": "Bike bell", "unit_minor": 400, "quantity": 1},
        {"item_ref": "cage", "name": "Bottle cage", "unit_minor": 799, "quantity": 2},
    ]
    assert a.captured_at == datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    assert rows["B-2003"].shipping_minor == 0


async def test_running_again_changes_nothing(tmp_path, fake, state, db):
    data = seed_data(tmp_path)
    await seed_once(data, fake, state, db, approve=True)

    report, prompts = await seed_once(data, fake, state, db, approve=True)

    assert report.created == report.captured == report.rows_written == []
    assert prompts == []  # nothing left to approve
    assert len(fake.create_request_ids) == 2
    assert len(fake.capture_request_ids) == 2
    assert len(stored_orders(db)) == 2


async def test_unapproved_orders_wait_and_finish_on_the_next_run(tmp_path, fake, state, db):
    data = seed_data(tmp_path)
    first, _ = await seed_once(data, fake, state, db, approve=["A-1001"])
    assert (first.captured, first.waiting) == (["A-1001"], ["B-2003"])
    assert list(stored_orders(db)) == ["A-1001"]

    second, prompts = await seed_once(data, fake, state, db, approve=True)

    assert second.created == []  # the existing PayPal order is reused
    assert [ref for ref, _url in prompts[0]] == ["B-2003"]
    assert second.captured == ["B-2003"]
    assert sorted(stored_orders(db)) == ["A-1001", "B-2003"]


@pytest.mark.parametrize(
    "override",
    [
        {"status": "PENDING"},
        {"amount": {"currency_code": "USD", "value": "99.99"}},
        {"amount": {"currency_code": "EUR", "value": "26.48"}},
        {"create_time": "not a date"},
        {"create_time": "2026-10-07T10:00:00"},  # no timezone
        {"amount": "26.48"},  # not an object
        {"id": "../../v1/x"},
    ],
)
async def test_suspicious_captures_write_no_rows(tmp_path, fake, state, db, override):
    fake.capture_override = override
    report, _ = await seed_once(seed_data(tmp_path, orders=[A_1001]), fake, state, db, approve=True)

    assert report.captured == report.rows_written == []
    assert len(report.problems) == 1
    assert load_state(state).orders["A-1001"].capture_id is None
    assert stored_orders(db) == {}


async def test_unfinished_capture_is_retried_on_the_next_run(tmp_path, fake, state, db):
    data = seed_data(tmp_path, orders=[A_1001])
    fake.capture_unfinished = True
    first, _ = await seed_once(data, fake, state, db, approve=True)
    assert first.captured == [] and "no capture yet" in first.problems[0]

    fake.capture_unfinished = False
    second, _ = await seed_once(data, fake, state, db)

    assert second.captured == ["A-1001"]
    assert fake.capture_request_ids == ["seed-capture-ORDER1"] * 2  # same id both times


async def test_refused_capture_is_reported_and_others_continue(tmp_path, fake, state, db):
    fake.capture_refusals["A-1001"] = {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "INSTRUMENT_DECLINED"}]}
    report, _ = await seed_once(seed_data(tmp_path), fake, state, db, approve=True)

    assert report.captured == ["B-2003"]
    assert "INSTRUMENT_DECLINED" in report.problems[0]


async def test_expired_order_is_recreated_with_a_new_request_id(tmp_path, fake, state, db):
    data = seed_data(tmp_path, orders=[A_1001])
    await seed_once(data, fake, state, db)  # never approved
    fake.orders.clear()  # PayPal forgets it

    second, _ = await seed_once(data, fake, state, db)
    assert "no longer knows" in second.problems[0]

    third, _ = await seed_once(data, fake, state, db, approve=True)
    assert third.created == third.captured == ["A-1001"]
    assert fake.create_request_ids[0].endswith("-1") and fake.create_request_ids[1].endswith("-2")


async def test_order_already_captured_is_read_not_captured_again(tmp_path, fake, state, db):
    data = seed_data(tmp_path, orders=[A_1001])
    await seed_once(data, fake, state, db, approve=True)
    # Simulate a crash after PayPal captured but before the state file was saved.
    saved = load_state(state)
    saved.orders["A-1001"].capture_id = None
    save_state(saved, state)

    report, _ = await seed_once(data, fake, state, db)

    assert report.captured == ["A-1001"]
    assert len(fake.capture_request_ids) == 1  # no second capture call
    assert len(stored_orders(db)) == 1


async def test_restore_db_rebuilds_rows_without_paypal(tmp_path, fake, state, db):
    data = seed_data(tmp_path)
    await seed_once(data, fake, state, db, approve=True)
    engine, fresh_db = make_db()

    with respx.mock():  # no routes: any HTTP request would fail the test
        written = restore_db(data, state, fresh_db)

    assert written == ["A-1001", "B-2003"]
    before, after = stored_orders(db), stored_orders(fresh_db)
    for ref in before:
        assert (after[ref].capture_id, after[ref].captured_minor, after[ref].captured_at) == (
            before[ref].capture_id, before[ref].captured_minor, before[ref].captured_at)
    engine.dispose()


async def test_editing_an_order_after_capture_is_refused(tmp_path, fake, state, db):
    await seed_once(seed_data(tmp_path), fake, state, db, approve=True)
    edited = seed_data(tmp_path, orders=[{**A_1001, "shipping": "9.99"}, B_2003])

    with pytest.raises(SeedError, match="edited after it was captured"):
        await seed_once(edited, fake, state, db)
    with pytest.raises(SeedError, match="edited after it was captured"):
        restore_db(edited, state, db)


# ---------------------------------------------------------------- card mode (no approvals)


def test_card_payload_pays_with_the_sandbox_test_card(tmp_path):
    payload = build_order_payload(seed_data(tmp_path).orders[0], SandboxCard())
    card = payload["payment_source"]["card"]

    assert list(payload["payment_source"]) == ["card"]  # no browser experience context
    assert card["number"] == DEFAULT_SANDBOX_CARD_NUMBER
    assert re.fullmatch(r"20\d\d-12", card["expiry"])
    assert int(card["expiry"][:4]) > datetime.now(timezone.utc).year  # always in the future
    assert card["billing_address"]["country_code"] == "US"


@pytest.mark.parametrize(
    "fields",
    [{"number": "1234"}, {"number": "4005 5192 0000 0004"}, {"expiry": "12/29"}, {"security_code": "12"}],
)
def test_sandbox_card_rejects_malformed_details(fields):
    with pytest.raises(SeedError):
        SandboxCard(**fields)


async def test_card_flow_needs_no_approval_and_no_capture_call(tmp_path, fake, state, db):
    report, prompts = await seed_once(seed_data(tmp_path), fake, state, db, card=SandboxCard())

    assert prompts == []  # nobody is asked to approve anything
    assert fake.capture_request_ids == []  # PayPal took the payment during create
    assert fake.card_numbers_sent == [DEFAULT_SANDBOX_CARD_NUMBER] * 2
    assert report.captured == report.rows_written == ["A-1001", "B-2003"]
    assert stored_orders(db)["A-1001"].captured_minor == 2648


async def test_card_needing_3d_secure_is_reported_then_retried_with_another_card(tmp_path, fake, state, db):
    data = seed_data(tmp_path, orders=[A_1001])
    fake.card_result = "PAYER_ACTION_REQUIRED"
    first, _ = await seed_once(data, fake, state, db, card=SandboxCard())
    assert "3D Secure" in first.problems[0]
    assert stored_orders(db) == {}

    fake.card_result = "COMPLETED"
    other_card = SandboxCard(number="4012000033330026")
    second, prompts = await seed_once(data, fake, state, db, card=other_card)

    assert second.created == second.captured == ["A-1001"]
    assert prompts == []
    assert fake.card_numbers_sent == [DEFAULT_SANDBOX_CARD_NUMBER, "4012000033330026"]
    assert fake.create_request_ids[1].endswith("-2")  # a fresh order, not the stuck one


async def test_card_is_never_sent_outside_the_sandbox(tmp_path, state, db):
    async with PayPalClient(LIVE_BASE_URL, "id", "secret", allow_live=True) as live:
        with respx.mock():  # no routes: any HTTP request would fail the test
            with pytest.raises(SeedError, match="only ever sent to the PayPal sandbox"):
                await run_seed(seed_data(tmp_path), live, state, db, lambda _p: None, SandboxCard())


def test_select_orders(tmp_path):
    data = seed_data(tmp_path)
    assert [o.order_ref for o in select_orders(data, ["B-2003"]).orders] == ["B-2003"]
    assert select_orders(data, None) is data
    with pytest.raises(SeedError, match="unknown order_ref"):
        select_orders(data, ["Z-0000"])


async def test_declined_card_frees_the_order_for_another_card(tmp_path, fake, state, db):
    data = seed_data(tmp_path, orders=[A_1001])
    fake.capture_override = {"status": "DECLINED"}
    first, _ = await seed_once(data, fake, state, db, card=SandboxCard())
    assert "--card-number" in first.problems[0]
    assert load_state(state).orders["A-1001"].paypal_order_id is None

    fake.capture_override = {}
    second, _ = await seed_once(data, fake, state, db, card=SandboxCard(number="4012000033330026"))

    assert second.captured == ["A-1001"]
    assert fake.create_request_ids[1].endswith("-2")
