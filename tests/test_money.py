from decimal import Decimal

import pytest

from wardlock.money import currency_exponent, from_minor, to_minor, to_paypal_value


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        (Decimal("4"), "USD", "4.00"),
        (Decimal("4.5"), "USD", "4.50"),
        (Decimal("4.000"), "USD", "4.00"),  # trailing zeros are exact, so they are fine
        (Decimal("1E+2"), "USD", "100.00"),  # exponent notation is written out in full
        (Decimal("500"), "JPY", "500"),
        (Decimal("500.00"), "JPY", "500"),
    ],
)
def test_formats_exact_amounts(amount, currency, expected):
    assert to_paypal_value(amount, currency) == expected


def test_three_times_19_99_is_exactly_59_97():
    # The float version of this sum is 59.97000000000001 (SPEC T18).
    assert to_paypal_value(Decimal("19.99") * 3, "USD") == "59.97"


@pytest.mark.parametrize(
    ("amount", "currency"),
    [
        (4.0, "USD"),  # float
        (4, "USD"),  # int
        (True, "USD"),  # bool
        ("4.00", "USD"),  # str
        (Decimal("0"), "USD"),
        (Decimal("-0"), "USD"),
        (Decimal("-1.00"), "USD"),
        (Decimal("NaN"), "USD"),
        (Decimal("sNaN"), "USD"),
        (Decimal("Infinity"), "USD"),
        (Decimal("4.001"), "USD"),  # would need rounding
        (Decimal("1.5"), "JPY"),  # zero-decimal currency
        (Decimal("1E+40"), "USD"),  # too large
        (Decimal("4.00"), "usd"),  # codes are upper-case
        (Decimal("4.00"), "XYZ"),
    ],
)
def test_rejects_anything_not_exactly_representable(amount, currency):
    with pytest.raises(ValueError):
        to_paypal_value(amount, currency)


def test_currency_exponent():
    assert currency_exponent("USD") == 2
    assert currency_exponent("EUR") == 2
    assert currency_exponent("JPY") == 0
    assert currency_exponent("HUF") == 0
    assert currency_exponent("TWD") == 0
    with pytest.raises(ValueError):
        currency_exponent("ABC")


@pytest.mark.parametrize(
    ("amount", "currency", "minor"),
    [
        (Decimal("4.50"), "USD", 450),
        (Decimal("4.5"), "USD", 450),
        (Decimal("0"), "USD", 0),  # free shipping is allowed here
        (Decimal("0.01"), "USD", 1),
        (Decimal("1E+2"), "USD", 10000),
        (Decimal("500"), "JPY", 500),
    ],
)
def test_to_minor_is_exact(amount, currency, minor):
    assert to_minor(amount, currency) == minor
    assert isinstance(to_minor(amount, currency), int)


@pytest.mark.parametrize(
    ("amount", "currency"),
    [
        (4.5, "USD"),
        (450, "USD"),
        (Decimal("-0.01"), "USD"),
        (Decimal("0.001"), "USD"),
        (Decimal("1.5"), "JPY"),
        (Decimal("NaN"), "USD"),
        (Decimal("1E+15"), "USD"),  # beyond any real amount; keeps Decimal math exact
        (Decimal("1"), "XYZ"),
    ],
)
def test_to_minor_rejects_instead_of_rounding(amount, currency):
    with pytest.raises(ValueError):
        to_minor(amount, currency)


def test_from_minor_round_trips():
    assert from_minor(450, "USD") == Decimal("4.50")
    assert str(from_minor(450, "USD")) == "4.50"
    assert from_minor(500, "JPY") == Decimal("500")
    assert to_minor(from_minor(5997, "USD"), "USD") == 5997
    with pytest.raises(ValueError):
        from_minor(True, "USD")
    with pytest.raises(ValueError):
        from_minor(4.5, "USD")
