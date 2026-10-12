"""The ONLY PayPal HTTP client (SPEC §6–7, SPEC §0 invariant 1).

Nothing else in Wardlock may call PayPal. This module:
- refuses every base URL except the sandbox unless ALLOW_LIVE is set (invariant 8);
- caches the OAuth token until shortly before it expires;
- always sends an explicit refund amount and a PayPal-Request-Id (invariant 4);
- retries only what PayPal documents as safe to retry, always with the SAME request id;
- raises the typed errors in errors.py so the executor can BLOCK or HOLD (invariant 5).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import SecretStr

from wardlock.config import SANDBOX_BASE_URL, Settings, get_settings
from wardlock.money import to_paypal_value
from wardlock.paypal.errors import (
    PayPalAuthError,
    PayPalRequestError,
    PayPalUnavailable,
    UnsafeBaseURLError,
)

logger = logging.getLogger(__name__)

LIVE_BASE_URL = "https://api-m.paypal.com"

# PayPal IDs are short alphanumeric strings such as "2GG279541U471931P". Checking the shape
# before building a URL stops a crafted id like "../../v1/..." from changing which endpoint
# we call. This matters because the compat tool passes an agent-supplied capture id through.
_ID_PATTERN = re.compile(r"[A-Za-z0-9-]{1,64}")
# We generate uuid4 strings; allow any printable ASCII without spaces (no header injection).
_REQUEST_ID_PATTERN = re.compile(r"[!-~]{1,64}")

# Field limits from PayPal's OpenAPI spec (refund_request), checked Oct 2026.
NOTE_MAX_LENGTH = 255
CUSTOM_ID_MAX_LENGTH = 127

# Refresh the token this many seconds before PayPal says it expires, so a token never
# runs out halfway through a request.
TOKEN_EXPIRY_MARGIN_SECONDS = 60

DEFAULT_BACKOFF_SECONDS = (0.5, 1.0, 2.0)


@dataclass(frozen=True)
class Refund:
    """The parts of PayPal's refund resource that Wardlock uses."""

    id: str
    status: str  # COMPLETED | PENDING | FAILED | CANCELLED
    amount: Decimal | None
    currency: str | None
    custom_id: str | None
    raw: dict[str, Any] = field(repr=False)


