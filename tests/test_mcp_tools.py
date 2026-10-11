"""The agent-facing MCP tools (SPEC §3), driven through the real MCP protocol, in-process.

The SDK's own client talks to our FastAPI app through httpx2.ASGITransport: initialize,
list the tools, call them, all with the agent's Authorization header. PayPal is faked with respx.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import httpx2
import pytest
import respx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from wardlock.config import Settings, get_settings
from wardlock.db import init_db, make_engine, make_session_factory
from wardlock.engine.policy import load_policy
from wardlock.enums import OrderStatus
from wardlock.executor import Executor
from wardlock.main import create_app
from wardlock.mcp_tools import AUTH_ERROR, NOT_CONFIGURED, UNCONFIRMED, AgentServices
from wardlock.models import Base, Customer, Order
from wardlock.paypal.client import PayPalClient
from wardlock.reason_check import ReasonCheckResult
from wardlock.sessions import open_session

SANDBOX = "https://api-m.sandbox.paypal.com"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
CAPTURES = ("CAPA1001", "CAPA1002", "CAPB2001")


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class FakePayPal:
    def __init__(self):
        self.calls = []

    def refund(self, request, capture):
        body = json.loads(request.content)
        self.calls.append((capture, body))
        return httpx.Response(201, json={"id": f"RF{len(self.calls)}", "status": "COMPLETED",
                                         "amount": body["amount"], "custom_id": body.get("custom_id")})


async def consistent(_reason, _labels, _body):
    return ReasonCheckResult(True, "matches the ticket")


def make_order(ref, customer, items, shipping, capture):
    return Order(
        customer_id=customer, order_ref=ref, status=OrderStatus.DELIVERED, placed_at=date(2026, 9, 20),
        paypal_order_id=f"PO{capture}", capture_id=capture, currency="USD",
        captured_minor=sum(u * q for _r, _n, u, q in items) + shipping,
        items_json=[{"item_ref": r, "name": n, "unit_minor": u, "quantity": q} for r, n, u, q in items],
        shipping_minor=shipping, captured_at=NOW - timedelta(days=5),
    )


@asynccontextmanager
async def running(app):
    """Run the app's lifespan in ONE background task. The MCP session manager uses an anyio task
    group, which must be entered and exited by the same task, and pytest-asyncio runs fixture
    setup and teardown in different tasks."""
    started, stop = asyncio.Event(), asyncio.Event()

    async def lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(lifespan())
    await started.wait()
    try:
        yield app
    finally:
        stop.set()
        await task


async def start(engine, factory, executor, clock):
    services = AgentServices(factory, load_policy(), executor, clock=clock)
    settings = Settings(_env_file=None, wardlock_service_key="svc")
    app = create_app(engine=engine, services=services, settings=settings, sweep_interval=None)
    app.dependency_overrides[get_settings] = lambda: settings
    return app


@pytest.fixture
async def wl():
    engine = make_engine("sqlite://")
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
    fake, clock = FakePayPal(), Clock()

    async def no_sleep(_seconds):
        pass

    with respx.mock(base_url=SANDBOX, assert_all_called=False) as router:
        router.post("/v1/oauth2/token").respond(200, json={"access_token": "tok", "expires_in": 3600})
        router.post(path__regex=r"^/v2/payments/captures/(?P<capture>[A-Z0-9]+)/refund$").mock(side_effect=fake.refund)
        async with PayPalClient(SANDBOX, "id", "secret", sleep=no_sleep) as paypal:
            executor = Executor(factory, paypal, load_policy(), consistent, clock=clock)
            app = await start(engine, factory, executor, clock)
            async with running(app):
                yield SimpleNamespace(app=app, factory=factory, fake=fake, clock=clock, engine=engine)
    engine.dispose()


def token_for(wl, customer="cust_ana"):
    with wl.factory() as db, db.begin():
        return open_session(db, ticket_id="T-1", customer_id=customer, ticket_body="My bike bell arrived scratched.",
                            ttl_minutes=30, now=NOW).token


@asynccontextmanager
async def agent(app, token=None, *, authorization=None):
    """An MCP client session, exactly like a real agent's, but in-process."""
    headers = {"Authorization": authorization or f"Bearer {token}"} if (token or authorization) else {}
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://localhost:8000", headers=headers) as http:
        async with streamable_http_client("http://localhost:8000/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


def data(result):
    assert not result.is_error, result.content[0].text
    return result.structured_content or json.loads(result.content[0].text)


def error_text(result):
    assert result.is_error
    return result.content[0].text


async def options(session):
    context = data(await session.call_tool("get_session_context", {}))
    return {o["label"]: o["handle"] for order in context["orders"] for o in order["refund_options"]
            if order["order_ref"] == "A-1001"}, context


# ---------------------------------------------------------------- the tool list


async def test_four_tools_with_the_right_hints_and_no_amount_parameter(wl):
    async with agent(wl.app, token_for(wl)) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}

    assert set(tools) == {"get_session_context", "request_refund", "get_refund_status", "request_refund_raw"}
    assert tools["get_session_context"].annotations.read_only_hint is True
    assert tools["get_refund_status"].annotations.read_only_hint is True
    assert tools["request_refund"].annotations.destructive_hint is True
    assert tools["request_refund_raw"].annotations.destructive_hint is True
    refund = tools["request_refund"]
    assert set(refund.input_schema["properties"]) == {"refund_handles", "reason_code", "note_to_customer"}
    assert "cannot type or change an amount" in refund.description  # SPEC §3.2, invariant 2
    assert "DAMAGED" in json.dumps(refund.input_schema)  # the agent sees the allowed reasons


