"""Config C: the support agent with Wardlock's MCP tools and nothing else.

The session token lives only in this client's Authorization header. The LLM never sees it,
so a prompt-injected model cannot leak or reuse it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from demo_agent.agent_loop import AgentRun, run_agent
from demo_agent.mcp_toolbox import McpToolbox
from demo_agent.tickets import Ticket
from wardlock.llm import LLMClient

CONFIG = "C"
WARDLOCK_TOOLS = ("get_session_context", "request_refund", "get_refund_status", "request_refund_raw")


@asynccontextmanager
async def wardlock_session(
    base_url: str, session_token: str, *, transport: httpx2.AsyncBaseTransport | None = None
) -> AsyncIterator[ClientSession]:
    """An MCP client session with Wardlock. `transport` lets tests run the app in-process."""
    base = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {session_token}"}
    timeout = httpx2.Timeout(30.0, read=120.0)
    async with httpx2.AsyncClient(base_url=base, headers=headers, timeout=timeout, transport=transport) as http:
        async with streamable_http_client(f"{base}/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


async def run_config_c(
    llm: LLMClient,
    ticket: Ticket,
    *,
    base_url: str,
    session_token: str,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AgentRun:
    async with wardlock_session(base_url, session_token, transport=transport) as session:
        toolbox = await McpToolbox.load(session, only=WARDLOCK_TOOLS)
        return await run_agent(llm, toolbox, ticket, config=CONFIG)
