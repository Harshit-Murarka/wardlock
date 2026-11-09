"""PayPal webhooks and reconciliation (SPEC §8): G3, catch any refund Wardlock did not make.

POST /webhooks/paypal
1. Read the raw body (capped), and parse it only to find the event id and type.
2. Ask PayPal to verify the signature, sending the event exactly as received.
   Not genuine -> 400 and stored as unverified, nothing else happens.
   PayPal unreachable -> 503, so PayPal delivers again later.
3. Skip an event already processed as VERIFIED. An unverified row never counts, so a
   forged copy of an event id cannot make Wardlock ignore the genuine event.
4. PAYMENT.CAPTURE.REFUNDED, in this order:
   a. the ledger already has this PayPal refund id -> Wardlock's own refund, done;
   b. custom_id names a Wardlock decision whose capture AND amount match -> Wardlock's own
      refund whose response was lost: backfill the ledger (no alert);
   c. anything else -> alert OUT_OF_BAND_REFUND.
   PAYMENT.CAPTURE.REVERSED -> alert CAPTURE_REVERSED. Other types are stored and acknowledged.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from wardlock.config import Settings, get_settings
from wardlock.enums import AlertKind, Verdict
from wardlock.mcp_tools import AgentServices
from wardlock.models import Alert, Decision, LedgerEntry, WebhookEvent
from wardlock.money import to_minor
from wardlock.paypal.errors import PayPalAuthError, PayPalRequestError, PayPalUnavailable

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY_BYTES = 256 * 1024  # real PayPal events are a few KB
REFUNDED = "PAYMENT.CAPTURE.REFUNDED"
REVERSED = "PAYMENT.CAPTURE.REVERSED"
MONEY_MOVED_STATUSES = ("COMPLETED", "PENDING")  # the same statuses the executor counts as refunded
TRANSMISSION_HEADERS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}
_CAPTURE_LINK = re.compile(r"/v2/payments/captures/([A-Za-z0-9-]{1,64})/?$")


class _TooLarge(Exception):
    pass


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request, settings: Settings = Depends(get_settings)) -> JSONResponse:
    try:
        raw = await _read_capped(request)
    except _TooLarge:
        return _answer(413, "event too large")
    event = _parse(raw)
    if event is None:
        return _answer(400, "not a PayPal event")

    services: AgentServices = request.app.state.services
    if not settings.paypal_webhook_id or services.paypal is None or services.executor is None:
        # Fail closed: without verification nothing can be trusted, and PayPal will retry.
        return _answer(503, "webhooks are not configured")

    transmission = {field: request.headers.get(header) for field, header in TRANSMISSION_HEADERS.items()}
    if not all(transmission.values()):
        _store_unverified(services, event)
        return _answer(400, "missing PayPal signature headers")
    try:
        genuine = await services.paypal.verify_webhook_signature(transmission, raw, settings.paypal_webhook_id)
    except PayPalRequestError:
        genuine = False  # PayPal called our verification request malformed: treat as not genuine
    except (PayPalUnavailable, PayPalAuthError):
        logger.warning("Could not verify webhook %s: PayPal unavailable", event["id"])
        return _answer(503, "could not verify the event, deliver again later")
    if not genuine:
        logger.warning("Webhook %s failed signature verification", event["id"])
        _store_unverified(services, event)
        return _answer(400, "signature verification failed")

    if _already_processed(services, event["id"]):
        return _answer(200, "duplicate")
    outcome = _process(services, event)
    _store_verified(services, event)  # only after processing: a crash means PayPal re-delivers
    return _answer(200, outcome)


# ---------------------------------------------------------------- reading the request


async def _read_capped(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _TooLarge
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise _TooLarge
    return bytes(body)


def _parse(raw: bytes) -> dict[str, Any] | None:
    """The event as a dict with a usable id and type, or None. Used only to route the event."""
    if b"\x00" in raw or rb"\u0000" in raw:  # a raw NUL, or JSON's escaped form of it
        return None  # PayPal never sends NUL, and Postgres can't store it: not a PayPal event
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(event, dict):
        return None
    event_id, event_type = event.get("id"), event.get("event_type")
    if not (isinstance(event_id, str) and 0 < len(event_id) <= 100 and isinstance(event_type, str) and event_type):
        return None
    return event


def _answer(status: int, message: str) -> JSONResponse:
    return JSONResponse({"status": message}, status_code=status)


# ---------------------------------------------------------------- storing events


def _resource_id(event: dict[str, Any]) -> str | None:
    resource = event.get("resource")
    value = resource.get("id") if isinstance(resource, dict) else None
    return value[:64] if isinstance(value, str) else None


def _store_unverified(services: AgentServices, event: dict[str, Any]) -> None:
    """Keep a record for investigation, but never in a way that blocks the genuine event."""
    with services.session_factory() as db:
        try:
            with db.begin():
                if db.get(WebhookEvent, event["id"]) is None:
                    db.add(WebhookEvent(event_id=event["id"], event_type=event["event_type"][:64],
                                        resource_id=_resource_id(event), verified=False, payload_json=event))
        except IntegrityError:
            pass  # stored concurrently; nothing to add


def _already_processed(services: AgentServices, event_id: str) -> bool:
    with services.session_factory() as db:
        row = db.get(WebhookEvent, event_id)
        return row is not None and row.verified


def _store_verified(services: AgentServices, event: dict[str, Any]) -> None:
    with services.session_factory() as db:
        try:
            with db.begin():
                row = db.get(WebhookEvent, event["id"])
                if row is None:
                    db.add(WebhookEvent(event_id=event["id"], event_type=event["event_type"][:64],
                                        resource_id=_resource_id(event), verified=True, payload_json=event))
                else:  # an unverified copy came first (maybe forged): the genuine event replaces it
                    row.verified, row.payload_json = True, event
                    row.event_type, row.resource_id = event["event_type"][:64], _resource_id(event)
        except IntegrityError:
            pass  # a concurrent delivery of the same event stored it


# ---------------------------------------------------------------- reconciliation


def _process(services: AgentServices, event: dict[str, Any]) -> str:
    if event["event_type"] == REFUNDED:
        return _reconcile_refund(services, event)
    if event["event_type"] == REVERSED:
        resource = event.get("resource") if isinstance(event.get("resource"), dict) else {}
        _alert(services, AlertKind.CAPTURE_REVERSED, {
            "event_id": event["id"], "capture_id": _resource_id(event), "amount": _amount_text(resource),
        })
        return "alerted"
    return "ignored"


def _reconcile_refund(services: AgentServices, event: dict[str, Any]) -> str:
    facts = _refund_facts(event)
    if facts is None:
        # A genuine refund event we cannot read: a person must look (fail closed).
        _alert(services, AlertKind.OUT_OF_BAND_REFUND, {"event_id": event["id"], "reason": "unreadable refund event",
                                                        "refund_id": _resource_id(event)})
        return "alerted"
    if facts["status"] not in MONEY_MOVED_STATUSES:
        return "ignored"  # FAILED or CANCELLED: no money moved

    with services.session_factory() as db:
        # a. Already in the ledger: Wardlock made and recorded it.
        if db.scalar(select(LedgerEntry.id).where(LedgerEntry.paypal_refund_id == facts["refund_id"])):
            return "already recorded"
        reason = _why_not_ours(db, facts)

    if reason is None:
        # b. Wardlock's own refund whose response was lost: backfill.
        ledger_id = services.executor.record_webhook_refund(facts["custom_id"], facts["refund_id"], facts["status"])
        with services.session_factory() as db:
            recorded = db.get(LedgerEntry, ledger_id)
            if recorded is not None and recorded.paypal_refund_id == facts["refund_id"]:
                return "backfilled"
        reason = "the decision is already linked to a different refund"

    # c. Not explained by any Wardlock decision.
    _alert(services, AlertKind.OUT_OF_BAND_REFUND, {
        "event_id": event["id"], "refund_id": facts["refund_id"], "capture_id": facts["capture_id"],
        "amount": facts["amount"], "currency": facts["currency"], "custom_id": facts["custom_id"], "reason": reason,
    })
    return "alerted"


def _refund_facts(event: dict[str, Any]) -> dict[str, Any] | None:
    """The refund's id, status, amount, capture and custom_id, or None if any key part is missing.

    Confirmed with a real sandbox event (Nov 9, 2026): the resource is the refund object,
    custom_id is echoed back, and the capture is the "up" link (/v2/payments/captures/{id}).
    If the capture were ever missing, step b could not match and the refund would be alerted.
    """
    resource = event.get("resource")
    if not isinstance(resource, dict) or not isinstance(resource.get("id"), str):
        return None
    amount = resource.get("amount")
    if not isinstance(amount, dict):
        return None
    value, currency = amount.get("value"), amount.get("currency_code")
    if not isinstance(value, str) or not isinstance(currency, str):
        return None
    try:
        minor = to_minor(Decimal(value), currency)
    except (InvalidOperation, ValueError):
        return None
    custom_id = resource.get("custom_id")
    return {
        "refund_id": resource["id"][:64],
        "status": str(resource.get("status") or ""),
        "amount": value,
        "amount_minor": minor,
        "currency": currency,
        "capture_id": _capture_from_links(resource.get("links")),
        "custom_id": custom_id[:127] if isinstance(custom_id, str) else None,
    }


def _capture_from_links(links: Any) -> str | None:
    if not isinstance(links, list):
        return None
    for link in links:
        if isinstance(link, dict) and link.get("rel") == "up" and isinstance(link.get("href"), str):
            match = _CAPTURE_LINK.search(link["href"])
            if match:
                return match.group(1)
    return None


def _why_not_ours(db, facts: dict[str, Any]) -> str | None:
    """None if the event is exactly one of Wardlock's own unrecorded refunds, else the reason it isn't.

    Matching capture AND amount stops someone outside Wardlock from hiding a refund by
    copying a real decision id into custom_id (T21).
    """
    if not facts["custom_id"]:
        return "no custom_id: not made by Wardlock"
    decision = db.get(Decision, facts["custom_id"])
    if decision is None or decision.idempotency_key is None:
        return "custom_id does not name a Wardlock refund"
    if decision.verdict == Verdict.BLOCK:
        return "the decision was blocked, so Wardlock never sent it"
    if decision.capture_id != facts["capture_id"]:
        return "the capture does not match the decision"
    if decision.currency != facts["currency"] or decision.amount_minor != facts["amount_minor"]:
        return "the amount does not match the decision"
    if db.scalar(select(LedgerEntry.id).where(LedgerEntry.decision_id == decision.id)):
        return "the decision is already linked to a different refund"
    return None


def _amount_text(resource: dict[str, Any]) -> str | None:
    amount = resource.get("amount")
    if isinstance(amount, dict) and isinstance(amount.get("value"), str):
        return f"{amount['value']} {amount.get('currency_code', '')}".strip()
    return None


def _alert(services: AgentServices, kind: AlertKind, details: dict[str, Any]) -> None:
    logger.warning("ALERT %s: %s", kind, details.get("reason") or details.get("event_id"))
    with services.session_factory() as db, db.begin():
        db.add(Alert(kind=kind, details_json=details))