class PayPalClient:
    """Async client for the handful of PayPal REST calls Wardlock needs.

    Use it as an async context manager so the HTTP connection pool is closed:

        async with PayPalClient.from_settings() as paypal:
            refund = await paypal.refund_capture(...)
    """

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: SecretStr | str,
        *,
        allow_live: bool = False,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff: Sequence[float] = DEFAULT_BACKOFF_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        # The guard runs first, so an unsafe URL never gets as far as opening a connection.
        self._base_url = _check_base_url(base_url, allow_live)
        secret = client_secret if isinstance(client_secret, SecretStr) else SecretStr(client_secret)
        if not client_id or not secret.get_secret_value():
            raise ValueError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set (see .env.example)")
        if max_retries < 0 or not backoff:
            raise ValueError("max_retries must be >= 0 and backoff must not be empty")

        self._client_id = client_id
        self._client_secret = secret
        self._max_retries = max_retries
        self._backoff = tuple(backoff)
        # sleep and clock are injectable so tests run instantly and control token expiry.
        self._sleep = sleep
        self._clock = clock
        # Never follow redirects: one could carry our bearer token to another host.
        self._http = http or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self._owns_http = http is None

        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **kwargs: Any) -> PayPalClient:
        """Build a client from environment settings (see .env.example)."""
        s = settings or get_settings()
        return cls(
            s.paypal_base_url,
            s.paypal_client_id,
            s.paypal_client_secret,
            allow_live=s.allow_live,
            **kwargs,
        )

    @property
    def base_url(self) -> str:
        """The PayPal API base URL this client talks to (already passed the sandbox guard)."""
        return self._base_url

    def __repr__(self) -> str:
        # Deliberately excludes the client id, secret and token.
        return f"PayPalClient(base_url={self._base_url!r})"

    async def __aenter__(self) -> PayPalClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ---------------------------------------------------------------- reads

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v2/checkout/orders/{_check_id(order_id, 'order_id')}")

    async def get_capture(self, capture_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v2/payments/captures/{_check_id(capture_id, 'capture_id')}")

    async def get_refund(self, refund_id: str) -> Refund:
        data = await self._request("GET", f"/v2/payments/refunds/{_check_id(refund_id, 'refund_id')}")
        return _parse_refund(data, request_id=None)

    # ---------------------------------------------------------------- refunds

    async def refund_capture(
        self,
        capture_id: str,
        amount: Decimal,
        currency: str,
        request_id: str,
        note: str | None = None,
        custom_id: str | None = None,
    ) -> Refund:
        """Refund exactly `amount` of a capture. Every input is validated before any HTTP call.

        The body ALWAYS contains `amount`, because PayPal treats an empty body as a
        FULL refund (invariant 4). `request_id` must already be saved by the caller, so a
        retry or a later hold approval reuses it and PayPal cannot refund twice.
        """
        path = f"/v2/payments/captures/{_check_id(capture_id, 'capture_id')}/refund"
        body: dict[str, Any] = {
            "amount": {"value": to_paypal_value(amount, currency), "currency_code": currency},
        }
        if note is not None:
            body["note_to_payer"] = _check_text(note, "note", NOTE_MAX_LENGTH)
        if custom_id is not None:
            body["custom_id"] = _check_text(custom_id, "custom_id", CUSTOM_ID_MAX_LENGTH)
        _check_request_id(request_id)

        # VERIFY: PayPal does not document how long it keeps refund PayPal-Request-Ids.
        # Our holds time out after minutes, which should be far inside that window.
        headers = {"PayPal-Request-Id": request_id, "Prefer": "return=representation"}
        data = await self._request("POST", path, json=body, headers=headers, request_id=request_id)
        refund = _parse_refund(data, request_id=request_id)
        # A reused request id makes PayPal return the ORIGINAL refund, whatever we sent this
        # time. If the amount differs, our request id bookkeeping is broken. We can't trust
        # what happened, so we fail closed and a human checks it.
        if refund.amount != amount or refund.currency != currency:
            raise PayPalUnavailable(
                f"refund {refund.id} is for {refund.amount} {refund.currency}, "
                f"but {amount} {currency} was requested",
                None,
                request_id,
            )
        logger.info("PayPal refund %s status=%s request_id=%s", refund.id, refund.status, request_id)
        return refund

    # ---------------------------------------------------------------- seeding only

    async def create_order(self, body: dict[str, Any], request_id: str) -> dict[str, Any]:
        """Create a checkout order. Only sandbox seeding (wardlock/seed.py) uses this;
        nothing on the agent's path ever creates orders."""
        if not isinstance(body, dict) or body.get("intent") != "CAPTURE":
            raise ValueError("create_order only supports intent CAPTURE")
        _check_request_id(request_id)
        # PayPal keeps create-order request ids for 6 hours (OpenAPI spec). The id is
        # mandatory when the body carries a card, because then this one call takes the payment.
        headers = {"PayPal-Request-Id": request_id}
        return await self._request(
            "POST", "/v2/checkout/orders", json=body, headers=headers, request_id=request_id
        )

    async def capture_order(self, order_id: str, request_id: str) -> dict[str, Any]:
        """Capture an approved order.

        A timeout can still have captured the money, so retries reuse request_id. PayPal
        then returns the original result instead of capturing twice.
        """
        path = f"/v2/checkout/orders/{_check_id(order_id, 'order_id')}/capture"
        _check_request_id(request_id)
        headers = {"PayPal-Request-Id": request_id, "Prefer": "return=representation"}
        return await self._request("POST", path, json={}, headers=headers, request_id=request_id)

    # ---------------------------------------------------------------- plumbing

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Send one API call with auth, retries and error mapping, and return its JSON body."""
        url = self._base_url + path
        label = f"{method} {path}"
        used: dict[str, str] = {}  # the token the latest attempt sent

        async def send() -> httpx.Response:
            token = await self._get_token()
            used["token"] = token
            all_headers = {**(headers or {}), "Authorization": f"Bearer {token}"}
            return await self._http.request(method, url, json=json, headers=all_headers)

        response = await self._with_retries(send, label, request_id)
        if response.status_code == 401:
            # The token expired or was revoked early. PayPal rejected the call before
            # processing it, so fetching a new token and resending once is safe.
            self._forget_token(used.get("token"))
            response = await self._with_retries(send, label, request_id)

        status = response.status_code
        if response.is_success:
            return _read_json(response, label, request_id)
        if status in (401, 403):
            raise PayPalAuthError(status, _error_summary(response))
        if 400 <= status < 500:
            raise _to_request_error(response)
        # Anything else (1xx/3xx) is unexpected, so treat the outcome as unknown: fail closed.
        raise PayPalUnavailable(f"unexpected HTTP {status} from {label}", None, request_id)

    async def _with_retries(
        self,
        send: Callable[[], Awaitable[httpx.Response]],
        label: str,
        request_id: str | None,
    ) -> httpx.Response:
        """Call send() and retry timeouts, network errors, 5xx, 409 and 429.

        PayPal documents these as safe to retry with the same PayPal-Request-Id.
        Returns the first response that is not retryable. Raises PayPalUnavailable
        once 1 + max_retries attempts have all failed.
        """
        failures = 0
        while True:
            try:
                response = await send()
            except httpx.TransportError as exc:  # includes every kind of timeout
                reason = f"network error ({type(exc).__name__})"
            else:
                if not _is_retryable(response.status_code):
                    return response
                reason = f"HTTP {response.status_code}"

            failures += 1
            if failures > self._max_retries:
                raise PayPalUnavailable(f"{label}: {reason}", failures, request_id)
            delay = self._backoff[min(failures, len(self._backoff)) - 1]
            logger.warning(
                "%s failed (%s); retry %d/%d in %.1fs request_id=%s",
                label, reason, failures, self._max_retries, delay, request_id,
            )
            await self._sleep(delay)

    async def access_token(self) -> str:
        """A current OAuth access token. Only for the demo's config A (SPEC §14), which hands it
        to PayPal's own MCP server as the "no Wardlock" baseline. Wardlock never uses it."""
        return await self._get_token()

    async def _get_token(self) -> str:
        # The lock makes concurrent calls share a single token fetch instead of each fetching one.
        async with self._token_lock:
            if self._token is None or self._clock() >= self._token_expires_at:
                await self._fetch_token()
            assert self._token is not None
            return self._token

    def _forget_token(self, stale_token: str | None) -> None:
        # Only drop the token if it is the one that failed. Another request may already
        # have replaced it with a fresh one.
        if stale_token is not None and self._token == stale_token:
            self._token = None

    async def _fetch_token(self) -> None:
        url = self._base_url + "/v1/oauth2/token"

        async def send() -> httpx.Response:
            return await self._http.post(
                url,
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret.get_secret_value()),
            )

        response = await self._with_retries(send, "POST /v1/oauth2/token", request_id=None)
        if not response.is_success:
            # e.g. 401 {"error": "invalid_client"}: the credentials in .env are wrong.
            raise PayPalAuthError(response.status_code, _error_summary(response))
        try:
            body = response.json()
            token = body["access_token"]
            expires_in = int(body["expires_in"])
        except (ValueError, KeyError, TypeError):
            raise PayPalUnavailable("token response was unreadable", None, None) from None
        if not isinstance(token, str) or not token:
            raise PayPalUnavailable("token response had no access_token", None, None)

        self._token = token
        self._token_expires_at = self._clock() + max(expires_in - TOKEN_EXPIRY_MARGIN_SECONDS, 0)


