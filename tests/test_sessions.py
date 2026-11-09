"""Sessions (SPEC §10): only the trusted orchestrator opens them, and only a token hash is stored."""

import hashlib
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from support import fresh_engine

from wardlock.config import Settings, get_settings
from wardlock.db import init_db, make_engine, make_session_factory
from wardlock.handles import mint_handles
from wardlock.main import create_app
from wardlock.models import AgentSession, Customer, Handle, HandleState, Order, OrderStatus, SessionStatus
from wardlock.sessions import MAX_TICKET_BODY_CHARS, open_session, resolve_session

SERVICE_KEY = "test-service-key"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
BODY = {"ticket_id": "T-0001", "customer_id": "cust_ana", "ticket_body": "My bike bell arrived scratched"}


@pytest.fixture
def engine():
    engine = fresh_engine()
    init_db(engine)
    with make_session_factory(engine)() as db, db.begin():
        db.add(Customer(id="cust_ana", first_name="Ana", email="ana@example.com"))
        db.add(Order(
            customer_id="cust_ana", order_ref="A-1001", status=OrderStatus.DELIVERED, placed_at=date(2026, 9, 20),
            paypal_order_id="PPORDER1", capture_id="CAPA1001", currency="USD", captured_minor=2648,
            items_json=[{"item_ref": "bell", "name": "Bike bell", "unit_minor": 400, "quantity": 1}],
            shipping_minor=650, captured_at=NOW - timedelta(days=5),
        ))
    yield engine
    engine.dispose()


def client_for(engine, key=SERVICE_KEY):
    # _env_file=None: tests never read the developer's real .env (no PayPal or LLM keys here).
    settings = Settings(_env_file=None, wardlock_service_key=key, session_ttl_minutes=30)
    app = create_app(engine=engine, settings=settings, sweep_interval=None)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def session_count(engine):
    with make_session_factory(engine)() as db:
        return db.scalar(select(func.count()).select_from(AgentSession))


# ---------------------------------------------------------------- POST /sessions


def test_opening_a_session_returns_a_token_and_stores_only_its_hash(engine):
    with client_for(engine) as client:
        response = client.post("/sessions", json=BODY, headers={"X-Service-Key": SERVICE_KEY})

    assert response.status_code == 201
    data = response.json()
    token = data["session_token"]
    assert len(token) >= 43  # 32 random bytes, base64url
    with make_session_factory(engine)() as db:
        stored = db.get(AgentSession, data["session_id"])
        assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in (stored.token_hash, stored.ticket_id, stored.ticket_body)
        assert (stored.customer_id, stored.status) == ("cust_ana", SessionStatus.ACTIVE)
        assert stored.ticket_body == BODY["ticket_body"]
    lifetime = datetime.fromisoformat(data["expires_at"]) - datetime.now(timezone.utc)
    assert timedelta(minutes=29) < lifetime <= timedelta(minutes=30)


@pytest.mark.parametrize("headers", [{}, {"X-Service-Key": "wrong"}, {"X-Service-Key": ""}])
def test_missing_or_wrong_service_key_is_rejected(engine, headers):
    with client_for(engine) as client:
        response = client.post("/sessions", json=BODY, headers=headers)
    assert response.status_code == 401
    assert session_count(engine) == 0


def test_without_a_key_the_body_is_not_even_validated(engine):
    # An outsider learns nothing about the request format.
    with client_for(engine) as client:
        response = client.post("/sessions", json={"nonsense": True})
    assert response.status_code == 401


def test_no_configured_service_key_means_no_sessions_at_all(engine):
    """Fail closed: a missing WARDLOCK_SERVICE_KEY must not mean 'no check'."""
    with client_for(engine, key="") as client:
        for headers in ({}, {"X-Service-Key": ""}, {"X-Service-Key": "anything"}):
            assert client.post("/sessions", json=BODY, headers=headers).status_code == 503
    assert session_count(engine) == 0


def test_unknown_customer_is_404(engine):
    with client_for(engine) as client:
        response = client.post("/sessions", json={**BODY, "customer_id": "cust_nobody"},
                               headers={"X-Service-Key": SERVICE_KEY})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {k: v for k, v in BODY.items() if k != "ticket_body"},
        {**BODY, "amount": "189.00"},  # unknown fields are refused
        {**BODY, "ticket_id": "T 0001; DROP"},
        {**BODY, "ticket_body": "x" * (MAX_TICKET_BODY_CHARS + 1)},
    ],
)
def test_malformed_bodies_are_rejected(engine, body):
    with client_for(engine) as client:
        response = client.post("/sessions", json=body, headers={"X-Service-Key": SERVICE_KEY})
    assert response.status_code == 422
    assert session_count(engine) == 0


# ---------------------------------------------------------------- open_session / resolve_session


@pytest.fixture
def db(engine):
    with make_session_factory(engine)() as session:
        yield session


def open_ana(db, now=NOW):
    return open_session(db, ticket_id="T-1", customer_id="cust_ana", ticket_body="hi", ttl_minutes=30, now=now)


def test_open_session_rejects_a_non_positive_ttl(db):
    with pytest.raises(ValueError):
        open_session(db, ticket_id="T-1", customer_id="cust_ana", ticket_body="hi", ttl_minutes=0)


def test_resolve_session_finds_the_live_session_for_its_token(db):
    opened = open_ana(db)
    assert opened.expires_at == NOW + timedelta(minutes=30)
    assert resolve_session(db, opened.token, now=NOW + timedelta(minutes=29)).id == opened.session_id


@pytest.mark.parametrize("token", ["not-the-token", "", None, 123])
def test_resolve_session_rejects_other_tokens(db, token):
    open_ana(db)
    assert resolve_session(db, token, now=NOW) is None


def test_an_expired_session_is_marked_and_its_handles_expire(db):
    opened = open_ana(db)
    session = db.get(AgentSession, opened.session_id)
    mint_handles(db, session, goodwill_percentages=[10], now=NOW)

    assert resolve_session(db, opened.token, now=NOW + timedelta(minutes=30)) is None
    assert session.status == SessionStatus.EXPIRED
    states = set(db.scalars(select(Handle.state).where(Handle.session_id == session.id)))
    assert states == {HandleState.EXPIRED}


def test_a_closed_session_no_longer_resolves(db):
    opened = open_ana(db)
    db.get(AgentSession, opened.session_id).status = SessionStatus.CLOSED
    assert resolve_session(db, opened.token, now=NOW) is None
