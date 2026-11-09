"""PayPal webhooks and reconciliation (SPEC §8): T14, T15, T20, T21 and the edge cases.

The app runs in-process (support.py). PayPal's verify-webhook-signature is faked: its answer
is whatever `wl.fake.verify_status` says.
"""

import json

import httpx2
from sqlalchemy import func, select
from support import NOW, SANDBOX, WEBHOOK_ID, token_for

from wardlock.config import Settings, get_settings
from wardlock.enums import AlertKind, HandleState, HoldStatus, ReasonCode
from wardlock.handles import mint_handles
from wardlock.models import AgentSession, Alert, Decision, Handle, Hold, LedgerEntry, WebhookEvent
from wardlock.sessions import resolve_session

SIGNED = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
    "PAYPAL-TRANSMISSION-ID": "tx-1",
    "PAYPAL-TRANSMISSION-SIG": "c2lnbmF0dXJl",
    "PAYPAL-TRANSMISSION-TIME": "2026-10-09T12:00:00Z",
}


def refund_event(refund_id, capture, value, *, custom_id=None, event_id=None, status="COMPLETED"):
    resource = {
        "id": refund_id, "status": status, "amount": {"value": value, "currency_code": "USD"},
        "links": [{"rel": "self", "href": f"{SANDBOX}/v2/payments/refunds/{refund_id}"},
                  {"rel": "up", "href": f"{SANDBOX}/v2/payments/captures/{capture}"}],
    }
    if custom_id is not None:
        resource["custom_id"] = custom_id
    return {"id": event_id or f"WH-{refund_id}", "event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": resource}


async def post(wl, event, headers=SIGNED, *, raw=None):
    body = raw if raw is not None else json.dumps(event).encode()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=wl.app), base_url="http://localhost:8000") as http:
        return await http.post("/webhooks/paypal", content=body, headers={**headers, "Content-Type": "application/json"})


def count(wl, model, *where):
    with wl.factory() as db:
        return db.scalar(select(func.count()).select_from(model).where(*where))


def alerts(wl):
    with wl.factory() as db:
        return [(a.kind, a.details_json) for a in db.scalars(select(Alert))]


async def refund_bell(wl):
    """Ana's agent asks for the bell (4.00 on CAPA1001) through the executor. Returns the outcome."""
    token = token_for(wl)
    with wl.factory() as db, db.begin():
        session = resolve_session(db, token, NOW)
        orders = mint_handles(db, session, goodwill_percentages=[10, 25], now=NOW)
        bell = next(o.handle for order in orders for o in order.refund_options if o.label == "Bike bell x1")
        session_id = session.id
    return await wl.executor.request_refund(session_id, [bell], ReasonCode.DAMAGED), bell


# ---------------------------------------------------------------- verification (T15)


async def test_t15_a_bad_signature_is_400_and_nothing_is_processed(wl):
    wl.fake.verify_status = "FAILURE"
    response = await post(wl, refund_event("RFX1", "CAPB2001", "189.00"))

    assert response.status_code == 400
    assert alerts(wl) == []  # not processed
    with wl.factory() as db:
        assert db.get(WebhookEvent, "WH-RFX1").verified is False  # kept for investigation


async def test_missing_signature_headers_are_400_without_asking_paypal(wl):
    response = await post(wl, refund_event("RFX1", "CAPB2001", "189.00"), headers={})
    assert response.status_code == 400
    assert wl.fake.verify_requests == [] and alerts(wl) == []


async def test_the_event_is_verified_byte_for_byte(wl):
    raw = b'{ "id":"WH-RAW", "event_type":"PAYMENT.CAPTURE.COMPLETED",  "resource":{"id":"CAP9", "amount":1.10} }'
    assert (await post(wl, None, raw=raw)).status_code == 200

    sent = wl.fake.verify_requests[0]
    assert raw in sent  # exactly as received, not re-serialised
    request = json.loads(sent)
    assert request["webhook_id"] == WEBHOOK_ID
    assert (request["transmission_id"], request["auth_algo"]) == ("tx-1", "SHA256withRSA")
    assert request["webhook_event"]["id"] == "WH-RAW"


