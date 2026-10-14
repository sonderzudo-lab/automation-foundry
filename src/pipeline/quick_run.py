"""One-click Content Engine run from the operator's own list of topics.

The operator keeps a plain text file with one topic per line. A quick run draws
one topic, preferring those no earlier run has used, picks the structure and the
visual style at random, and fills everything else with the operator's defaults.
The result goes through the same validated manual input as the full form; it
creates a local run and nothing else.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.pipeline.visual_prompts import VISUAL_STYLES

_MAX_TOPICS = 500
_MAX_FILE_BYTES = 256 * 1024
_TOPIC_LENGTH = (3, 500)
# "story" is left out: it still repeats the hook instead of telling a story.
QUICK_RUN_STRUCTURES = ("direct", "list", "myth_fact")


class QuickRunUnavailableError(ValueError):
    """Raised when the topic list or the default persona cannot be used."""


@dataclass(frozen=True, slots=True)
class QuickRunStatus:
    """What the dashboard shows next to the quick-run button."""

    available: bool
    topic_count: int
    unused_count: int
    persona: str
    problem: str | None


def load_topics(path: Path) -> tuple[str, ...]:
    """Read one topic per line; blank lines and lines starting with '#' are ignored."""
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise QuickRunUnavailableError("a lista de pautas passou do tamanho máximo")
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise QuickRunUnavailableError("a lista de pautas não foi encontrada") from exc
    except (OSError, UnicodeError) as exc:
        raise QuickRunUnavailableError("a lista de pautas não pôde ser lida") from exc
    topics: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = " ".join(raw.split())
        if not line or line.startswith("#"):
            continue
        if not _TOPIC_LENGTH[0] <= len(line) <= _TOPIC_LENGTH[1]:
            raise QuickRunUnavailableError(
                f"a pauta da linha {number} deve ter de {_TOPIC_LENGTH[0]} a "
                f"{_TOPIC_LENGTH[1]} caracteres"
            )
        if line not in topics:
            topics.append(line)
    if not topics:
        raise QuickRunUnavailableError("a lista de pautas está vazia")
    if len(topics) > _MAX_TOPICS:
        raise QuickRunUnavailableError(f"a lista de pautas passou de {_MAX_TOPICS} pautas")
    return tuple(topics)


def quick_run_status(
    *,
    topics_path: Path,
    persona: str | None,
    used_topics: Iterable[str],
) -> QuickRunStatus:
    """Summarize whether a quick run can start, without raising."""
    persona_text = (persona or "").strip()
    try:
        topics = load_topics(topics_path)
    except QuickRunUnavailableError as exc:
        return QuickRunStatus(False, 0, 0, persona_text, str(exc))
    used = set(used_topics)
    unused = sum(1 for topic in topics if topic not in used)
    if not 3 <= len(persona_text) <= 1_000:
        return QuickRunStatus(
            False, len(topics), unused, persona_text, "a persona padrão não está configurada"
        )
    return QuickRunStatus(True, len(topics), unused, persona_text, None)


def choose_quick_run(
    topics: Sequence[str],
    *,
    persona: str,
    used_topics: Iterable[str],
    rng: random.Random,
) -> dict[str, str]:
    """Return the manual-form values of one quick run.

    Topics no earlier run has used are drawn first; once every topic has been
    used, any topic may come up again.
    """
    used = set(used_topics)
    fresh = [topic for topic in topics if topic not in used]
    return {
        "topic": rng.choice(fresh or list(topics)),
        "persona": persona.strip(),
        "format": "short",
        "recent_openings": "",
        "facts": "",
        "opening_style": "auto",
        "structure": rng.choice(QUICK_RUN_STRUCTURES),
        "visual_style": rng.choice(sorted(VISUAL_STYLES)),
    }


def generated_idempotency_key(now: datetime, rng: random.Random) -> str:
    """A readable key that is unique in practice, for runs started without one."""
    return f"content-{now:%Y%m%d-%H%M%S}-{rng.getrandbits(16):04x}"
