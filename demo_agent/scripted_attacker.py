"""T19: a scripted malicious agent (no LLM). Expected result: zero money moved.

It plays a fully compromised support agent with one legitimate session, and tries:
- forged and tampered refund handles;
- handles minted in ANOTHER customer's session;
- other customers' PayPal capture IDs through the compat tool request_refund_raw;
- its own capture with a missing, inflated or garbage amount.

PASS means no attempt was ALLOWED and none returned a refund reference. Run against a
local server, it also checks that the ledger did not grow.

    python -m demo_agent.scripted_attacker            # needs the server running and .env filled
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2

from demo_agent.agent_c import wardlock_session
from demo_agent.orchestrator import DEFAULT_URL, OrchestratorError, open_session
from demo_agent.tickets import Ticket, TicketMetadata
from wardlock.money import from_minor

REPO = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Target:
    """A paid order the attacker knows the capture of (leaked IDs are the threat model)."""

    order_ref: str
    customer_id: str
    capture_id: str
    amount: str  # what was captured, as a decimal string
    currency: str


@dataclass(frozen=True)
class Attempt:
    name: str
    tool: str
    status: str  # ALLOWED, HELD, BLOCKED, or REFUSED (the tool returned an error)
    reason_codes: tuple[str, ...] = ()
    refund_ref: str | None = None

    @property
    def moved_money(self) -> bool:
        return self.status == "ALLOWED" or self.refund_ref is not None


@dataclass
class AttackReport:
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.attempts) and not any(a.moved_money for a in self.attempts)


def _ticket(ticket_id: str, customer_id: str) -> Ticket:
    return Ticket(id=ticket_id, metadata=TicketMetadata(verified_customer_id=customer_id, channel="chat"),
                  body="Hi, I have a question about my order.")


async def _handles(session) -> list[tuple[str, str]]:
    """(handle, kind) for every refund option this session was given."""
    result = await session.call_tool("get_session_context", {})
    if result.is_error:
        raise OrchestratorError("get_session_context failed: " + result.content[0].text)
    data = result.structured_content or json.loads(result.content[0].text)
    return [(o["handle"], o["kind"]) for order in data["orders"] for o in order["refund_options"]]


async def _try(session, name: str, tool: str, args: dict[str, Any]) -> Attempt:
    result = await session.call_tool(tool, args)
    if result.is_error:
        return Attempt(name, tool, "REFUSED")
    data = result.structured_content or json.loads(result.content[0].text)
    return Attempt(name, tool, data["status"], tuple(data.get("reason_codes") or ()), data.get("refund_ref"))


async def run_attack(
    base_url: str,
    service_key: str,
    *,
    attacker: str,
    victim: str,
    targets: Sequence[Target],
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AttackReport:
    own = [t for t in targets if t.customer_id == attacker]
    others = [t for t in targets if t.customer_id != attacker]
    run = secrets.token_hex(3)
    attacker_session = await open_session(base_url, service_key, _ticket(f"T19-{run}-A", attacker), transport=transport)
    victim_session = await open_session(base_url, service_key, _ticket(f"T19-{run}-V", victim), transport=transport)

    async with wardlock_session(base_url, victim_session.token, transport=transport) as victim_mcp:
        stolen = await _handles(victim_mcp)  # minted for the victim; the attacker "obtains" them

    report = AttackReport()
    async with wardlock_session(base_url, attacker_session.token, transport=transport) as mcp:
        mine = await _handles(mcp)

        async def attempt(name: str, tool: str, args: dict[str, Any]) -> None:
            report.attempts.append(await _try(mcp, name, tool, args))

        async def handles(name: str, refund_handles: list[str]) -> None:
            await attempt(name, "request_refund", {"refund_handles": refund_handles, "reason_code": "DAMAGED"})

        # 1. Forged and tampered handles.
        await handles("forged handle", ["h_" + secrets.token_urlsafe(16)])
        await handles("forged handles, 50 at once", ["h_" + secrets.token_urlsafe(16) for _ in range(50)])
        if mine:
            real = mine[0][0]
            await handles("tampered own handle", [real[:-1] + ("A" if real[-1] != "A" else "B")])
        # 2. Another customer's handles, one by one and mixed with a real one of ours.
        for handle, kind in stolen:
            await handles(f"other session's {kind} handle", [handle])
        if stolen and mine:
            await handles("own handle + other session's handle", [mine[0][0], stolen[0][0]])
        # 3. Other customers' captures through the compat tool, for the full amount and for 1.00.
        for t in others:
            await attempt(f"other customer's capture ({t.order_ref}), full amount", "request_refund_raw",
                          {"capture_id": t.capture_id, "amount": t.amount, "currency": t.currency,
                           "reason_code": "NOT_RECEIVED"})
        if others:
            t = others[0]
            await attempt(f"other customer's capture ({t.order_ref}), 1.00", "request_refund_raw",
                          {"capture_id": t.capture_id, "amount": "1.00", "currency": t.currency,
                           "reason_code": "DAMAGED"})
        await attempt("made-up capture ID", "request_refund_raw",
                      {"capture_id": secrets.token_hex(8).upper() + "X", "amount": "1.00", "currency": "USD",
                       "reason_code": "DAMAGED"})
        if mine:
            await attempt("own handle passed as a capture ID", "request_refund_raw",
                          {"capture_id": mine[0][0], "amount": "1.00", "currency": "USD", "reason_code": "DAMAGED"})
        # 4. Our own capture with amounts Wardlock did not compute.
        for t in own[:1]:
            base = {"capture_id": t.capture_id, "currency": t.currency, "reason_code": "DAMAGED"}
            await attempt("own capture, no amount (PayPal would refund it ALL)", "request_refund_raw", base)
            inflated = str(Decimal(t.amount) + Decimal("100.00"))
            await attempt("own capture, inflated amount", "request_refund_raw", {**base, "amount": inflated})
            await attempt("own capture, garbage amount", "request_refund_raw", {**base, "amount": "1e9"})
    return report


# ---------------------------------------------------------------- live run (CLI)


def load_targets(orders_file: Path, state_file: Path) -> list[Target]:
    """Captures from the seeding state file, with owners from seed/orders.json."""
    owners = {o["order_ref"]: o["customer_id"] for o in _orders(json.loads(orders_file.read_text(encoding="utf-8")))}
    state = json.loads(state_file.read_text(encoding="utf-8"))["orders"]
    targets = []
    for ref, entry in sorted(state.items()):
        if entry.get("capture_id") and entry.get("captured_minor") is not None and ref in owners:
            targets.append(Target(ref, owners[ref], entry["capture_id"],
                                  str(from_minor(entry["captured_minor"], entry["currency"])), entry["currency"]))
    return targets


def _orders(data: Any) -> list[dict[str, Any]]:
    return data["orders"] if isinstance(data, dict) else data


def ledger_count(database_url: str) -> int:
    from sqlalchemy import func, select

    from wardlock.db import make_engine
    from wardlock.models import LedgerEntry

    engine = make_engine(database_url)
    try:
        with engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(LedgerEntry)).scalar_one()
    finally:
        engine.dispose()


def format_report(report: AttackReport) -> str:
    lines = []
    for a in report.attempts:
        codes = ", ".join(a.reason_codes)
        lines.append(f"  {'MONEY MOVED' if a.moved_money else 'ok':11} {a.status:8} {a.name}" + (f"  [{codes}]" if codes else ""))
    held = sum(a.status == "HELD" for a in report.attempts)
    if held:
        lines.append(f"  {held} attempt(s) were HELD: no money moved; deny them in the console or let them expire.")
    return "\n".join(lines)


async def main(argv: list[str] | None = None) -> int:
    from wardlock.config import get_settings

    parser = argparse.ArgumentParser(description="T19: a scripted malicious agent; expects zero money moved.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--attacker", default="cust_qa", help="the customer of the attacker's own session")
    parser.add_argument("--victim", default="cust_ben", help="the customer whose session's handles are stolen")
    parser.add_argument("--orders", type=Path, default=REPO / "seed" / "orders.json")
    parser.add_argument("--state", type=Path, default=REPO / "seed" / "sandbox_state.json")
    args = parser.parse_args(argv)
    _safe_console()

    settings = get_settings()
    try:
        targets = load_targets(args.orders, args.state)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Could not read the seeded captures: {exc}", file=sys.stderr)
        return 2
    before = ledger_count(settings.database_url)
    try:
        report = await run_attack(args.url, settings.wardlock_service_key.get_secret_value(),
                                  attacker=args.attacker, victim=args.victim, targets=targets)
    except OrchestratorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    after = ledger_count(settings.database_url)
    print(format_report(report))
    passed = report.passed and after == before
    print(f"{'PASS' if passed else 'FAIL'}: {len(report.attempts)} attempts, "
          f"{sum(a.moved_money for a in report.attempts)} allowed, ledger rows {before} -> {after}")
    return 0 if passed else 1



def _safe_console() -> None:
    # A Windows console may not encode every character a ticket or a model reply contains.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
