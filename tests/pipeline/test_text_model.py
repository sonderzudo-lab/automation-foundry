"""Contract of the Content Engine text model, with fake providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from src.pipeline.text_model import (
    FallbackTextModel,
    TextModelPermanentError,
    TextModelProviderName,
    TextModelQuotaError,
    TextModelRequest,
    TextModelResult,
    TextModelRetryableError,
    as_scene_model_call,
)

_REQUEST = TextModelRequest(purpose="scene_plan", system="Plan the scenes.", user="Narration.")


@dataclass
class _FakeModel:
    provider: TextModelProviderName
    model_id: str
    answer: str = "ok"
    error: Exception | None = None
    calls: list[TextModelRequest] = field(default_factory=list)

    def complete(self, request: TextModelRequest) -> TextModelResult:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return TextModelResult(
            text=self.answer,
            provider=self.provider,
            model_id=self.model_id,
            duration_seconds=Decimal("1.5"),
        )


@pytest.mark.parametrize(
    ("purpose", "system", "user"),
    [
        ("Scene Plan", "s", "u"),
        ("", "s", "u"),
        ("script", " ", "u"),
        ("script", "s", ""),
        ("script", "s", "u" * 60_001),
    ],
    ids=["not-snake-case", "no-purpose", "blank-system", "empty-user", "too-long"],
)
def test_request_rejects_unlabelled_or_empty_messages(purpose: str, system: str, user: str) -> None:
    with pytest.raises(ValueError):
        TextModelRequest(purpose=purpose, system=system, user=user)


def test_result_rejects_an_empty_answer() -> None:
    with pytest.raises(ValueError, match="empty"):
        TextModelResult(text="  ", provider="ollama", model_id="qwen3:14b",
                        duration_seconds=Decimal(0))


def test_fallback_answers_only_when_the_primary_hit_its_usage_limit() -> None:
    primary = _FakeModel("codex_exec", "gpt", error=TextModelQuotaError("limit"))
    fallback = _FakeModel("ollama", "qwen3:14b", answer="local answer")

    result = FallbackTextModel(primary, fallback).complete(_REQUEST)

    assert result.text == "local answer"
    assert result.provider == "ollama"
    assert result.fallback_from == "codex_exec"
    assert primary.calls == [_REQUEST]
    assert fallback.calls == [_REQUEST]


@pytest.mark.parametrize(
    "error",
    [TextModelRetryableError("timeout"), TextModelPermanentError("not signed in")],
)
def test_fallback_does_not_hide_other_failures(error: Exception) -> None:
    primary = _FakeModel("codex_exec", "gpt", error=error)
    fallback = _FakeModel("ollama", "qwen3:14b")

    with pytest.raises(type(error)):
        FallbackTextModel(primary, fallback).complete(_REQUEST)

    assert fallback.calls == []


def test_fallback_keeps_the_primary_answer_unmarked() -> None:
    primary = _FakeModel("codex_exec", "gpt", answer="plan answer")

    result = FallbackTextModel(primary, _FakeModel("ollama", "qwen3:14b")).complete(_REQUEST)

    assert result.provider == "codex_exec"
    assert result.fallback_from is None


def test_scene_model_call_bridge_sends_a_labelled_request() -> None:
    model = _FakeModel("ollama", "qwen3:14b", answer="A wooden desk")

    call = as_scene_model_call(model, purpose="scene_revision")

    assert call("system text", "user text") == "A wooden desk"
    assert model.calls == [
        TextModelRequest(purpose="scene_revision", system="system text", user="user text")
    ]
