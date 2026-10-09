"""Tests for exact, notation-free amount formatting."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.dashboard.formatting import format_exact_decimal


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0E-10"), "0.00"),
        (Decimal("-0E-10"), "0.00"),
        (0, "0.00"),
        (Decimal("12.5000000000"), "12.50"),
        (Decimal("1E+3"), "1000.00"),
        (Decimal("-3.1000"), "-3.10"),
        (Decimal("0.0000000123"), "0.0000000123"),
        (Decimal("123456789.123456789"), "123456789.123456789"),
    ],
)
def test_amounts_are_positional_and_never_rounded(
    value: object,
    expected: str,
) -> None:
    assert format_exact_decimal(value) == expected


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), "n/a", None, True])
def test_non_numeric_values_are_returned_unchanged(value: object) -> None:
    assert format_exact_decimal(value) == str(value)
