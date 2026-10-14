"""
Testes de scripts/experiments/e9_visual_prompts.py — modelo do roteiro mockado.

Cobrem as checagens da descrição, o pedido refeito com o motivo da recusa e a leitura
somente leitura de uma run.
"""

from __future__ import annotations

import json
import wave
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.experiments.e9_visual_prompts import (
    PROMPT_ATTEMPTS,
    load_run,
    prompt_problem,
    prompt_request,
    run,
    write_prompts,
)
from src.pipeline.visuals import VisualPlanSlot, plan_visual_slots

_GOOD = "A wooden desk seen from above with only a pencil, scissors and blank paper in the center"
_OTHER = "A small ceramic tray on the edge of a desk holding pens and paper clips, soft light"
_NARRATION = "Pega só o que vai usar. Põe os objetos soltos num pote. Abre um caderno agora."


def _client(*responses: str) -> MagicMock:
    client = MagicMock()
    replies = []
    for content in responses:
        reply = MagicMock()
        reply.choices[0].message.content = content
        replies.append(reply)
    client.chat.completions.create.side_effect = replies
    return client


def _slots(count: int = 2) -> tuple[VisualPlanSlot, ...]:
    return plan_visual_slots(_NARRATION, duration_seconds=Decimal("12"), count=count)


def test_prompt_problem_accepts_a_plain_english_scene() -> None:
    assert prompt_problem(_GOOD) is None


@pytest.mark.parametrize(
    ("prompt", "fragment"),
    [
        ("A desk", "has 2 words"),
        (" ".join(["desk"] * 61), "has 61 words"),
        ("Uma mesa de madeira vista de cima com lápis, tesoura e papéis no centro", "English"),
        ("A notebook page with a chart and a pencil resting on a wooden desk", "'chart'"),
        ("A wall with a poster above a tidy wooden desk in warm afternoon light", "'poster'"),
    ],
)
def test_prompt_problem_explains_the_refusal(prompt: str, fragment: str) -> None:
    problem = prompt_problem(prompt)
    assert problem is not None and fragment in problem


def test_write_prompts_returns_one_clean_prompt_per_slot() -> None:
    client = _client(f'"{_GOOD}"', f"  {_OTHER}\n")
    prompts = write_prompts(client, topic="Mesa", narration=_NARRATION, slots=_slots())
    assert [(p.index, p.prompt, p.attempts, p.problem) for p in prompts] == [
        (1, _GOOD, 1, None),
        (2, _OTHER, 1, None),
    ]
    second_user = client.chat.completions.create.call_args_list[1].kwargs["messages"][1]
    assert f"- {_GOOD}" in second_user["content"]


def test_write_prompts_retries_with_the_refusal_reason() -> None:
    client = _client("A desk with a logo on a mug and many pens around it today", _GOOD)
    prompts = write_prompts(client, topic="Mesa", narration=_NARRATION, slots=_slots(1))
    assert (prompts[0].prompt, prompts[0].attempts, prompts[0].problem) == (_GOOD, 2, None)
    retry_user = client.chat.completions.create.call_args_list[1].kwargs["messages"][1]
    assert "refused because it mentions 'logo'" in retry_user["content"]


def test_write_prompts_keeps_the_problem_after_the_ceiling() -> None:
    client = _client(*(["Uma mesa de madeira com lápis, tesoura e papéis no centro"] * 3))
    prompts = write_prompts(client, topic="Mesa", narration=_NARRATION, slots=_slots(1))
    assert prompts[0].attempts == PROMPT_ATTEMPTS
    assert prompts[0].problem == "is not written in plain English"


def test_prompt_request_forbids_readable_content_and_people() -> None:
    system, user = prompt_request("Mesa", _NARRATION, _slots(1)[0], [])
    assert "no written words" in system and "No identifiable person" in system
    assert "style is added separately" in system
    assert "(none yet)" in user and _NARRATION in user


def _fake_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run-99"
    (run_dir / "script").mkdir(parents=True)
    (run_dir / "audio").mkdir()
    (run_dir / "script" / "script.json").write_text(
        json.dumps({"topic": "Mesa", "result": {"narration": _NARRATION}}), encoding="utf-8"
    )
    with wave.open(str(run_dir / "audio" / "narration.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 8000 * 12)
    return run_dir


def test_load_run_reads_topic_narration_and_duration(tmp_path: Path) -> None:
    assert load_run(_fake_run(tmp_path)) == ("Mesa", _NARRATION, Decimal("12"))


def test_run_writes_the_prompts_and_refuses_to_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _fake_run(tmp_path)
    monkeypatch.setattr(
        "scripts.experiments.e9_visual_prompts._make_client", lambda: _client(_GOOD, _OTHER)
    )
    output = tmp_path / "saida"
    data = json.loads(run(run_dir, output, count=None).read_text(encoding="utf-8"))
    assert data["scope"] == "quality test only; not approved for monetized content"
    assert [slot["prompt"] for slot in data["slots"]] == [_GOOD, _OTHER]
    assert data["style"] == data["styles"]["photo"]
    assert all("no text" in style for style in data["styles"].values())
    assert _source_files(run_dir) == ["narration.wav", "script.json"]
    with pytest.raises(SystemExit):
        run(run_dir, output, count=None)


def _source_files(run_dir: Path) -> list[str]:
    """A run de origem só é lida: nenhum arquivo novo pode aparecer nela."""
    return sorted(path.name for path in run_dir.rglob("*") if path.is_file())