# -------------------------------------------------------------------- helpers


def _check_base_url(base_url: str, allow_live: bool) -> str:
    url = base_url.rstrip("/")
    if url == SANDBOX_BASE_URL:
        return url
    if url == LIVE_BASE_URL and allow_live:
        logger.warning("ALLOW_LIVE is set: this client can move REAL money")
        return url
    raise UnsafeBaseURLError(
        f"refusing PayPal base URL {base_url!r}: only {SANDBOX_BASE_URL} is allowed "
        "(or the live URL with ALLOW_LIVE=1)"
    )


def _check_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        # The bad value is not echoed: it may be agent-controlled text.
        raise ValueError(f"invalid {label}: expected 1-64 letters, digits or '-'")
    return value


def _check_request_id(value: str) -> None:
    if not isinstance(value, str) or not _REQUEST_ID_PATTERN.fullmatch(value):
        raise ValueError("invalid request_id: expected 1-64 printable characters without spaces")


def _check_text(value: str, label: str, max_length: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= max_length:
        raise ValueError(f"{label} must be 1-{max_length} characters")
    # VERIFY: PayPal's pattern for these fields is ^.*$, which rejects line breaks.
    # isprintable() also rejects tabs and other control characters.
    if not value.isprintable():
        raise ValueError(f"{label} must be a single line without control characters")
    return value


def _is_retryable(status: int) -> bool:
    return status in (409, 429) or status >= 500


def _read_json(response: httpx.Response, label: str, request_id: str | None) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        # A 2xx we cannot read: for a refund it probably went through, so the outcome is unknown.
        raise PayPalUnavailable(f"unreadable success response from {label}", None, request_id)
    return data


def _parse_refund(data: dict[str, Any], request_id: str | None) -> Refund:
    try:
        refund_id = data["id"]
        status = data["status"]
        if not isinstance(refund_id, str) or not isinstance(status, str):
            raise TypeError
        money = data.get("amount") or {}
        value = money.get("value")
        amount = Decimal(value) if value is not None else None
    except (KeyError, TypeError, AttributeError, InvalidOperation):
        raise PayPalUnavailable("refund response was missing id, status or amount", None, request_id) from None
    return Refund(
        id=refund_id,
        status=status,
        amount=amount,
        currency=money.get("currency_code"),
        custom_id=data.get("custom_id"),
        raw=data,
    )


def _error_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_summary(response: httpx.Response) -> str:
    """Short, secret-free description of an error response, for logs and auth errors."""
    body = _error_body(response)
    # REST errors use "name"; OAuth errors use "error" (e.g. "invalid_client").
    return str(body.get("name") or body.get("error") or f"HTTP {response.status_code}")


def _to_request_error(response: httpx.Response) -> PayPalRequestError:
    body = _error_body(response)
    details = body.get("details")
    issues = [
        str(d["issue"]) for d in details if isinstance(d, dict) and d.get("issue")
    ] if isinstance(details, list) else []
    return PayPalRequestError(
        status_code=response.status_code,
        name=str(body.get("name") or f"HTTP_{response.status_code}"),
        message=str(body.get("message") or ""),
        issues=issues,
        debug_id=body.get("debug_id") or response.headers.get("paypal-debug-id"),
    )
