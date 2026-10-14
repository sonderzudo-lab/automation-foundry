"""One-click Content Engine run drawn from the operator's topic list."""

from __future__ import annotations

import random
import re
from datetime import datetime
from pathlib import Path

import pytest

from src.pipeline.quick_run import (
    QUICK_RUN_STRUCTURES,
    QuickRunUnavailableError,
    choose_quick_run,
    generated_idempotency_key,
    load_topics,
    quick_run_status,
)
from src.pipeline.visual_prompts import VISUAL_STYLES
from src.platform.executor_registry import parse_registered_manual_input

_PERSONA = "Criador brasileiro falando de forma próxima e clara"


def _topics_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "pautas.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_topics_skips_comments_blank_lines_and_repeats(tmp_path: Path) -> None:
    path = _topics_file(
        tmp_path,
        "﻿# minhas pautas\n\nPausas curtas no estudo\n  Pausas   curtas no estudo \n"
        "Planejar o dia antes de dormir\n",
    )
    assert load_topics(path) == ("Pausas curtas no estudo", "Planejar o dia antes de dormir")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# só comentário\n\n", "vazia"),
        ("ok\n", "linha 1"),
        ("Pauta boa\n" + "x" * 501 + "\n", "linha 2"),
    ],
)
def test_load_topics_explains_what_is_wrong(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(QuickRunUnavailableError, match=message):
        load_topics(_topics_file(tmp_path, text))


def test_load_topics_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(QuickRunUnavailableError, match="não foi encontrada"):
        load_topics(tmp_path / "nenhuma.txt")


def test_status_counts_unused_topics_and_needs_a_persona(tmp_path: Path) -> None:
    path = _topics_file(tmp_path, "Pauta A\nPauta B\nPauta C\n")
    ready = quick_run_status(topics_path=path, persona=_PERSONA, used_topics={"Pauta B"})
    assert (ready.available, ready.topic_count, ready.unused_count, ready.problem) == (
        True,
        3,
        2,
        None,
    )
    no_persona = quick_run_status(topics_path=path, persona="  ", used_topics=())
    assert no_persona.available is False and "persona" in (no_persona.problem or "")
    no_file = quick_run_status(topics_path=tmp_path / "x.txt", persona=_PERSONA, used_topics=())
    assert no_file.available is False and "não foi encontrada" in (no_file.problem or "")


def test_choose_quick_run_prefers_unused_topics_then_reuses_any() -> None:
    topics = ("Pauta A", "Pauta B", "Pauta C")
    rng = random.Random(7)
    for _ in range(20):
        values = choose_quick_run(
            topics, persona=_PERSONA, used_topics={"Pauta A", "Pauta C"}, rng=rng
        )
        assert values["topic"] == "Pauta B"
        assert values["structure"] in QUICK_RUN_STRUCTURES
        assert values["visual_style"] in VISUAL_STYLES
    seen = {
        choose_quick_run(topics, persona=_PERSONA, used_topics=set(topics), rng=rng)["topic"]
        for _ in range(50)
    }
    assert seen == set(topics)


def test_quick_run_values_pass_the_same_validation_as_the_full_form() -> None:
    values = choose_quick_run(
        ("Planejar o dia antes de dormir",),
        persona=f"  {_PERSONA}  ",
        used_topics=(),
        rng=random.Random(1),
    )
    payload = parse_registered_manual_input(slug="content-engine", values=values)
    assert payload["topic"] == "Planejar o dia antes de dormir"
    assert payload["persona"] == _PERSONA
    assert payload["format"] == "short"
    assert payload["opening_style"] == "auto"
    assert "facts" not in payload


def test_generated_idempotency_key_is_readable_and_varies() -> None:
    rng = random.Random(3)
    now = datetime(2026, 10, 14, 12, 4, 31)
    first = generated_idempotency_key(now, rng)
    second = generated_idempotency_key(now, rng)
    assert re.fullmatch(r"content-20261014-120431-[0-9a-f]{4}", first)
    assert first != second