# ---------------------------------------------------------------- authentication


@pytest.mark.parametrize("token", [None, "not-a-real-token", "expired"])
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_session_context", {}),
        ("request_refund", {"refund_handles": ["h_x"], "reason_code": "DAMAGED"}),
        ("get_refund_status", {"ref": "rf_x"}),
        ("request_refund_raw", {"capture_id": "CAPA1001", "amount": "4.00", "currency": "USD", "reason_code": "DAMAGED"}),
    ],
)
async def test_every_tool_needs_a_live_session_token(wl, token, tool, args):
    if token == "expired":
        token = token_for(wl)
        wl.clock.now = NOW + timedelta(minutes=31)
    async with agent(wl.app, token) as session:
        assert AUTH_ERROR in error_text(await session.call_tool(tool, args))
    assert wl.fake.calls == []


# ---------------------------------------------------------------- the tools


async def test_get_session_context_shows_only_this_customer_and_no_payment_ids(wl):
    async with agent(wl.app, token_for(wl)) as session:
        result = await session.call_tool("get_session_context", {})
    context = data(result)
    raw = result.content[0].text + json.dumps(result.structured_content or {})

    assert context["customer_first_name"] == "Ana"
    assert [o["order_ref"] for o in context["orders"]] == ["A-1001", "A-1002"]  # never Ben's B-2001
    a1001 = context["orders"][0]["refund_options"]
    assert [(o["label"], o["amount"]) for o in a1001] == [
        ("Bike bell x1", "4.00"), ("Bottle cage x2", "15.98"), ("Shipping", "6.50"),
        ("10% goodwill", "2.00"), ("25% goodwill", "5.00"), ("Full remaining refund", "26.48"),
    ]
    for paypal_id in (*CAPTURES, "POCAPA1001", "POCAPA1002"):  # invariant 7
        assert paypal_id not in raw


async def test_a_bell_refund_and_its_status(wl):
    async with agent(wl.app, token_for(wl)) as session:
        bell = (await options(session))[0]["Bike bell x1"]
        refund = data(await session.call_tool("request_refund", {"refund_handles": [bell], "reason_code": "DAMAGED",
                                                                 "note_to_customer": "Sorry!"}))
        status = data(await session.call_tool("get_refund_status", {"ref": refund["refund_ref"]}))

    assert (refund["status"], refund["amount"], refund["currency"]) == ("ALLOWED", "4.00", "USD")
    assert refund["message_for_agent"] == "Refund of 4.00 USD completed."
    assert (status["kind"], status["status"], status["amount"]) == ("refund", "COMPLETED", "4.00")
    assert wl.fake.calls[0][1]["amount"] == {"value": "4.00", "currency_code": "USD"}

    async with agent(wl.app, token_for(wl, "cust_ben")) as ben:  # another conversation can't see it
        assert "No refund or hold" in error_text(await ben.call_tool("get_refund_status", {"ref": refund["refund_ref"]}))


