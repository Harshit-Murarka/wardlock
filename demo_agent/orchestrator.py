"""The trusted orchestrator: read a ticket, open a Wardlock session, run the support agent.

It plays the helpdesk's role (SPEC §2). It holds WARDLOCK_SERVICE_KEY and opens the session
from the ticket's METADATA only, so nothing the customer wrote can choose whose orders the
agent sees. It prints the run, never a token or a key.

    python -m demo_agent.orchestrator demo_agent/tickets/T-0001.json            # config C
    python -m demo_agent.orchestrator demo_agent/tickets/T-0002.json --config A # dry run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass

import httpx2

from demo_agent.agent_c import run_config_c
from demo_agent.agent_loop import AgentRun
from demo_agent.tickets import Ticket, load_ticket
from wardlock.config import SANDBOX_BASE_URL, Settings, get_settings
from wardlock.llm import LLMClient

DEFAULT_URL = "http://localhost:8000"
AGENT_LLM_TIMEOUT_SECONDS = 60.0  # an agent turn can be slower than the 8 s reason check


class OrchestratorError(Exception):
    """A setup problem (no key, server down, session refused). Never contains a secret."""


@dataclass(frozen=True)
class OpenedSession:
    session_id: str
    token: str

    def __repr__(self) -> str:
        return f"OpenedSession(session_id={self.session_id!r})"  # never the token


async def open_session(
    base_url: str, service_key: str, ticket: Ticket, *, transport: httpx2.AsyncBaseTransport | None = None
) -> OpenedSession:
    """POST /sessions with the ticket's metadata. The body goes along only as untrusted text."""
    if not service_key:
        raise OrchestratorError("WARDLOCK_SERVICE_KEY is not set in .env")
    payload = {
        "ticket_id": ticket.id,
        "customer_id": ticket.metadata.verified_customer_id,  # metadata, never the body
        "ticket_body": ticket.body,
    }
    try:
        async with httpx2.AsyncClient(base_url=base_url.rstrip("/"), transport=transport, timeout=30.0) as http:
            response = await http.post("/sessions", json=payload, headers={"X-Service-Key": service_key})
    except httpx2.HTTPError as exc:
        raise OrchestratorError(f"could not reach Wardlock at {base_url} ({type(exc).__name__})") from None
    if response.status_code != 201:
        raise OrchestratorError(f"Wardlock refused the session: HTTP {response.status_code}")
    data = response.json()
    return OpenedSession(data["session_id"], data["session_token"])


async def run_c(
    llm: LLMClient, ticket: Ticket, *, base_url: str, service_key: str,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AgentRun:
    opened = await open_session(base_url, service_key, ticket, transport=transport)
    return await run_config_c(llm, ticket, base_url=base_url, session_token=opened.token, transport=transport)


async def run_a(llm: LLMClient, ticket: Ticket, settings: Settings, *, live: bool):
    # Imported here: only config A needs PayPal credentials, the DB and Node.js.
    from demo_agent.agent_a import paypal_toolkit, run_config_a
    from wardlock.db import get_engine, make_session_factory
    from wardlock.paypal.client import PayPalClient

    if settings.paypal_base_url.rstrip("/") != SANDBOX_BASE_URL:
        raise OrchestratorError("config A runs against the PayPal sandbox only")
    try:
        paypal_client = PayPalClient.from_settings(settings)
    except ValueError as exc:
        raise OrchestratorError(f"PayPal is not configured: {exc}") from None
    async with paypal_client:
        token = await paypal_client.access_token()
    async with paypal_toolkit(token) as paypal:
        return await run_config_a(llm, ticket, session_factory=make_session_factory(get_engine()),
                                  paypal=paypal, live=live)


def format_run(run: AgentRun) -> str:
    lines = [f"Config {run.config} | ticket {run.ticket_id} | {run.steps} model turns | {run.seconds:.1f} s"]
    for i, call in enumerate(run.calls, 1):
        marker = "ERROR " if call.is_error else ""
        lines.append(f"  {i}. {call.tool}({json.dumps(call.arguments)})")
        lines.append(f"     -> {marker}{_one_line(call.result)}")
    if run.stopped != "done":
        lines.append(f"  stopped: {run.stopped}" + (f" ({run.error})" if run.error else ""))
    lines.append(f"Reply to customer: {run.reply or '(none)'}")
    return "\n".join(lines)


def _one_line(text: str, limit: int = 300) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ticket", help="a ticket JSON file (SPEC §2)")
    parser.add_argument("--config", choices=("C", "A"), default="C")
    parser.add_argument("--url", default=DEFAULT_URL, help="Wardlock's URL (config C)")
    parser.add_argument("--live", action="store_true",
                        help="config A only: really send create_refund to the PayPal sandbox (default: dry run)")
    args = parser.parse_args(argv)
    _safe_console()

    settings = get_settings()
    try:
        ticket = load_ticket(args.ticket)
    except (OSError, ValueError) as exc:
        print(f"Bad ticket file: {exc}", file=sys.stderr)
        return 2
    if not (settings.llm_base_url and settings.llm_api_key.get_secret_value() and settings.llm_model):
        print("No LLM configured: set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL in .env", file=sys.stderr)
        return 2
    try:
        llm = LLMClient(settings.llm_base_url, settings.llm_api_key, settings.llm_model,
                        timeout=AGENT_LLM_TIMEOUT_SECONDS)
    except ValueError as exc:
        print(f"Bad LLM settings: {exc}", file=sys.stderr)
        return 2
    try:
        if args.config == "C":
            run = await run_c(llm, ticket, base_url=args.url,
                              service_key=settings.wardlock_service_key.get_secret_value())
            print(format_run(run))
        else:
            run, refunds = await run_a(llm, ticket, settings, live=args.live)
            print(format_run(run))
            mode = "SENT to PayPal" if args.live else "dry run, NOT sent"
            for r in refunds:
                print(f"create_refund ({mode}): capture {r.capture_id}, amount {r.amount or 'FULL CAPTURE'} {r.currency or ''}")
    except OrchestratorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        await llm.aclose()
    return 0 if run.stopped == "done" else 1



def _safe_console() -> None:
    # A Windows console may not encode every character a ticket or a model reply contains.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
