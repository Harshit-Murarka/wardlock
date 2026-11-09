"""Hostile agent inputs must give a clean answer, never a crash (run this on Postgres too).

Postgres rejects things SQLite accepts: NUL bytes in text, integers above 2**31-1, strings
longer than a column. An agent controls every argument below, so each one must end as a
normal BLOCKED (on record) or a plain validation error, never "could not be confirmed".
"""

import json

import pytest
from sqlalchemy import func, select
from support import token_for

from wardlock.mcp_tools import UNCONFIRMED
from wardlock.models import Decision

from test_mcp_tools import agent

NASTY_STRINGS = [
    "h_\x00null",  # NUL byte: Postgres text can't hold it
    "\x00",
    "h_" + "‮" * 10,  # right-to-left override
    "h_💸💸💸",
    "'; DROP TABLE handles; --",
    "h_" + "x" * 62,  # exactly the 64-character limit
    " ",
    "\\",
    "%",
    "_",
]


def outcome(result):
    """('error', text) or ('ok', data) for one tool result."""
    text = result.content[0].text if result.content else ""
    if result.is_error:
        return "error", text
    return "ok", result.structured_content or json.loads(text)


def decisions(wl):
    with wl.factory() as db:
        return db.scalar(select(func.count()).select_from(Decision))


@pytest.mark.parametrize("handle", NASTY_STRINGS)
async def test_nasty_handles_are_blocked_and_recorded(wl, handle):
    async with agent(wl.app, token_for(wl)) as session:
        kind, data = outcome(await session.call_tool("request_refund", {"refund_handles": [handle],
                                                                        "reason_code": "DAMAGED"}))
    assert kind == "ok", data
    assert (data["status"], data["reason_codes"]) == ("BLOCKED", ["UNKNOWN_HANDLE"])
    assert decisions(wl) == 1
    assert wl.fake.calls == []


@pytest.mark.parametrize("capture", NASTY_STRINGS)
async def test_nasty_capture_ids_are_blocked_and_recorded(wl, capture):
    async with agent(wl.app, token_for(wl)) as session:
        kind, data = outcome(await session.call_tool("request_refund_raw", {
            "capture_id": capture, "amount": "4.00", "currency": "USD", "reason_code": "DAMAGED"}))
    if kind == "error":  # the tool's own validation (e.g. a blank id) is fine; a crash is not
        assert UNCONFIRMED not in data
        return
    assert (data["status"], data["reason_codes"]) == ("BLOCKED", ["NOT_OWNER"])
    assert decisions(wl) == 1
    assert wl.fake.calls == []


@pytest.mark.parametrize("amount", ["1e9", "1e400", "-0", "0.000", "NaN", "Infinity", "4.00\x00",
                                    "99999999999999999999999999999", "4,00", " 4.00 ", "4.00 USD"])
async def test_nasty_amounts_are_blocked_and_recorded(wl, amount):
    async with agent(wl.app, token_for(wl)) as session:
        kind, data = outcome(await session.call_tool("request_refund_raw", {
            "capture_id": "CAPA1001", "amount": amount, "currency": "USD", "reason_code": "DAMAGED"}))
    assert kind == "ok", data
    if amount == " 4.00 ":  # surrounding spaces are trimmed: a real, valid 4.00
        assert data["status"] == "ALLOWED"
        return
    assert data["status"] == "BLOCKED", data
    assert decisions(wl) == 1


@pytest.mark.parametrize("currency", ["\x00", "usd", "US", "💵", "XXX"])
async def test_nasty_currencies_are_blocked(wl, currency):
    async with agent(wl.app, token_for(wl)) as session:
        kind, data = outcome(await session.call_tool("request_refund_raw", {
            "capture_id": "CAPA1001", "amount": "4.00", "currency": currency, "reason_code": "DAMAGED"}))
    if kind == "error":
        assert UNCONFIRMED not in data
        return
    assert data["status"] == "BLOCKED" and wl.fake.calls == []


