"""Guards for the keyboard behaviour that lives in the static stylesheet and script.

The behaviour itself was verified in a browser; these tests only stop the rules that
make it work from being removed or reworded away by accident.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[2] / "src" / "dashboard" / "static"


def _mobile_block(stylesheet: str) -> str:
    start = stylesheet.index("@media (max-width: 56rem)")
    return stylesheet[start : stylesheet.index("@media (max-width: 40rem)", start)]


def test_mobile_drawer_is_removed_from_the_tab_order_while_closed() -> None:
    block = _mobile_block((_STATIC / "styles.css").read_text(encoding="utf-8"))

    closed = re.search(r"\n\s*\.sidebar\s*\{([^}]*)\}", block)
    opened = re.search(r"\.nav-toggle:checked ~ \.app \.sidebar\s*\{([^}]*)\}", block)

    assert closed is not None and opened is not None
    assert "visibility: hidden" in closed.group(1)
    assert "visibility: visible" in opened.group(1)


def test_script_keeps_dialog_focus_and_scroll_position() -> None:
    script = (_STATIC / "theme.js").read_text(encoding="utf-8")

    for marker in (
        ".dialog-backdrop:target",
        "restoreAfterClose",
        "savedScroll",
        "insideClosedDetails",
        "focusDialogIfOutside",
        'event.key === "Escape"',
    ):
        assert marker in script


def test_script_defers_fragment_swaps_while_focus_is_inside() -> None:
    script = (_STATIC / "theme.js").read_text(encoding="utf-8")

    guard = script[script.index("htmx:beforeSwap") :]

    assert "target.contains(active)" in guard.split("});")[0]
    assert "shouldSwap = false" in guard.split("});")[0]
