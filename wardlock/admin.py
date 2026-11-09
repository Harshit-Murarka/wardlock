"""The merchant console's API (SPEC §12): /admin/api/*, behind HTTP Basic auth.

Auth: user "admin", password WARDLOCK_ADMIN_PASSWORD, compared in constant time. Unset ->
503 for everyone (fail closed). A 401 deliberately has NO `WWW-Authenticate` header, so a
browser never caches the credentials: the console sends them itself on every call, and a
malicious page in another tab cannot ride on a cached login to approve a hold (CSRF).

Everything is read from existing tables. Approve and deny go through the executor, which
re-checks and refunds at most once.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wardlock.config import Settings, get_settings
from wardlock.db import get_db
from wardlock.engine.codes import EXPLANATIONS, Code
from wardlock.enums import HoldStatus, Verdict
from wardlock.models import AgentSession, Alert, Decision, Hold, LedgerEntry
from wardlock.money import from_minor
from wardlock.replay import segments

ADMIN_USER = "admin"
MAX_ROWS = 500


def require_admin(request: Request, settings: Settings = Depends(get_settings)) -> str:
    password = settings.wardlock_admin_password.get_secret_value()
    if not password:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The console is not configured.")
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    user, given = "", ""
    if scheme.lower() == "basic":
        try:
            user, _, given = base64.b64decode(encoded.strip(), validate=True).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            pass
    # Both comparisons always run, so timing reveals nothing about which part was wrong.
    user_ok = hmac.compare_digest(user.encode(), ADMIN_USER.encode())
    password_ok = hmac.compare_digest(given.encode(), password.encode())
    if not (user_ok and password_ok):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong console password.")  # no WWW-Authenticate
    return ADMIN_USER


router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)])
Db = Annotated[Session, Depends(get_db)]


def _money(minor: int | None, currency: str | None) -> str | None:
    return str(from_minor(minor, currency)) if minor is not None and currency else None


def _explain(codes: list[str]) -> list[dict[str, str]]:
    return [{"code": c, "explanation": EXPLANATIONS.get(Code(c), "") if c in Code.__members__ else ""} for c in codes]


def _executor(request: Request):
    executor = request.app.state.services.executor
    if executor is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "PayPal is not configured, so holds cannot be decided.")
    return executor


# ---------------------------------------------------------------- decisions


@router.get("/decisions")
def list_decisions(db: Db, verdict: Verdict | None = None, reason_code: str | None = None,
                   limit: Annotated[int, Query(ge=1, le=MAX_ROWS)] = 200) -> list[dict[str, Any]]:
    query = (select(Decision, AgentSession).join(AgentSession, Decision.session_id == AgentSession.id)
             .order_by(Decision.created_at.desc()))
    if verdict is not None:
        query = query.where(Decision.verdict == verdict)
    rows = db.execute(query.limit(MAX_ROWS)).all()
    if reason_code:
        rows = [(d, s) for d, s in rows if reason_code in (d.reason_codes or [])]
    holds = dict(db.execute(select(Hold.decision_id, Hold.id)).all())
    ledger = dict(db.execute(select(LedgerEntry.decision_id, LedgerEntry.id)).all())
    return [
        {
            "id": d.id, "created_at": d.created_at.isoformat(), "ticket_id": s.ticket_id,
            "customer_id": s.customer_id, "session_id": s.id, "tool": d.tool, "verdict": d.verdict,
            "reason_codes": d.reason_codes or [], "amount": _money(d.amount_minor, d.currency),
            "currency": d.currency, "latency_ms": d.latency_ms, "hold_id": holds.get(d.id),
            "refund_ref": ledger.get(d.id),
        }
        for d, s in rows[:limit]
    ]


# ---------------------------------------------------------------- holds


@router.get("/holds")
def list_holds(db: Db, hold_status: Annotated[HoldStatus | None, Query(alias="status")] = None) -> list[dict[str, Any]]:
    query = (select(Hold, Decision, AgentSession).join(Decision, Hold.decision_id == Decision.id)
             .join(AgentSession, Decision.session_id == AgentSession.id).order_by(Hold.created_at.desc()))
    if hold_status is not None:
        query = query.where(Hold.status == hold_status)
    return [
        {
            "id": h.id, "decision_id": d.id, "status": h.status, "created_at": h.created_at.isoformat(),
            "ticket_id": s.ticket_id, "customer_id": s.customer_id, "session_id": s.id,
            "amount": _money(h.frozen_request_json.get("amount_minor"), h.frozen_request_json.get("currency")),
            "currency": h.frozen_request_json.get("currency"), "capture_id": h.frozen_request_json.get("capture_id"),
            "reasons": _explain(d.reason_codes or []), "outcome_unknown": h.outcome_unknown,
            "approver": h.approver, "decided_at": h.decided_at.isoformat() if h.decided_at else None,
        }
        for h, d, s in db.execute(query.limit(MAX_ROWS)).all()
    ]


@router.post("/holds/{hold_id}/approve")
async def approve_hold(hold_id: str, request: Request) -> dict[str, Any]:
    outcome = await _executor(request).approve_hold(hold_id, approver=ADMIN_USER)
    return _hold_answer(outcome)


@router.post("/holds/{hold_id}/deny")
def deny_hold(hold_id: str, request: Request) -> dict[str, Any]:
    return _hold_answer(_executor(request).deny_hold(hold_id, approver=ADMIN_USER))


def _hold_answer(outcome) -> dict[str, Any]:
    if outcome.status == "UNKNOWN":
        raise HTTPException(status.HTTP_404_NOT_FOUND, outcome.message)
    return {"hold_id": outcome.hold_id, "status": outcome.status, "executed": outcome.executed,
            "refund_ref": outcome.refund_ref, "reason_codes": list(outcome.reason_codes), "message": outcome.message}


# ---------------------------------------------------------------- alerts and ledger


@router.get("/alerts")
def list_alerts(db: Db) -> list[dict[str, Any]]:
    rows = db.scalars(select(Alert).order_by(Alert.created_at.desc()).limit(MAX_ROWS))
    return [{"id": a.id, "kind": a.kind, "created_at": a.created_at.isoformat(), "acknowledged": a.acknowledged,
             "details": a.details_json} for a in rows]


@router.post("/alerts/{alert_id}/ack")
def acknowledge_alert(alert_id: str, db: Db) -> dict[str, Any]:
    with db.begin():
        alert = db.get(Alert, alert_id)
        if alert is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no such alert.")
        alert.acknowledged = True
    return {"id": alert_id, "acknowledged": True}


@router.get("/ledger")
def list_ledger(db: Db) -> list[dict[str, Any]]:
    query = (select(LedgerEntry, Decision, AgentSession).join(Decision, LedgerEntry.decision_id == Decision.id)
             .join(AgentSession, Decision.session_id == AgentSession.id).order_by(LedgerEntry.created_at.desc()))
    return [
        {"id": e.id, "created_at": e.created_at.isoformat(), "decision_id": d.id, "ticket_id": s.ticket_id,
         "session_id": s.id,
         "customer_id": s.customer_id, "capture_id": e.capture_id, "paypal_refund_id": e.paypal_refund_id,
         "amount": _money(e.amount_minor, e.currency), "currency": e.currency, "status": e.status}
        for e, d, s in db.execute(query.limit(MAX_ROWS)).all()
    ]


# ---------------------------------------------------------------- ticket replay


@router.get("/sessions")
def list_sessions(db: Db) -> list[dict[str, Any]]:
    counts = dict(db.execute(select(Decision.session_id, func.count()).group_by(Decision.session_id)).all())
    rows = db.scalars(select(AgentSession).order_by(AgentSession.created_at.desc()).limit(MAX_ROWS))
    return [{"id": s.id, "ticket_id": s.ticket_id, "customer_id": s.customer_id, "created_at": s.created_at.isoformat(),
             "status": s.status, "refund_calls": counts.get(s.id, 0)} for s in rows]


@router.get("/sessions/{session_id}/replay")
def replay(session_id: str, db: Db) -> dict[str, Any]:
    session = db.get(AgentSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no such session.")
    decisions = db.scalars(select(Decision).where(Decision.session_id == session_id).order_by(Decision.created_at))
    holds = {h.decision_id: h for h in db.scalars(select(Hold).join(Decision).where(Decision.session_id == session_id))}
    ledger = {e.decision_id: e for e in db.scalars(select(LedgerEntry).join(Decision)
                                                   .where(Decision.session_id == session_id))}
    calls = []
    for d in decisions:
        hold, entry = holds.get(d.id), ledger.get(d.id)
        calls.append({
            "decision_id": d.id, "created_at": d.created_at.isoformat(), "tool": d.tool,
            "request": d.request_json, "verdict": d.verdict, "reasons": _explain(d.reason_codes or []),
            "amount": _money(d.amount_minor, d.currency), "currency": d.currency,
            "hold": {"id": hold.id, "status": hold.status} if hold else None,
            "refund": {"id": entry.id, "status": entry.status} if entry else None,
            "reason_check": (d.details_json or {}).get("reason_check"),
        })
    return {
        "session": {"id": session.id, "ticket_id": session.ticket_id, "customer_id": session.customer_id,
                    "created_at": session.created_at.isoformat(), "status": session.status},
        "ticket": [{"text": s.text, "suspicious": s.suspicious, "why": s.why} for s in segments(session.ticket_body)],
        "calls": calls,
    }