async def test_paypal_unreachable_during_verification_is_503_so_paypal_retries(wl):
    wl.fake.verify_status = 503
    response = await post(wl, refund_event("RFX1", "CAPB2001", "189.00"))
    assert response.status_code == 503
    assert alerts(wl) == [] and count(wl, WebhookEvent) == 0


async def test_without_a_webhook_id_nothing_is_accepted(wl):
    settings = Settings(_env_file=None, paypal_webhook_id="")
    wl.app.dependency_overrides[get_settings] = lambda: settings
    assert (await post(wl, refund_event("RFX1", "CAPB2001", "189.00"))).status_code == 503
    assert wl.fake.verify_requests == []


async def test_oversized_and_malformed_bodies_are_refused(wl):
    assert (await post(wl, None, raw=b"{" + b" " * 300_000 + b"}")).status_code == 413
    assert (await post(wl, None, raw=b"not json")).status_code == 400
    assert (await post(wl, None, raw=b'{"id": "", "event_type": "X"}')).status_code == 400
    assert wl.fake.verify_requests == []


# ---------------------------------------------------------------- reconciliation (T14, T20, T21)


async def test_t14_a_refund_wardlock_did_not_make_raises_an_alert(wl):
    response = await post(wl, refund_event("RFOUT1", "CAPB2001", "189.00"))

    assert (response.status_code, response.json()["status"]) == (200, "alerted")
    [(kind, details)] = alerts(wl)
    assert kind == AlertKind.OUT_OF_BAND_REFUND
    assert (details["refund_id"], details["capture_id"], details["amount"]) == ("RFOUT1", "CAPB2001", "189.00")


async def test_wardlocks_own_recorded_refund_raises_no_alert(wl):
    outcome, _bell = await refund_bell(wl)
    assert outcome.status == "ALLOWED"
    response = await post(wl, refund_event("RF1", "CAPA1001", "4.00", custom_id=outcome.decision_id))

    assert response.json()["status"] == "already recorded"
    assert alerts(wl) == [] and count(wl, LedgerEntry) == 1


async def test_t20_a_lost_response_is_backfilled_without_an_alert(wl):
    """PayPal refunded, but Wardlock only saw errors: an unknown-outcome hold. The webhook settles it."""
    wl.fake.refund_error = 503
    outcome, bell = await refund_bell(wl)
    assert (outcome.status, outcome.reason_codes) == ("HELD", ("DEPENDENCY_UNAVAILABLE",))

    response = await post(wl, refund_event("RFLOST1", "CAPA1001", "4.00", custom_id=outcome.decision_id))

    assert response.json()["status"] == "backfilled"
    assert alerts(wl) == []
    with wl.factory() as db:
        ledger = db.scalar(select(LedgerEntry))
        assert (ledger.decision_id, ledger.paypal_refund_id, ledger.amount_minor) == (outcome.decision_id, "RFLOST1", 400)
        assert db.get(Handle, bell).state == HandleState.CONSUMED
        assert db.get(Hold, outcome.hold_id).status == HoldStatus.EXECUTED

    # Approving the settled hold now does nothing: no second refund is possible.
    sends_before = len(wl.fake.calls)
    approval = await wl.executor.approve_hold(outcome.hold_id, "merchant")
    assert not approval.executed and len(wl.fake.calls) == sends_before


async def test_t21_a_copied_decision_id_with_another_amount_or_capture_is_alerted(wl):
    wl.fake.refund_error = 503
    outcome, bell = await refund_bell(wl)

    await post(wl, refund_event("RFFAKE1", "CAPA1001", "26.48", custom_id=outcome.decision_id))  # wrong amount
    await post(wl, refund_event("RFFAKE2", "CAPA1002", "4.00", custom_id=outcome.decision_id))  # wrong capture

    reasons = [details["reason"] for _kind, details in alerts(wl)]
    assert reasons == ["the amount does not match the decision", "the capture does not match the decision"]
    assert count(wl, LedgerEntry) == 0
    with wl.factory() as db:
        assert db.get(Handle, bell).state == HandleState.RESERVED  # still waiting on the real outcome