async def test_a_held_refund_and_its_status(wl):
    async with agent(wl.app, token_for(wl)) as session:
        context = data(await session.call_tool("get_session_context", {}))
        helmet = next(o["handle"] for order in context["orders"] if order["order_ref"] == "A-1002"
                      for o in order["refund_options"] if o["kind"] == "FULL_REMAINING")
        held = data(await session.call_tool("request_refund", {"refund_handles": [helmet], "reason_code": "NOT_RECEIVED"}))
        status = data(await session.call_tool("get_refund_status", {"ref": held["hold_id"]}))
    assert (held["status"], held["reason_codes"]) == ("HELD", ["OVER_AUTO_LIMIT"])
    assert (status["kind"], status["status"], status["amount"]) == ("hold", "PENDING", "189.00")
    assert wl.fake.calls == []


async def test_an_amount_sent_to_request_refund_is_never_used(wl):
    """Invariant 2: even if an agent smuggles an "amount" argument in, it changes nothing."""
    async with agent(wl.app, token_for(wl)) as session:
        bell = (await options(session))[0]["Bike bell x1"]
        result = await session.call_tool("request_refund", {"refund_handles": [bell], "reason_code": "DAMAGED",
                                                            "amount": "189.00"})
    if not result.is_error:
        assert data(result)["amount"] == "4.00"
    assert all(body["amount"]["value"] == "4.00" for _capture, body in wl.fake.calls)


@pytest.mark.parametrize(
    ("args", "status", "codes"),
    [
        ({"capture_id": "CAPB2001", "amount": "189.00", "currency": "USD", "reason_code": "NOT_RECEIVED"},
         "BLOCKED", ["NOT_OWNER"]),
        ({"capture_id": "CAPA1001", "currency": "USD", "reason_code": "DAMAGED"}, "BLOCKED", ["MISSING_AMOUNT"]),
        ({"capture_id": "CAPA1001", "amount": "abc", "currency": "USD", "reason_code": "DAMAGED"},
         "BLOCKED", ["INVALID_AMOUNT"]),
        ({"capture_id": "CAPA1001", "amount": "4.00", "currency": "USD", "reason_code": "DAMAGED"}, "ALLOWED", []),
    ],
)
async def test_the_compat_tool(wl, args, status, codes):
    async with agent(wl.app, token_for(wl)) as session:
        result = data(await session.call_tool("request_refund_raw", args))
    assert (result["status"], result["reason_codes"]) == (status, codes)


async def test_a_compromised_agent_moves_no_money(wl):
    """A quick T19 through the real protocol (the full scripted attacker is in P7)."""
    with wl.factory() as db, db.begin():
        bens_token = open_session(db, ticket_id="T-B", customer_id="cust_ben", ticket_body="hi", ttl_minutes=30,
                                  now=NOW).token
    async with agent(wl.app, bens_token) as ben:
        bens_handles = [o["handle"] for order in data(await ben.call_tool("get_session_context", {}))["orders"]
                        for o in order["refund_options"]]
    async with agent(wl.app, token_for(wl)) as session:
        attempts = [
            ("request_refund", {"refund_handles": ["h_forged0000000000000000"], "reason_code": "DAMAGED"}),
            ("request_refund", {"refund_handles": bens_handles[:1], "reason_code": "DAMAGED"}),
            ("request_refund_raw", {"capture_id": "CAPB2001", "amount": "189.00", "currency": "USD",
                                    "reason_code": "NOT_RECEIVED"}),
            ("request_refund_raw", {"capture_id": "CAPA1001", "currency": "USD", "reason_code": "NOT_RECEIVED"}),
        ]
        statuses = [data(await session.call_tool(tool, args))["status"] for tool, args in attempts]
    assert statuses == ["BLOCKED"] * 4
    assert wl.fake.calls == []  # zero money moved


