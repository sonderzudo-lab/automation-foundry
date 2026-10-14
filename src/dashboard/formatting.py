"""Display helpers that never change the value they present."""

from __future__ import annotations

from datetime import UTC, datetime
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


def _as_utc(value: datetime) -> datetime:
    """Treat naive datetimes as UTC, which is how the dashboard persists them."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def format_utc_label(value: object) -> str:
    """Render an instant as ``YYYY-MM-DD HH:MM UTC`` or a dash when it is absent."""
    if not isinstance(value, datetime):
        return "—"
    return _as_utc(value).strftime("%Y-%m-%d %H:%M UTC")


def format_relative_label(value: object, now: datetime | None = None) -> str:
    """Render how long ago, or how long until, an instant happens.

    The unit ladder matches the server-side pending-age label: minutes, hours and
    minutes, then days and hours. Anything that is not a datetime renders as a dash.
    """
    if not isinstance(value, datetime):
        return "—"
    reference = _as_utc(now) if now is not None else datetime.now(UTC)
    delta = int((_as_utc(value) - reference).total_seconds())
    span = _format_span(abs(delta))
    if span == "menos de 1 min":
        return "agora"
    return f"em {span}" if delta > 0 else f"há {span}"


def format_duration_seconds(value: object) -> str:
    """Render a duration in seconds as a compact human label, or a dash."""
    if isinstance(value, bool):
        return "—"
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "—"
    if not number.is_finite() or number < 0:
        return "—"
    if number < 60:
        shown = format(number.quantize(Decimal("0.01")), "f").rstrip("0").rstrip(".")
        return f"{shown or '0'} s".replace(".", ",")
    return _format_span(int(number))


def _format_span(total_seconds: int) -> str:
    if total_seconds < 60:
        return "menos de 1 min"
    if total_seconds < 3_600:
        return f"{total_seconds // 60} min"
    if total_seconds < 86_400:
        hours, remainder = divmod(total_seconds, 3_600)
        minutes = remainder // 60
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, remainder = divmod(total_seconds, 86_400)
    hours = remainder // 3_600
    return f"{days} d {hours} h" if hours else f"{days} d"


def format_byte_size(value: object) -> str:
    """Render a byte count with a binary unit, or a dash when it is not a count."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            shown = f"{size:.0f}" if unit == "B" else f"{size:.1f}"
            return f"{shown} {unit}".replace(".", ",")
        size /= 1024
    return "—"  # pragma: no cover - the loop always returns on the last unit
