"""Typed errors raised by the PayPal client.

The executor (P5) maps each one to a verdict (SPEC §6):
    PayPalRequestError -> BLOCK  PayPal refused the request; no money moved.
    PayPalAuthError    -> HOLD   Our credentials or permissions are wrong, not the request.
    PayPalUnavailable  -> HOLD   PayPal was unreachable, or a refund's outcome is unknown.

Messages never include credentials, tokens or request bodies (SPEC §0 invariant 9).
"""


class PayPalError(Exception):
    """Base class for every error the PayPal client raises on purpose."""


class UnsafeBaseURLError(PayPalError, ValueError):
    """The configured base URL is not the PayPal sandbox (SPEC §0 invariant 8)."""


class PayPalRequestError(PayPalError):
    """PayPal rejected the request with a 4xx that retrying cannot fix (400, 404, 422, ...)."""

    def __init__(
        self,
        status_code: int,
        name: str,
        message: str,
        issues: list[str],
        debug_id: str | None,
    ) -> None:
        self.status_code = status_code
        self.name = name  # e.g. "UNPROCESSABLE_ENTITY"
        self.message = message
        self.issues = issues  # e.g. ["REFUND_AMOUNT_EXCEEDED"]
        self.debug_id = debug_id  # quote this when asking PayPal support
        super().__init__(f"PayPal {status_code} {name}: {message} issues={issues} debug_id={debug_id}")


class PayPalAuthError(PayPalError):
    """PayPal rejected our credentials or permissions (401/403, or the token request failed)."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"PayPal authentication failed (HTTP {status_code}): {detail}")


class PayPalUnavailable(PayPalError):
    """PayPal gave no usable answer, even after retries.

    For a refund the outcome is UNKNOWN: PayPal may have processed it. The caller must
    HOLD and later retry with the SAME request_id, never a new one, so it cannot
    refund twice.
    """

    def __init__(self, reason: str, attempts: int | None, request_id: str | None) -> None:
        self.reason = reason
        self.attempts = attempts
        self.request_id = request_id
        tries = f" after {attempts} attempt(s)" if attempts else ""
        super().__init__(f"PayPal unavailable{tries}: {reason}")
