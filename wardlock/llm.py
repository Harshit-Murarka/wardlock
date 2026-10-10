"""The single adapter for LLM calls: any OpenAI-compatible chat API.

That covers free options such as Google Gemini (AI Studio) and Groq, plus a local Ollama.
LLM_BASE_URL, LLM_API_KEY and LLM_MODEL in .env choose the provider (see .env.example).
The reason check (P5) uses it now, and the demo agent (P7) will too.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from wardlock.config import Settings, get_settings

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class LLMError(Exception):
    """The LLM could not be reached or gave an unusable answer. Never contains the API key."""


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
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,  # the same ticket should get the same verdict
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {self._key.get_secret_value()}"}
        try:
            response = await self._http.post(self._url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM unreachable ({type(exc).__name__})") from None
        if not response.is_success:
            raise LLMError(f"LLM answered HTTP {response.status_code}")
        try:
            text = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("LLM reply had no message content") from None
        if not isinstance(text, str):
            raise LLMError("LLM reply had no message content")
        return text


def _check_base_url(base_url: str) -> str:
    """https only, except plain http to this machine (a local Ollama). Keys never travel unencrypted."""
    url = base_url.rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.netloc:
        return url
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
        return url
    raise ValueError("LLM_BASE_URL must be an https URL (or http://localhost for a local model)")
