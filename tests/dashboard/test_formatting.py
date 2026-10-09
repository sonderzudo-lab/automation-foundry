"""Tests for exact, notation-free amount formatting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.dashboard.formatting import (
    format_byte_size,
    format_duration_seconds,
    format_exact_decimal,
    format_relative_label,
    format_utc_label,
)


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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2026, 10, 9, 13, 33), "2026-10-09 13:33 UTC"),
        (datetime(2026, 10, 9, 13, 33, tzinfo=UTC), "2026-10-09 13:33 UTC"),
        (
            datetime(2026, 10, 9, 10, 33, tzinfo=timezone(timedelta(hours=-3))),
            "2026-10-09 13:33 UTC",
        ),
        (None, "—"),
        ("2026-10-09", "—"),
    ],
)
def test_utc_label_is_absolute_and_timezone_safe(value: object, expected: str) -> None:
    assert format_utc_label(value) == expected


_NOW = datetime(2026, 10, 9, 14, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2026, 10, 9, 14, 20, 30), "agora"),
        (datetime(2026, 10, 9, 13, 33), "há 47 min"),
        (datetime(2026, 10, 9, 11, 50), "há 2 h 30 min"),
        (datetime(2026, 10, 9, 12, 20), "há 2 h"),
        (datetime(2026, 10, 7, 16, 40), "há 1 d 21 h"),
        (datetime(2026, 10, 12, 11, 0), "em 2 d 20 h"),
        (None, "—"),
    ],
)
def test_relative_label_counts_in_both_directions(
    value: object,
    expected: str,
) -> None:
    assert format_relative_label(value, _NOW) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0.05"), "0,05 s"),
        (Decimal("12.40"), "12,4 s"),
        (Decimal("59"), "59 s"),
        (102 * 60, "1 h 42 min"),
        (Decimal("252"), "4 min"),
        (None, "—"),
        (Decimal("-1"), "—"),
        (True, "—"),
    ],
)
def test_duration_label_is_compact(value: object, expected: str) -> None:
    assert format_duration_seconds(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1,0 KB"),
        (4300, "4,2 KB"),
        (9_856_000, "9,4 MB"),
        (51_093_299, "48,7 MB"),
        (3 * 1024**3, "3,0 GB"),
        (None, "—"),
        (-1, "—"),
        (True, "—"),
        (1.5, "—"),
    ],
)
def test_byte_size_uses_binary_units(value: object, expected: str) -> None:
    assert format_byte_size(value) == expected