async def test_a_blocked_or_unknown_decision_id_cannot_explain_a_refund(wl):
    wl.fake.refund_error = 422  # PayPal refuses: the decision ends BLOCKED and its handles are freed
    rejected, _bell = await refund_bell(wl)
    assert (rejected.status, rejected.reason_codes) == ("BLOCKED", ("PAYPAL_REJECTED",))
    never_sent = await wl.executor.request_refund_raw(_session_id(wl), "CAPA1001", None, "USD", ReasonCode.DAMAGED)

    await post(wl, refund_event("RFB1", "CAPA1001", "4.00", custom_id=rejected.decision_id))
    await post(wl, refund_event("RFB2", "CAPA1001", "4.00", custom_id=never_sent.decision_id))
    await post(wl, refund_event("RFB3", "CAPA1001", "4.00", custom_id="dec_does_not_exist"))

    assert [d["reason"] for _k, d in alerts(wl)] == [
        "the decision was blocked, so Wardlock never sent it",
        "custom_id does not name a Wardlock refund",
        "custom_id does not name a Wardlock refund",
    ]
    assert count(wl, LedgerEntry) == 0


def _session_id(wl):
    with wl.factory() as db:
        return db.scalar(select(AgentSession.id))


async def test_failed_refund_events_move_no_money_and_are_ignored(wl):
    response = await post(wl, refund_event("RFF1", "CAPB2001", "189.00", status="FAILED"))
    assert response.json()["status"] == "ignored" and alerts(wl) == []


async def test_an_unreadable_genuine_refund_event_is_alerted(wl):
    event = refund_event("RFU1", "CAPB2001", "189.00")
    del event["resource"]["amount"]
    assert (await post(wl, event)).json()["status"] == "alerted"
    assert alerts(wl)[0][1]["reason"] == "unreadable refund event"


async def test_a_reversed_capture_raises_an_alert(wl):
    event = {"id": "WH-REV1", "event_type": "PAYMENT.CAPTURE.REVERSED",
             "resource": {"id": "CAPA1001", "amount": {"value": "26.48", "currency_code": "USD"}}}
    await post(wl, event)
    assert alerts(wl) == [(AlertKind.CAPTURE_REVERSED, {"event_id": "WH-REV1", "capture_id": "CAPA1001",
                                                        "amount": "26.48 USD"})]


# ---------------------------------------------------------------- dedupe


async def test_a_redelivered_event_is_processed_once(wl):
    event = refund_event("RFOUT1", "CAPB2001", "189.00")
    first, second = await post(wl, event), await post(wl, event)
    assert (first.json()["status"], second.json()["status"]) == ("alerted", "duplicate")
    assert len(alerts(wl)) == 1


async def test_a_forged_copy_of_an_event_id_cannot_hide_the_genuine_event(wl):
    """An attacker posts a fake event with the real event's id first. It fails verification,
    and must not make Wardlock treat the genuine delivery as a duplicate."""
    event = refund_event("RFOUT1", "CAPB2001", "189.00")
    wl.fake.verify_status = "FAILURE"
    assert (await post(wl, {**event, "resource": {"id": "harmless"}})).status_code == 400

    wl.fake.verify_status = "SUCCESS"
    response = await post(wl, event)

    assert response.json()["status"] == "alerted"
    with wl.factory() as db:
        row = db.get(WebhookEvent, event["id"])
        assert row.verified is True and row.payload_json["resource"]["id"] == "RFOUT1"


async def test_counts_after_processing(wl):
    await post(wl, refund_event("RFOUT1", "CAPB2001", "189.00"))
    assert count(wl, WebhookEvent, WebhookEvent.verified.is_(True)) == 1
    assert count(wl, Decision) == 0  # a webhook never creates decisions


async def _open_page(wl):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=wl.app), base_url="http://localhost:8000") as http:
        return await http.get("/webhooks/paypal")


async def test_a_browser_gets_a_page_saying_verification_is_on(wl):
    page = await _open_page(wl)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "PAYMENT.CAPTURE.REFUNDED" in page.text and ">ON<" in page.text
    assert wl.fake.verify_requests == []  # opening the page asks PayPal nothing


async def test_the_page_says_off_without_a_webhook_id(wl):
    settings = Settings(_env_file=None, paypal_webhook_id="")
    wl.app.dependency_overrides[get_settings] = lambda: settings
    page = await _open_page(wl)
    assert ">OFF<" in page.text and ">ON<" not in page.text
