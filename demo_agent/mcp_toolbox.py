"""Expose an MCP client session's tools to the agent loop."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from mcp.client.session import ClientSession

from demo_agent.agent_loop import ToolResult, to_openai_tool


class McpToolbox:
    """The tools of one MCP server, optionally limited to `only`."""

    def __init__(self, session: ClientSession, tools: list[dict[str, Any]]) -> None:
        self._session = session
        self.tools = tools

    @classmethod
    async def load(cls, session: ClientSession, *, only: Iterable[str] | None = None) -> McpToolbox:
        wanted = set(only) if only is not None else None
        listed = (await session.list_tools()).tools
        tools = [to_openai_tool(t.name, t.description, t.input_schema) for t in listed
                 if wanted is None or t.name in wanted]
        if wanted is not None and {t["function"]["name"] for t in tools} != wanted:
            raise RuntimeError(f"the MCP server does not offer all of {sorted(wanted)}")
        return cls(session, tools)

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        result = await self._session.call_tool(name, arguments)
        text = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
        return ToolResult(text or "(no output)", bool(result.is_error))