async def test_a_pasted_session_token_is_never_stored_or_sent_to_paypal(wl):
    """Wardlock keeps only the token's hash. An agent pasting the token into a field must not undo that."""
    token = token_for(wl)
    async with agent(wl.app, token) as session:
        bell = (await options(session))[0]["Bike bell x1"]
        raw = data(await session.call_tool("request_refund_raw", {"capture_id": token, "amount": "4.00",
                                                                  "currency": "USD", "reason_code": "DAMAGED"}))
        forged = data(await session.call_tool("request_refund", {"refund_handles": [token], "reason_code": "DAMAGED"}))
        refund = data(await session.call_tool("request_refund", {"refund_handles": [bell], "reason_code": "DAMAGED",
                                                                 "note_to_customer": f"Your code is {token}"}))

    assert (raw["status"], raw["reason_codes"]) == ("BLOCKED", ["NOT_OWNER"])
    assert forged["status"] == "BLOCKED"
    assert refund["status"] == "ALLOWED"
    assert [body["note_to_payer"] for _capture, body in wl.fake.calls] == ["Your code is [redacted]"]
    with wl.factory() as db:
        stored = [row for table in Base.metadata.sorted_tables for row in db.execute(table.select())]
    assert stored and token not in repr(stored)


# ---------------------------------------------------------------- failure modes


async def test_without_paypal_configured_refund_tools_refuse_but_reads_work(wl):
    app = await start(wl.engine, wl.factory, None, wl.clock)
    async with running(app):
        async with agent(app, token_for(wl)) as session:
            assert data(await session.call_tool("get_session_context", {}))["customer_first_name"] == "Ana"
            result = await session.call_tool("request_refund", {"refund_handles": ["h_x"], "reason_code": "DAMAGED"})
            assert NOT_CONFIGURED in error_text(result)


async def test_an_unexpected_error_is_reported_without_internals(wl, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise RuntimeError("database exploded at line 42")

    monkeypatch.setattr(wl.app.state.services.executor, "request_refund", boom)
    async with agent(wl.app, token_for(wl)) as session:
        text = error_text(await session.call_tool("request_refund", {"refund_handles": ["h_x"], "reason_code": "DAMAGED"}))
    assert UNCONFIRMED in text and "line 42" not in text


async def test_a_foreign_host_is_refused_and_the_other_routes_still_work(wl):
    transport = httpx2.ASGITransport(app=wl.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://evil.example.com") as http:
        response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                   headers={"Accept": "application/json, text/event-stream"})
    assert response.status_code == 421  # DNS-rebinding protection

    async with httpx2.AsyncClient(transport=transport, base_url="http://localhost:8000") as http:
        assert (await http.get("/health")).json() == {"status": "ok"}
        opened = await http.post("/sessions", headers={"X-Service-Key": "svc"},
                                 json={"ticket_id": "T-9", "customer_id": "cust_ana", "ticket_body": "hi"})
        assert opened.status_code == 201


@pytest.mark.parametrize("scheme", ["Basic {}", "Token {}", "{}", "Bearer"])
async def test_the_token_only_counts_as_a_bearer_token(wl, scheme):
    """A valid token sent any other way is refused: no guessing at what the client meant."""
    async with agent(wl.app, authorization=scheme.format(token_for(wl))) as session:
        assert AUTH_ERROR in error_text(await session.call_tool("get_session_context", {}))


async def test_each_conversation_sees_only_its_own_customer(wl):
    """G1 at the tool layer: with two live sessions, each token gets only its own customer's orders."""
    ben_token = token_for(wl, "cust_ben")  # opened first on purpose
    ana_token = token_for(wl, "cust_ana")
    async with agent(wl.app, ana_token) as ana:
        ana_context = data(await ana.call_tool("get_session_context", {}))
    async with agent(wl.app, ben_token) as ben:
        ben_context = data(await ben.call_tool("get_session_context", {}))
    assert (ana_context["customer_first_name"], [o["order_ref"] for o in ana_context["orders"]]) == (
        "Ana", ["A-1001", "A-1002"])
    assert (ben_context["customer_first_name"], [o["order_ref"] for o in ben_context["orders"]]) == (
        "Ben", ["B-2001"])