@pytest.mark.parametrize("note", ["\x00", "a\x00b", "🙂" * 500, "‮ evil", "x" * 2000])
async def test_nasty_notes_never_break_a_refund(wl, note):
    async with agent(wl.app, token_for(wl)) as session:
        context = outcome(await session.call_tool("get_session_context", {}))[1]
        bell = next(o["handle"] for order in context["orders"] for o in order["refund_options"]
                    if o["label"] == "Bike bell x1")
        kind, data = outcome(await session.call_tool("request_refund", {
            "refund_handles": [bell], "reason_code": "DAMAGED", "note_to_customer": note}))
    assert kind == "ok", data
    assert data["status"] == "ALLOWED"
    sent = wl.fake.calls[0][1].get("note_to_payer", "")
    assert "\x00" not in sent and len(sent) <= 200


@pytest.mark.parametrize("ref", ["\x00", "rf_\x00", "hold_" + "\x00" * 3, "rf_" + "x" * 61, "'; --"])
async def test_nasty_refund_status_refs_are_just_not_found(wl, ref):
    async with agent(wl.app, token_for(wl)) as session:
        kind, data = outcome(await session.call_tool("get_refund_status", {"ref": ref}))
    assert kind == "error"
    assert "No refund or hold" in data or "validation" in data.lower()


@pytest.mark.parametrize("args", [
    {"refund_handles": "h_x", "reason_code": "DAMAGED"},  # a string, not a list
    {"refund_handles": [1, 2], "reason_code": "DAMAGED"},
    {"refund_handles": [None], "reason_code": "DAMAGED"},
    {"refund_handles": [], "reason_code": "DAMAGED"},
    {"refund_handles": ["h_x"], "reason_code": "BECAUSE"},
    {"refund_handles": ["h_x"]},
])
async def test_malformed_arguments_are_validation_errors_not_crashes(wl, args):
    async with agent(wl.app, token_for(wl)) as session:
        kind, data = outcome(await session.call_tool("request_refund", args))
    if kind == "ok":  # e.g. an empty list reaches the engine and is blocked
        assert data["status"] == "BLOCKED"
    else:
        assert UNCONFIRMED not in data
    assert wl.fake.calls == []


# ---------------------------------------------------------------- the other entry points


async def http(wl, method, path, **kwargs):
    import httpx2

    transport = httpx2.ASGITransport(app=wl.app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        return await client.request(method, path, **kwargs)


async def test_a_nul_in_the_ticket_body_is_stored_visibly_not_a_crash(wl):
    from support import SERVICE_KEY

    from wardlock.models import AgentSession

    response = await http(wl, "POST", "/sessions", headers={"X-Service-Key": SERVICE_KEY},
                          json={"ticket_id": "T-1", "customer_id": "cust_ana", "ticket_body": "bell\x00 broke"})
    assert response.status_code == 201
    with wl.factory() as db:
        assert db.get(AgentSession, response.json()["session_id"]).ticket_body == "bell� broke"


async def test_a_nul_in_the_customer_id_is_just_an_unknown_customer(wl):
    from support import SERVICE_KEY

    response = await http(wl, "POST", "/sessions", headers={"X-Service-Key": SERVICE_KEY},
                          json={"ticket_id": "T-1", "customer_id": "cust_ana\x00", "ticket_body": "hi"})
    assert response.status_code == 404


@pytest.mark.parametrize("event", [
    {"id": "WH-\x00", "event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": {}},
    {"id": "WH-OK1", "event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": {"id": "RF\x00"}},
])
async def test_a_webhook_with_a_nul_is_not_a_paypal_event(wl, event):
    from test_webhooks import SIGNED

    response = await http(wl, "POST", "/webhooks/paypal", content=json.dumps(event),
                          headers={**SIGNED, "Content-Type": "application/json"})
    assert response.status_code == 400
    assert wl.fake.verify_requests == []  # rejected before asking PayPal


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/admin/api/sessions/ses_%00/replay"),
    ("POST", "/admin/api/holds/hold_%00/approve"),
    ("POST", "/admin/api/holds/hold_%00/deny"),
    ("POST", "/admin/api/alerts/alr_%00/ack"),
    ("GET", "/admin/api/decisions?reason_code=%00"),
    ("GET", "/health?x=%00"),
])
async def test_a_nul_in_any_url_is_a_plain_400(wl, method, path):
    from test_admin import basic

    response = await http(wl, method, path, headers=basic())
    assert response.status_code == 400
