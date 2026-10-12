"""The single adapter for LLM calls: any OpenAI-compatible chat API.

That covers free options such as Google Gemini (AI Studio) and Groq, plus a local Ollama.
LLM_BASE_URL, LLM_API_KEY and LLM_MODEL in .env choose the provider (see .env.example).
The reason check (P5) uses complete(); the demo agent (P7) uses chat() with tools.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from wardlock.config import Settings, get_settings

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class LLMError(Exception):
    """The LLM could not be reached or gave an unusable answer. Never contains the API key."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # the HTTP status, if the LLM answered at all (429 = rate limited)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]  # {} when the model sent arguments that were not a JSON object
    # Provider data that must be sent back unchanged with this call on the next turn.
    # Gemini puts a "thought_signature" here and answers HTTP 400 without it.
    extra_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class ChatReply:
    content: str  # "" when the model only called tools
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: SecretStr | str,
        model: str,
        *,
        timeout: float = 8.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = _check_base_url(base_url) + "/chat/completions"
        self._key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        if not model:
            raise ValueError("LLM_MODEL must be set")
        self._model = model
        self._http = http or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self._owns_http = http is None

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> LLMClient | None:
        """The configured client, or None when no LLM is set up (callers then fail closed)."""
        s = settings or get_settings()
        if not (s.llm_base_url and s.llm_api_key.get_secret_value() and s.llm_model):
            return None
        return cls(s.llm_base_url, s.llm_api_key, s.llm_model)

    def __repr__(self) -> str:
        return f"LLMClient(url={self._url!r}, model={self._model!r})"  # never the key

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def complete(self, system: str, user: str, *, max_tokens: int = 200) -> str:
        """Send one system + user message and return the reply text."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        message = await self._post({"messages": messages, "max_tokens": max_tokens})
        text = message.get("content")
        if not isinstance(text, str):
            raise LLMError("LLM reply had no message content")
        return text

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, max_tokens: int = 800
    ) -> ChatReply:
        """One turn of a tool-using conversation (OpenAI `tools` format).

        `messages` is the whole conversation so far; the caller appends the reply and the
        tool results. The model's tool arguments are untrusted: the tools validate them.
        """
        body: dict[str, Any] = {"messages": messages, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
        message = await self._post(body)
        content = message.get("content") or ""
        raw_calls = message.get("tool_calls") or []
        if not isinstance(content, str) or not isinstance(raw_calls, list):
            raise LLMError("LLM reply had an unexpected shape")
        calls = [_tool_call(raw, index) for index, raw in enumerate(raw_calls)]
        if not content and not calls:
            raise LLMError("LLM reply had no message content")
        return ChatReply(content, calls)

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        body = {"model": self._model, "temperature": 0, **body}  # temperature 0: the same input, the same answer
        headers = {"Authorization": f"Bearer {self._key.get_secret_value()}"}
        try:
            response = await self._http.post(self._url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM unreachable ({type(exc).__name__})") from None
        if not response.is_success:
            raise LLMError(f"LLM answered HTTP {response.status_code}", status=response.status_code)
        try:
            message = response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("LLM reply had no message content") from None
        if not isinstance(message, dict):
            raise LLMError("LLM reply had no message content")
        return message


def _tool_call(raw: Any, index: int) -> ToolCall:
    try:
        function = raw["function"]
        name = function["name"]
        arguments = function.get("arguments") or "{}"
    except (KeyError, TypeError):
        raise LLMError("LLM reply had a malformed tool call") from None
    if not isinstance(name, str):
        raise LLMError("LLM reply had a malformed tool call")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = {}
    call_id = raw.get("id") if isinstance(raw.get("id"), str) and raw.get("id") else f"call_{index}"
    extra = raw.get("extra_content")
    return ToolCall(call_id, name, arguments if isinstance(arguments, dict) else {},
                    extra if isinstance(extra, dict) else None)


def _check_base_url(base_url: str) -> str:
    """https only, except plain http to this machine (a local Ollama). Keys never travel unencrypted."""
    url = base_url.rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.netloc:
        return url
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
        return url
    raise ValueError("LLM_BASE_URL must be an https URL (or http://localhost for a local model)")
