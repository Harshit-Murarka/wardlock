"""Config A: the same agent with PayPal's own create_refund and no Wardlock (SPEC §14).

This is the baseline that Wardlock is measured against, so it is deliberately what a
direct integration looks like:
- `lookup_orders` reads the merchant's order table for the verified customer, including
  PayPal capture IDs and amounts, as a raw integration must.
- `create_refund` is PayPal's tool, from PayPal's official MCP server (`@paypal/mcp`),
  which takes any capture ID and any amount.

It is the one deliberate exception to invariant 1: here PayPal's own code calls PayPal,
because "no Wardlock" is the point. Wardlock's code is not involved.

DRY RUN by default: create_refund calls are recorded and answered with a fake success, so
attack tickets drain no sandbox orders. `live=True` sends them to PayPal for real.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from demo_agent.agent_loop import AgentRun, ToolResult, Toolbox, run_agent
from demo_agent.mcp_toolbox import McpToolbox
from demo_agent.tickets import Ticket
from wardlock.llm import LLMClient
from wardlock.models import Order
from wardlock.money import from_minor

CONFIG = "A"
PAYPAL_MCP_PACKAGE = "@paypal/mcp@1.8.1"  # pinned: the eval must not change under us
REFUND_TOOL = "create_refund"

LOOKUP_ORDERS_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_orders",
        "description": "List the verified customer's orders, with items, amounts and PayPal capture IDs.",
        "parameters": {"type": "object", "properties": {}},
    },
}


@dataclass(frozen=True)
class RefundAttempt:
    capture_id: str
    amount: str | None  # None: no amount, which PayPal treats as a FULL refund
    currency: str | None
    sent: bool  # False in a dry run


class RawToolbox:
    """lookup_orders + PayPal's create_refund."""

    def __init__(self, session_factory: sessionmaker[Session], customer_id: str, paypal: Toolbox, *, live: bool):
        self._session_factory = session_factory
        self._customer_id = customer_id
        self._paypal = paypal
        self._live = live
        self.refunds: list[RefundAttempt] = []
        self.tools = [LOOKUP_ORDERS_TOOL, *(t for t in paypal.tools if t["function"]["name"] == REFUND_TOOL)]
        if len(self.tools) != 2:
            raise RuntimeError(f"PayPal's MCP server did not offer {REFUND_TOOL}")

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "lookup_orders":
            return ToolResult(json.dumps(self._orders()))
        return await self._refund(arguments)

    def _orders(self) -> list[dict[str, Any]]:
        with self._session_factory() as db:
            orders = db.scalars(select(Order).where(Order.customer_id == self._customer_id).order_by(Order.placed_at))
            return [
                {
                    "order_ref": o.order_ref,
                    "placed_at": o.placed_at.isoformat(),
                    "status": str(o.status),
                    "capture_id": o.capture_id,
                    "currency": o.currency,
                    "amount_paid": str(from_minor(o.captured_minor, o.currency)) if o.captured_minor is not None else None,
                    "shipping": str(from_minor(o.shipping_minor, o.currency)),
                    "items": [
                        {"name": i["name"], "unit_price": str(from_minor(i["unit_minor"], o.currency)),
                         "quantity": i["quantity"]}
                        for i in o.items_json
                    ],
                }
                for o in orders
            ]

    async def _refund(self, arguments: dict[str, Any]) -> ToolResult:
        amount = arguments.get("amount") if isinstance(arguments.get("amount"), dict) else {}
        attempt = RefundAttempt(
            capture_id=str(arguments.get("capture_id", "")),
            amount=_str_or_none(amount.get("value")),
            currency=_str_or_none(amount.get("currency_code")),
            sent=self._live,
        )
        self.refunds.append(attempt)
        if self._live:
            return await self._paypal.call(REFUND_TOOL, arguments)
        # What PayPal would answer, so the agent behaves exactly as it would live.
        fake = {"id": f"DRYRUN{len(self.refunds)}", "status": "COMPLETED"}
        if attempt.amount is not None:
            fake["amount"] = {"value": attempt.amount, "currency_code": attempt.currency}
        return ToolResult(json.dumps(fake))


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


@asynccontextmanager
async def paypal_toolkit(access_token: str) -> AsyncIterator[McpToolbox]:
    """PayPal's official MCP server, run locally with npx, in SANDBOX mode only.

    The access token goes to the child process in its environment, never to the LLM.
    """
    npx = shutil.which("npx")
    if npx is None:
        raise RuntimeError("npx was not found: install Node.js to run config A")
    params = StdioServerParameters(
        command=npx,
        args=["-y", PAYPAL_MCP_PACKAGE, "--tools=all"],
        env={"PAYPAL_ACCESS_TOKEN": access_token, "PAYPAL_ENVIRONMENT": "SANDBOX"},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield await McpToolbox.load(session, only=[REFUND_TOOL])


async def run_config_a(
    llm: LLMClient,
    ticket: Ticket,
    *,
    session_factory: sessionmaker[Session],
    paypal: Toolbox,
    live: bool = False,
) -> tuple[AgentRun, list[RefundAttempt]]:
    toolbox = RawToolbox(session_factory, ticket.metadata.verified_customer_id, paypal, live=live)
    run = await run_agent(llm, toolbox, ticket, config=CONFIG)
    return run, toolbox.refunds
