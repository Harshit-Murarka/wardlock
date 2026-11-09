"""Shared in-process Wardlock for the tool, demo-agent and attacker tests.

The real FastAPI app with its MCP endpoint, an in-memory database with Ana's and Ben's
orders, and PayPal faked with respx. Imported by conftest.py and the tests.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import respx

from wardlock.config import Settings, get_settings
from wardlock.db import init_db, make_engine, make_session_factory
from wardlock.engine.policy import load_policy
from wardlock.enums import OrderStatus
from wardlock.executor import Executor
from wardlock.main import create_app
from wardlock.mcp_tools import AgentServices
from wardlock.models import Customer, Order
from wardlock.paypal.client import PayPalClient
from wardlock.reason_check import ReasonCheckResult
from wardlock.sessions import open_session

SERVICE_KEY = "svc"
WEBHOOK_ID = "WH-TEST-1"
ADMIN_PASSWORD = "admin-pass"

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
        self.refund_error = None  # set to an HTTP status to make every refund call fail with it
        self.verify_status = "SUCCESS"  # what verify-webhook-signature answers; an int means that HTTP status
        self.verify_requests = []  # the raw bodies Wardlock sent for verification

    def refund(self, request, capture):
        body = json.loads(request.content)
        self.calls.append((capture, body))
        if self.refund_error is not None:
            return httpx.Response(self.refund_error, json={"name": "INTERNAL_SERVER_ERROR"})
        return httpx.Response(201, json={"id": f"RF{len(self.calls)}", "status": "COMPLETED",
                                         "amount": body["amount"], "custom_id": body.get("custom_id")})

    def verify(self, request):
        self.verify_requests.append(request.content)
        if isinstance(self.verify_status, int):
            return httpx.Response(self.verify_status, json={"name": "INTERNAL_SERVER_ERROR"})
        return httpx.Response(200, json={"verification_status": self.verify_status})


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


async def start(engine, factory, executor, clock, *, paypal=None):
    services = AgentServices(factory, load_policy(), executor, clock=clock, paypal=paypal)
    settings = Settings(_env_file=None, wardlock_service_key=SERVICE_KEY, paypal_webhook_id=WEBHOOK_ID,
                        wardlock_admin_password=ADMIN_PASSWORD)
    app = create_app(engine=engine, services=services, settings=settings, sweep_interval=None)
    app.dependency_overrides[get_settings] = lambda: settings
    return app


@asynccontextmanager
async def in_process_wardlock():
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
        router.post("/v1/notifications/verify-webhook-signature").mock(side_effect=fake.verify)
        async with PayPalClient(SANDBOX, "id", "secret", sleep=no_sleep) as paypal:
            executor = Executor(factory, paypal, load_policy(), consistent, clock=clock)
            app = await start(engine, factory, executor, clock, paypal=paypal)
            async with running(app):
                yield SimpleNamespace(app=app, factory=factory, fake=fake, clock=clock, engine=engine,
                                      executor=executor, paypal=paypal)
    engine.dispose()


def token_for(wl, customer="cust_ana"):
    with wl.factory() as db, db.begin():
        return open_session(db, ticket_id="T-1", customer_id=customer, ticket_body="My bike bell arrived scratched.",
                            ttl_minutes=30, now=NOW).token
