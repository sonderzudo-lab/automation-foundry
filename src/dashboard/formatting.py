"""Display helpers that never change the value they present."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

_MINIMUM_FRACTION_DIGITS = 2


def format_exact_decimal(value: object) -> str:
    """Render a ledger amount in plain notation without rounding it.

    ``Decimal`` prints a zero with a wide exponent as ``0E-10``. This shows the same
    exact value in positional notation, dropping only redundant trailing zeros but
    keeping at least two fraction digits. Anything that is not a finite number is
    returned unchanged as text instead of being guessed at.
    """
    if isinstance(value, bool):
        return str(value)
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if not number.is_finite():
        return str(value)
    if number == 0:
        number = Decimal(0)
    whole, _, fraction = format(number, "f").partition(".")
    fraction = fraction.rstrip("0").ljust(_MINIMUM_FRACTION_DIGITS, "0")
    return f"{whole}.{fraction}"
