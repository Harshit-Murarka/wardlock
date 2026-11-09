"""Check that a running Wardlock's MCP server works, without moving any money.

    python scripts/mcp_check.py                                    # http://localhost:8000
    python scripts/mcp_check.py --url https://wardlock.onrender.com

It needs WARDLOCK_SERVICE_KEY in .env (the same value as on that server), because it plays
the trusted helpdesk to open one support session. It never prints the key or the token.
Each line is one check; the exit code is 0 only if every check passed. It makes no refund:
it only reads the session's context and asks for the status of a reference that doesn't exist.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # so `python scripts/mcp_check.py` finds the packages

from demo_agent.agent_c import WARDLOCK_TOOLS, wardlock_session  # noqa: E402
from demo_agent.orchestrator import DEFAULT_URL, OrchestratorError, open_session, wake_up  # noqa: E402
from demo_agent.tickets import Ticket  # noqa: E402

# PayPal capture ids are 17 upper-case letters and digits; agents must never see one.
_CAPTURE_LIKE = re.compile(r"\b[0-9A-Z]{17}\b")


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))
        self.failed += not ok
        return ok


def _ticket(customer: str) -> Ticket:
    return Ticket.model_validate({
        "id": "MCP-CHECK", "metadata": {"verified_customer_id": customer, "channel": "web"},
        "body": "Connectivity check; please do nothing.",
    })


def _text(result) -> str:
    return result.content[0].text if result.content else ""


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


async def run(url: str, service_key: str, customer: str, *, transport=None) -> int:
    """Run every check; `transport` lets the tests run it against the app in-process."""
    checks = Checks()
    print(f"Wardlock MCP check: {url}")
    await wake_up(url, transport=transport)
    checks.check("server is up (/health)", True)

    async with wardlock_session(url, "not-a-real-token", transport=transport) as anonymous:
        result = await anonymous.call_tool("get_session_context", {})
        checks.check("a call without a valid session token is refused", result.is_error, _text(result)[:80])

    opened = await open_session(url, service_key, _ticket(customer), transport=transport)
    checks.check(f"helpdesk opened a session for {customer}", True)

    async with wardlock_session(url, opened.token, transport=transport) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        checks.check("exactly the four Wardlock tools are offered", set(tools) == set(WARDLOCK_TOOLS),
                     ", ".join(sorted(tools)))
        refund = tools.get("request_refund")
        params = set(refund.input_schema.get("properties", {})) if refund else set()
        checks.check("request_refund has no amount parameter (G2)", refund is not None and not (
            params & {"amount", "value", "capture_id"}), ", ".join(sorted(params)))

        result = await session.call_tool("get_session_context", {})
        context = result.structured_content or (json.loads(_text(result)) if not result.is_error else {})
        orders = context.get("orders", [])
        options = [o for order in orders for o in order.get("refund_options", [])]
        checks.check("get_session_context lists this customer's orders", not result.is_error and bool(orders),
                     _text(result)[:80])
        checks.check("every refund option is an opaque handle with a computed amount",
                     bool(options) and all(o.get("handle") and o.get("amount") for o in options))
        raw = json.dumps(context)
        checks.check("no PayPal capture id is shown to the agent",
                     not (_keys(context) & {"capture_id", "paypal_capture_id"}) and not _CAPTURE_LIKE.search(raw))

        result = await session.call_tool("get_refund_status", {"ref": "rf_does_not_exist"})
        checks.check("an unknown refund reference is a clean 'not found'", result.is_error and "No refund" in _text(result),
                     _text(result)[:80])

    print("PASS: the MCP server works" if checks.failed == 0 else f"FAIL: {checks.failed} check(s) failed")
    return 0 if checks.failed == 0 else 1


async def main(argv: list[str] | None = None) -> int:
    from wardlock.config import get_settings

    parser = argparse.ArgumentParser(description="Check a running Wardlock's MCP server (moves no money).")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--customer", default="cust_ana", help="a seeded customer to open the session for")
    args = parser.parse_args(argv)
    key = get_settings().wardlock_service_key.get_secret_value()
    try:
        if not key:
            raise OrchestratorError("WARDLOCK_SERVICE_KEY is not set in .env")
        return await run(args.url.rstrip("/"), key, args.customer)
    except OrchestratorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
