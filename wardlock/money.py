"""Money helpers shared by the PayPal client and (from P3) the handle code.

Amounts are always Decimal (or integer minor units), never float (SPEC §0 invariant 3).
These helpers REJECT a value they cannot represent exactly instead of rounding it,
because silently rounding would change how much money moves.
"""

from decimal import Decimal, InvalidOperation

# PayPal's supported currency codes (developer.paypal.com/reference/currency-codes, checked Oct 2026).
SUPPORTED_CURRENCIES = frozenset({
    "AUD", "BRL", "CAD", "CNY", "CZK", "DKK", "EUR", "HKD", "HUF", "JPY", "MYR", "MXN",
    "TWD", "NZD", "NOK", "PHP", "PLN", "GBP", "SGD", "SEK", "CHF", "THB", "USD",
})

# PayPal lists these as "zero-digit" currencies: no decimal places allowed.
ZERO_DECIMAL_CURRENCIES = frozenset({"HUF", "JPY", "TWD"})

# money.value maxLength in PayPal's OpenAPI spec.
_MAX_VALUE_LENGTH = 32


def currency_exponent(currency: str) -> int:
    """Number of decimal places PayPal accepts for this currency: 0 or 2."""
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError(f"unsupported currency: {currency!r}")
    return 0 if currency in ZERO_DECIMAL_CURRENCIES else 2


def to_paypal_value(amount: Decimal, currency: str) -> str:
    """Format a positive amount as PayPal's money.value string, e.g. Decimal("4") -> "4.00".

    Raises ValueError instead of rounding: Decimal("4.001") in USD is an error, not "4.00".
    """
    exponent = currency_exponent(currency)
    # Only Decimal is accepted: a float may already carry a binary rounding error,
    # and int/str inputs usually mean the caller skipped the amount computation.
    if not isinstance(amount, Decimal):
        raise ValueError(f"amount must be a Decimal, got {type(amount).__name__}")
    if not amount.is_finite():
        raise ValueError("amount must be a finite number")
    if amount <= 0:
        raise ValueError("amount must be greater than zero")

    quantum = Decimal(1).scaleb(-exponent)  # Decimal("0.01") for USD, Decimal("1") for JPY
    try:
        exact = amount.quantize(quantum)
    except InvalidOperation:  # too many digits for Decimal's default precision
        raise ValueError("amount is too large") from None
    if exact != amount:
        raise ValueError(f"amount has more than {exponent} decimal places for {currency}")

    value = format(exact, "f")
    if len(value) > _MAX_VALUE_LENGTH:
        raise ValueError("amount is too large")
    return value


# Far above any real refund. Capping here keeps every value well inside Decimal's
# 28-digit precision, so the scaling in to_minor() can never round silently.
_MAX_AMOUNT = Decimal(10) ** 15


def to_minor(amount: Decimal, currency: str) -> int:
    """Convert an exact, non-negative amount to integer minor units: Decimal("4.50") USD -> 450.

    Zero is allowed (free shipping). Raises ValueError instead of rounding.
    """
    exponent = currency_exponent(currency)
    if not isinstance(amount, Decimal):
        raise ValueError(f"amount must be a Decimal, got {type(amount).__name__}")
    if not amount.is_finite():
        raise ValueError("amount must be a finite number")
    if amount < 0:
        raise ValueError("amount must not be negative")
    if amount >= _MAX_AMOUNT:
        raise ValueError("amount is too large")
    scaled = amount.scaleb(exponent)  # 4.50 -> 450.00 for USD
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount has more than {exponent} decimal places for {currency}")
    return int(scaled)


def from_minor(minor: int, currency: str) -> Decimal:
    """Convert integer minor units back to a Decimal amount: 450 USD -> Decimal("4.50")."""
    exponent = currency_exponent(currency)
    # bool is a subclass of int in Python, so it needs an explicit check.
    if not isinstance(minor, int) or isinstance(minor, bool):
        raise ValueError(f"minor units must be an int, got {type(minor).__name__}")
    return Decimal(minor).scaleb(-exponent)
