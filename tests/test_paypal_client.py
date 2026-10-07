"""PayPal client tests. respx fakes PayPal, so no test touches the network."""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest
import respx

from wardlock.paypal.client import LIVE_BASE_URL, PayPalClient, Refund
from wardlock.paypal.errors import (
    PayPalAuthError,
    PayPalRequestError,
    PayPalUnavailable,
    UnsafeBaseURLError,
)

SANDBOX = "https://api-m.sandbox.paypal.com"
SECRET = "shh-test-secret"
CAPTURE = "2GG279541U471931P"
REFUND_PATH = f"/v2/payments/captures/{CAPTURE}/refund"
REQUEST_ID = "0b6f1c9e-6f2a-4c55-9d3e-1f2a3b4c5d6e"

REFUND_BODY = {
    "id": "1JU08902781691411",
    "status": "COMPLETED",
    "amount": {"value": "4.00", "currency_code": "USD"},
    "custom_id": "dec_1",
}


class FakeClock:
    """A clock the test moves by hand, to control token expiry."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_client(sleeps, clock):
    def make(**kwargs):
        async def fake_sleep(seconds):
            sleeps.append(seconds)

        kwargs.setdefault("sleep", fake_sleep)
        kwargs.setdefault("clock", clock)
        return PayPalClient(SANDBOX, "test-client-id", SECRET, **kwargs)

    return make


@pytest.fixture
def paypal():
    """A fake PayPal sandbox with a working token endpoint."""
    with respx.mock(base_url=SANDBOX, assert_all_called=False) as router:
        router.post("/v1/oauth2/token", name="token").respond(
            200, json={"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600}
        )
        yield router


async def refund_4_usd(client, **overrides):
    kwargs = dict(
        capture_id=CAPTURE,
        amount=Decimal("4.00"),
        currency="USD",
        request_id=REQUEST_ID,
        note="Sorry about the bell",
        custom_id="dec_1",
    )
    kwargs.update(overrides)
    return await client.refund_capture(**kwargs)


def request_ids(route):
    return [call.request.headers.get("PayPal-Request-Id") for call in route.calls]


# ------------------------------------------------------------ sandbox guard (invariant 8)


def test_refuses_live_url_without_allow_live():
    with pytest.raises(UnsafeBaseURLError):
        PayPalClient(LIVE_BASE_URL, "id", SECRET)


@pytest.mark.parametrize("url", ["https://evil.example.com", "http://api-m.sandbox.paypal.com", ""])
def test_refuses_unknown_urls_even_with_allow_live(url):
    with pytest.raises(UnsafeBaseURLError):
        PayPalClient(url, "id", SECRET, allow_live=True)


async def test_accepts_sandbox_url_with_trailing_slash():
    async with PayPalClient(SANDBOX + "/", "id", SECRET) as client:
        assert repr(client) == f"PayPalClient(base_url={SANDBOX!r})"


async def test_live_url_needs_explicit_allow_live():
    async with PayPalClient(LIVE_BASE_URL, "id", SECRET, allow_live=True):
        pass


def test_requires_credentials():
    with pytest.raises(ValueError):
        PayPalClient(SANDBOX, "", SECRET)
    with pytest.raises(ValueError):
        PayPalClient(SANDBOX, "id", "")


# ------------------------------------------------------------ token caching


async def test_token_is_fetched_once_and_reused(paypal, make_client):
    paypal.get(f"/v2/payments/captures/{CAPTURE}").respond(200, json={"id": CAPTURE})
    async with make_client() as client:
        await client.get_capture(CAPTURE)
        await client.get_capture(CAPTURE)

    assert paypal["token"].call_count == 1
    token_request = paypal["token"].calls[0].request
    assert token_request.headers["Authorization"].startswith("Basic ")
    assert token_request.content == b"grant_type=client_credentials"


async def test_token_is_refreshed_60s_before_expiry(paypal, make_client, clock):
    paypal.get(f"/v2/payments/captures/{CAPTURE}").respond(200, json={"id": CAPTURE})
    async with make_client() as client:
        await client.get_capture(CAPTURE)  # token valid for 3600 s, used for 3540 s
        clock.now += 3539
        await client.get_capture(CAPTURE)
        assert paypal["token"].call_count == 1
        clock.now += 1
        await client.get_capture(CAPTURE)
        assert paypal["token"].call_count == 2


async def test_concurrent_calls_share_one_token_fetch(paypal, make_client):
    async def slow_token(_request):
        # Yield to the event loop like a real network call. Without this, the fake
        # answers instantly and the calls never actually overlap.
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

    paypal["token"].mock(side_effect=slow_token)
    paypal.get(f"/v2/payments/captures/{CAPTURE}").respond(200, json={"id": CAPTURE})
    async with make_client() as client:
        await asyncio.gather(*(client.get_capture(CAPTURE) for _ in range(5)))
    assert paypal["token"].call_count == 1


# ------------------------------------------------------------ refunds: body and headers


async def test_refund_body_always_has_explicit_amount(paypal, make_client):
    """T10: an empty body would be a FULL refund, so amount must always be sent."""
    route = paypal.post(REFUND_PATH).respond(201, json=REFUND_BODY)
    async with make_client() as client:
        refund = await refund_4_usd(client)

    request = route.calls[0].request
    assert json.loads(request.content) == {
        "amount": {"value": "4.00", "currency_code": "USD"},
        "note_to_payer": "Sorry about the bell",
        "custom_id": "dec_1",
    }
    assert request.headers["PayPal-Request-Id"] == REQUEST_ID
    assert request.headers["Prefer"] == "return=representation"
    assert request.headers["Authorization"] == "Bearer tok-1"
    assert refund == Refund(
        id="1JU08902781691411",
        status="COMPLETED",
        amount=Decimal("4.00"),
        currency="USD",
        custom_id="dec_1",
        raw=REFUND_BODY,
    )


async def test_refund_without_optional_fields_still_sends_amount(paypal, make_client):
    route = paypal.post(REFUND_PATH).respond(201, json=REFUND_BODY)
    async with make_client() as client:
        await refund_4_usd(client, note=None, custom_id=None)
    assert json.loads(route.calls[0].request.content) == {
        "amount": {"value": "4.00", "currency_code": "USD"}
    }


async def test_zero_decimal_currency_is_sent_without_decimals(paypal, make_client):
    jpy_body = {**REFUND_BODY, "amount": {"value": "500", "currency_code": "JPY"}}
    route = paypal.post(REFUND_PATH).respond(201, json=jpy_body)
    async with make_client() as client:
        await refund_4_usd(client, amount=Decimal("500"), currency="JPY")
    assert json.loads(route.calls[0].request.content)["amount"] == {
        "value": "500",
        "currency_code": "JPY",
    }


async def test_replayed_request_id_returning_200_is_success(paypal, make_client):
    # PayPal answers 200 (not 201) with the EXISTING refund when a request id is reused.
    paypal.post(REFUND_PATH).respond(200, json=REFUND_BODY)
    async with make_client() as client:
        refund = await refund_4_usd(client)
    assert refund.id == "1JU08902781691411"


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": 4.0},  # float (invariant 3)
        {"amount": Decimal("0")},
        {"amount": Decimal("-4.00")},
        {"amount": Decimal("4.001")},
        {"amount": Decimal("NaN")},
        {"currency": "usd"},
        {"capture_id": "../../v1/oauth2/token"},  # path trick
        {"capture_id": ""},
        {"capture_id": "ABC/DEF"},
        {"request_id": ""},
        {"request_id": "has space"},
        {"request_id": "x" * 65},
        {"note": ""},
        {"note": "x" * 256},
        {"note": "line one\nline two"},
        {"custom_id": "x" * 128},
    ],
)
async def test_invalid_refund_inputs_never_reach_paypal(paypal, make_client, overrides):
    async with make_client() as client:
        with pytest.raises(ValueError):
            await refund_4_usd(client, **overrides)
    assert paypal.calls.call_count == 0  # not even a token request


# ------------------------------------------------------------ retries (same PayPal-Request-Id)


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError])
async def test_network_failures_retry_3_times_with_same_request_id(paypal, make_client, sleeps, error):
    """T9: PayPal times out 4 times -> 3 retries with the same PayPal-Request-Id, then give up."""
    route = paypal.post(REFUND_PATH).mock(side_effect=error)
    async with make_client() as client:
        with pytest.raises(PayPalUnavailable) as caught:
            await refund_4_usd(client)

    assert route.call_count == 4
    assert request_ids(route) == [REQUEST_ID] * 4
    assert sleeps == [0.5, 1.0, 2.0]
    assert caught.value.attempts == 4
    assert caught.value.request_id == REQUEST_ID


async def test_server_errors_are_retried_until_success(paypal, make_client, sleeps):
    route = paypal.post(REFUND_PATH).mock(
        side_effect=[httpx.Response(503), httpx.Response(500), httpx.Response(201, json=REFUND_BODY)]
    )
    async with make_client() as client:
        refund = await refund_4_usd(client)

    assert refund.status == "COMPLETED"
    assert request_ids(route) == [REQUEST_ID] * 3
    assert sleeps == [0.5, 1.0]


@pytest.mark.parametrize("status", [409, 429])
async def test_in_progress_and_rate_limit_are_retried(paypal, make_client, status):
    # 409 means PayPal is still processing this request. Treating it as a final "no"
    # could lead to a second refund under a new request id, so it must be retried.
    route = paypal.post(REFUND_PATH).mock(
        side_effect=[httpx.Response(status), httpx.Response(201, json=REFUND_BODY)]
    )
    async with make_client() as client:
        await refund_4_usd(client)
    assert request_ids(route) == [REQUEST_ID] * 2


async def test_persistent_409_becomes_unavailable_not_a_rejection(paypal, make_client):
    paypal.post(REFUND_PATH).respond(409, json={"name": "RESOURCE_CONFLICT"})
    async with make_client() as client:
        with pytest.raises(PayPalUnavailable):
            await refund_4_usd(client)


async def test_token_endpoint_outage_is_retried(paypal, make_client):
    paypal["token"].mock(
        side_effect=[
            httpx.ConnectError("down"),
            httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600}),
        ]
    )
    paypal.post(REFUND_PATH).respond(201, json=REFUND_BODY)
    async with make_client() as client:
        await refund_4_usd(client)
    assert paypal["token"].call_count == 2


# ------------------------------------------------------------ 4xx mapping


async def test_422_becomes_request_error_without_retry(paypal, make_client, sleeps):
    route = paypal.post(REFUND_PATH).respond(
        422,
        json={
            "name": "UNPROCESSABLE_ENTITY",
            "message": "The requested action could not be performed.",
            "debug_id": "f3a1b2c4d5e6",
            "details": [{"issue": "REFUND_AMOUNT_EXCEEDED", "description": "..."}],
        },
    )
    async with make_client() as client:
        with pytest.raises(PayPalRequestError) as caught:
            await refund_4_usd(client)

    error = caught.value
    assert (error.status_code, error.name, error.issues, error.debug_id) == (
        422,
        "UNPROCESSABLE_ENTITY",
        ["REFUND_AMOUNT_EXCEEDED"],
        "f3a1b2c4d5e6",
    )
    assert route.call_count == 1
    assert sleeps == []


async def test_404_on_read_becomes_request_error(paypal, make_client):
    paypal.get(f"/v2/payments/captures/{CAPTURE}").respond(
        404, text="not json", headers={"PayPal-Debug-Id": "dbg404"}
    )
    async with make_client() as client:
        with pytest.raises(PayPalRequestError) as caught:
            await client.get_capture(CAPTURE)
    assert (caught.value.status_code, caught.value.debug_id) == (404, "dbg404")


# ------------------------------------------------------------ auth errors


async def test_401_refreshes_the_token_once_and_resends(paypal, make_client):
    paypal["token"].mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok-2", "expires_in": 3600}),
        ]
    )
    route = paypal.post(REFUND_PATH).mock(
        side_effect=[httpx.Response(401), httpx.Response(201, json=REFUND_BODY)]
    )
    async with make_client() as client:
        await refund_4_usd(client)

    sent_tokens = [call.request.headers["Authorization"] for call in route.calls]
    assert sent_tokens == ["Bearer tok-1", "Bearer tok-2"]
    assert request_ids(route) == [REQUEST_ID] * 2


async def test_persistent_401_raises_auth_error(paypal, make_client):
    route = paypal.post(REFUND_PATH).respond(401, json={"name": "AUTHENTICATION_FAILURE"})
    async with make_client() as client:
        with pytest.raises(PayPalAuthError):
            await refund_4_usd(client)
    assert route.call_count == 2


async def test_403_raises_auth_error_immediately(paypal, make_client):
    route = paypal.post(REFUND_PATH).respond(403, json={"name": "NOT_AUTHORIZED"})
    async with make_client() as client:
        with pytest.raises(PayPalAuthError):
            await refund_4_usd(client)
    assert route.call_count == 1


async def test_rejected_credentials_raise_auth_error_without_leaking_secret(paypal, make_client):
    paypal["token"].respond(
        401, json={"error": "invalid_client", "error_description": "Client Authentication failed"}
    )
    async with make_client() as client:
        with pytest.raises(PayPalAuthError) as caught:
            await refund_4_usd(client)
        assert SECRET not in repr(client)
    assert "invalid_client" in str(caught.value)
    assert SECRET not in str(caught.value)


# ------------------------------------------------------------ unknown outcomes (fail closed)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(201, text="<html>oops</html>"),
        httpx.Response(201, json=["not", "an", "object"]),
        httpx.Response(201, json={"status": "COMPLETED"}),  # no refund id
        httpx.Response(201, json={**REFUND_BODY, "amount": {"value": "abc"}}),
        httpx.Response(302, headers={"Location": "https://example.com"}),
        # PayPal answered for a different amount or currency, or gave no amount: a reused
        # request id can do this, and then we cannot know what really happened.
        httpx.Response(201, json={**REFUND_BODY, "amount": {"value": "40.00", "currency_code": "USD"}}),
        httpx.Response(201, json={**REFUND_BODY, "amount": {"value": "4.00", "currency_code": "EUR"}}),
        httpx.Response(201, json={k: v for k, v in REFUND_BODY.items() if k != "amount"}),
    ],
)
async def test_unreadable_refund_response_is_unknown_outcome(paypal, make_client, response):
    paypal.post(REFUND_PATH).mock(return_value=response)
    async with make_client() as client:
        with pytest.raises(PayPalUnavailable) as caught:
            await refund_4_usd(client)
    assert caught.value.request_id == REQUEST_ID


# ------------------------------------------------------------ reads


async def test_get_refund_parses_the_refund(paypal, make_client):
    paypal.get("/v2/payments/refunds/1JU08902781691411").respond(200, json=REFUND_BODY)
    async with make_client() as client:
        refund = await client.get_refund("1JU08902781691411")
    assert (refund.status, refund.amount, refund.currency) == ("COMPLETED", Decimal("4.00"), "USD")


async def test_get_order_returns_raw_json(paypal, make_client):
    paypal.get("/v2/checkout/orders/5O190127TN364715T").respond(200, json={"id": "5O190127TN364715T"})
    async with make_client() as client:
        assert await client.get_order("5O190127TN364715T") == {"id": "5O190127TN364715T"}


# ------------------------------------------------------------ seeding-only calls


async def test_create_order_sends_body_and_request_id(paypal, make_client):
    route = paypal.post("/v2/checkout/orders").respond(201, json={"id": "5O190127TN364715T"})
    body = {"intent": "CAPTURE", "purchase_units": [{"amount": {"currency_code": "USD", "value": "1.00"}}]}
    async with make_client() as client:
        result = await client.create_order(body, "seed-A-1001-abcd1234-1")

    assert result == {"id": "5O190127TN364715T"}
    request = route.calls[0].request
    assert json.loads(request.content) == body
    assert request.headers["PayPal-Request-Id"] == "seed-A-1001-abcd1234-1"


@pytest.mark.parametrize("body", [{"intent": "AUTHORIZE"}, {}, None])
async def test_create_order_only_allows_capture_intent(paypal, make_client, body):
    async with make_client() as client:
        with pytest.raises(ValueError):
            await client.create_order(body, "seed-x")
    assert paypal.calls.call_count == 0


async def test_capture_order_sends_empty_json_and_headers(paypal, make_client):
    route = paypal.post("/v2/checkout/orders/5O190127TN364715T/capture").respond(201, json={"id": "X"})
    async with make_client() as client:
        await client.capture_order("5O190127TN364715T", "seed-capture-5O190127TN364715T")

    request = route.calls[0].request
    assert request.content == b"{}"
    assert request.headers["PayPal-Request-Id"] == "seed-capture-5O190127TN364715T"
    assert request.headers["Prefer"] == "return=representation"


async def test_capture_order_validates_ids_before_calling(paypal, make_client):
    async with make_client() as client:
        with pytest.raises(ValueError):
            await client.capture_order("../refund", "seed-x")
        with pytest.raises(ValueError):
            await client.capture_order("5O190127TN364715T", "")
    assert paypal.calls.call_count == 0
