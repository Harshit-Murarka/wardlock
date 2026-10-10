"""The OpenAI-compatible LLM adapter, against a fake endpoint (respx)."""

import json

import httpx
import pytest
import respx

from wardlock.config import Settings
from wardlock.llm import LLMClient, LLMError

BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
KEY = "test-llm-key-123"


def reply(content):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


async def test_the_request_is_an_openai_style_chat_completion():
    with respx.mock() as router:
        route = router.post(URL).mock(return_value=reply('{"consistent": true, "rationale": "ok"}'))
        client = LLMClient(BASE, KEY, "some-free-model")
        text = await client.complete("system words", "user words")
        await client.aclose()

    assert text == '{"consistent": true, "rationale": "ok"}'
    request = route.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "some-free-model" and body["temperature"] == 0
    assert body["messages"] == [{"role": "system", "content": "system words"},
                                {"role": "user", "content": "user words"}]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, json={"error": "rate limited"}),
        httpx.Response(500),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
    ],
)
async def test_bad_answers_raise_llm_error_without_the_key(response):
    with respx.mock() as router:
        router.post(URL).mock(return_value=response)
        client = LLMClient(BASE, KEY, "m")
        with pytest.raises(LLMError) as caught:
            await client.complete("s", "u")
        await client.aclose()
    assert KEY not in str(caught.value)


async def test_a_network_error_raises_llm_error():
    with respx.mock() as router:
        router.post(URL).mock(side_effect=httpx.ConnectError("down"))
        client = LLMClient(BASE, KEY, "m")
        with pytest.raises(LLMError):
            await client.complete("s", "u")
        await client.aclose()


@pytest.mark.parametrize("url", ["http://api.groq.com/openai/v1", "ftp://x", "", "groq.com"])
def test_keys_never_travel_unencrypted(url):
    with pytest.raises(ValueError):
        LLMClient(url, KEY, "m")


async def test_a_local_model_over_plain_http_is_allowed():
    client = LLMClient("http://localhost:11434/v1", KEY, "llama")
    assert KEY not in repr(client)
    await client.aclose()


def test_from_settings_returns_none_until_everything_is_set():
    assert LLMClient.from_settings(Settings(_env_file=None)) is None
    assert LLMClient.from_settings(Settings(_env_file=None, llm_base_url=BASE, llm_api_key=KEY)) is None  # no model
