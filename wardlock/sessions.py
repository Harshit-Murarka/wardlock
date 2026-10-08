"""Open and validate agent sessions (SPEC §10).

A session binds ONE support conversation to ONE verified customer. Only trusted
orchestrator code, holding WARDLOCK_SERVICE_KEY, can open one. It passes the customer id
from the ticket's METADATA, never from the ticket text. After that, the agent only
ever holds the session token.

Only a SHA-256 hash of each token is stored, so a database leak does not hand out
working tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from wardlock.config import Settings, get_settings
from wardlock.db import get_db
from wardlock.handles import expire_available_handles
from wardlock.models import AgentSession, Customer, SessionStatus, utcnow

# Ticket bodies are untrusted text. Capping them keeps a hostile ticket from filling the database.
MAX_TICKET_BODY_CHARS = 50_000


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UnknownCustomer(LookupError):
    pass


@dataclass(frozen=True)
class OpenedSession:
    session_id: str
    token: str  # handed to the orchestrator exactly once and never stored
    expires_at: datetime


def open_session(
    db: Session,
    *,
    ticket_id: str,
    customer_id: str,
    ticket_body: str,
    ttl_minutes: int,
    now: datetime | None = None,
) -> OpenedSession:
    if ttl_minutes <= 0:
        raise ValueError("SESSION_TTL_MINUTES must be positive")
    if db.get(Customer, customer_id) is None:
        raise UnknownCustomer(customer_id)
    now = now or utcnow()
    token = secrets.token_urlsafe(32)  # 256 random bits
    session = AgentSession(
        ticket_id=ticket_id,
        customer_id=customer_id,
        ticket_body=ticket_body,
        token_hash=hash_token(token),
        created_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes),
    )
    db.add(session)
    db.flush()
    return OpenedSession(session.id, token, session.expires_at)


def resolve_session(db: Session, token: str | None, now: datetime | None = None) -> AgentSession | None:
    """The live session this token belongs to, or None. An expired session is marked as such."""
    if not isinstance(token, str) or not token:
        return None
    session = db.scalar(select(AgentSession).where(AgentSession.token_hash == hash_token(token)))
    if session is None or session.status != SessionStatus.ACTIVE:
        return None
    if not session.is_live(now or utcnow()):
        expire_session(db, session)
        return None
    return session


def expire_session(db: Session, session: AgentSession) -> None:
    """End a session. Its unused handles expire with it (SPEC §4)."""
    session.status = SessionStatus.EXPIRED
    expire_available_handles(db, session.id)
    db.flush()


# ---------------------------------------------------------------- HTTP: POST /sessions

router = APIRouter()


class OpenSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    customer_id: str = Field(min_length=1, max_length=64)  # from ticket METADATA only
    ticket_body: str = Field(max_length=MAX_TICKET_BODY_CHARS)  # untrusted


class OpenSessionResponse(BaseModel):
    session_id: str
    session_token: str
    expires_at: datetime


def require_service_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_service_key: Annotated[str | None, Header()] = None,
) -> None:
    expected = settings.wardlock_service_key.get_secret_value()
    if not expected:
        # Fail closed: with no key configured, nobody may open sessions.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "WARDLOCK_SERVICE_KEY is not configured")
    # compare_digest takes the same time whatever the input, so timing reveals nothing about the key.
    if x_service_key is None or not hmac.compare_digest(x_service_key.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid service key")


@router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=OpenSessionResponse,
    dependencies=[Depends(require_service_key)],
)
def create_session(
    body: OpenSessionRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> OpenSessionResponse:
    try:
        opened = open_session(
            db,
            ticket_id=body.ticket_id,
            customer_id=body.customer_id,
            ticket_body=body.ticket_body,
            ttl_minutes=settings.session_ttl_minutes,
        )
    except UnknownCustomer:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown customer") from None
    db.commit()
    return OpenSessionResponse(
        session_id=opened.session_id, session_token=opened.token, expires_at=opened.expires_at
    )
