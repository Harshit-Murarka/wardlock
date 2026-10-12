"""Ticket files (SPEC §2): trusted metadata from the helpdesk, plus an untrusted body.

Only `metadata.verified_customer_id` decides whose orders the agent may touch. The body is
what the customer wrote, so it is never used for identity, whatever it claims.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
MAX_BODY_CHARS = 50_000  # the same limit POST /sessions enforces


class TicketMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verified_customer_id: Annotated[str, Field(pattern=ID_PATTERN)]
    channel: Literal["email", "chat", "web"]


class Ticket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(pattern=ID_PATTERN)]
    metadata: TicketMetadata
    body: Annotated[str, Field(min_length=1, max_length=MAX_BODY_CHARS)]
    # Used only by the eval corpus (SPEC §14). The agents never see them.
    expected: Literal["ALLOW", "HOLD", "BLOCK", "NO_ACTION"] | None = None
    category: Annotated[str, Field(max_length=64)] | None = None


def load_ticket(path: str | Path) -> Ticket:
    """Read and validate one ticket file. Raises ValueError (pydantic's ValidationError) if it is malformed."""
    with open(path, encoding="utf-8") as f:
        return Ticket.model_validate(json.load(f))
