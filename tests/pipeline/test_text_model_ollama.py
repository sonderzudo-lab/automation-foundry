"""The ``ollama`` text model against a mocked native API."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
import respx
import structlog

from src.pipeline.text_model import (
    TextModelPermanentError,
    TextModelProviderName,
    TextModelRequest,
    TextModelResult,
    TextModelRetryableError,
)
from src.pipeline.text_model_factory import ObservedTextModel
from src.pipeline.text_model_ollama import OLLAMA_NUM_CTX, OllamaTextModel

_CHAT = "http://127.0.0.1:11434/api/chat"
_MODEL = OllamaTextModel(base_url="http://127.0.0.1:11434/v1", model="qwen3:14b")
_REQUEST = TextModelRequest(purpose="script_angle", system="/no_think\nSistema", user="Pauta")


def _reply(content: object) -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})


def test_calls_the_native_api_with_a_fixed_context_and_thinking_off() -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.post(_CHAT).mock(return_value=_reply("Resposta."))
        result = _MODEL.complete(_REQUEST)

    body = json.loads(route.calls[0].request.content)
    assert body == {
        "model": "qwen3:14b",
        "messages": [
            {"role": "system", "content": "/no_think\nSistema"},
            {"role": "user", "content": "Pauta"},
        ],
        "stream": False,
        "think": False,
        "options": {"num_ctx": OLLAMA_NUM_CTX},
    }
    assert (result.text, result.provider, result.model_id) == ("Resposta.", "ollama", "qwen3:14b")
    assert result.fallback_from is None
    assert result.duration_seconds >= 0


def test_a_response_schema_constrains_the_answer_format() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    request = TextModelRequest(
        purpose="scene_plan", system="Plan.", user="Narration.", response_schema=schema
    )
    with respx.mock(assert_all_called=True) as router:
        route = router.post(_CHAT).mock(return_value=_reply('{"a": "b"}'))
        _MODEL.complete(request)

    assert json.loads(route.calls[0].request.content)["format"] == schema


def test_reasoning_is_removed_from_the_answer() -> None:
    raw = "nowrap\nOkay, the user wants...\n</think>\n\nO sono consolida memórias."
    with respx.mock() as router:
        router.post(_CHAT).mock(return_value=_reply(raw))
        assert _MODEL.complete(_REQUEST).text == "O sono consolida memórias."


@pytest.mark.parametrize(
    "content", ["   ", "<think>só raciocínio</think>", None], ids=["blank", "think", "null"]
)
def test_an_empty_answer_is_retryable(content: object) -> None:
    with respx.mock() as router:
        router.post(_CHAT).mock(return_value=_reply(content))
        with pytest.raises(TextModelRetryableError, match="empty"):
            _MODEL.complete(_REQUEST)


@pytest.mark.parametrize(
    "response",
    [
        httpx.TimeoutException("slow"),
        httpx.ConnectError("refused"),
        httpx.Response(500, json={"error": "out of memory"}),
        httpx.Response(200, text="not json"),
    ],
    ids=["timeout", "connection", "server-error", "invalid-json"],
)
def test_transient_failures_are_retryable(response: httpx.Response | Exception) -> None:
    with respx.mock() as router:
        router.post(_CHAT).mock(side_effect=[response])
        with pytest.raises(TextModelRetryableError):
            _MODEL.complete(_REQUEST)


def test_a_model_that_was_never_pulled_is_permanent() -> None:
    with respx.mock() as router:
        router.post(_CHAT).mock(
            return_value=httpx.Response(404, json={"error": "model 'qwen3:14b' not found"})
        )
        with pytest.raises(TextModelPermanentError):
            _MODEL.complete(_REQUEST)


def test_release_unloads_the_model_and_ignores_failures() -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.post("http://127.0.0.1:11434/api/generate").mock(
            side_effect=[httpx.Response(200, json={}), httpx.ConnectError("down")]
        )
        _MODEL.release()
        _MODEL.release()

    assert json.loads(route.calls[0].request.content) == {"model": "qwen3:14b", "keep_alive": 0}


@pytest.mark.parametrize(
    "kwargs",
    [{"model": " "}, {"num_ctx": 100}, {"timeout_seconds": 0}],
    ids=["no-model", "tiny-context", "no-timeout"],
)
def test_configuration_is_validated(kwargs: dict[str, object]) -> None:
    arguments: dict[str, object] = {"base_url": "http://127.0.0.1:11434", "model": "qwen3:14b"}
    with pytest.raises(ValueError):
        OllamaTextModel(**{**arguments, **kwargs})  # type: ignore[arg-type]


class _Answering:
    provider: TextModelProviderName = "ollama"
    model_id = "qwen3:14b"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def complete(self, request: TextModelRequest) -> TextModelResult:
        if self.error is not None:
            raise self.error
        return TextModelResult(
            text="segredo da resposta",
            provider="ollama",
            model_id="qwen3:14b",
            duration_seconds=Decimal("2.5"),
        )


def test_every_call_is_logged_with_its_attribution_but_not_its_text() -> None:
    with structlog.testing.capture_logs() as logs:
        ObservedTextModel(_Answering()).complete(_REQUEST)
        with pytest.raises(TextModelRetryableError):
            ObservedTextModel(_Answering(TextModelRetryableError("x"))).complete(_REQUEST)

    assert logs == [
        {
            "event": "text_model_call_done",
            "log_level": "info",
            "purpose": "script_angle",
            "provider": "ollama",
            "model_id": "qwen3:14b",
            "fallback_from": None,
            "duration_seconds": 2.5,
            "answer_chars": len("segredo da resposta"),
        },
        {
            "event": "text_model_call_failed",
            "log_level": "warning",
            "purpose": "script_angle",
            "provider": "ollama",
            "model_id": "qwen3:14b",
            "error": "TextModelRetryableError",
        },
    ]
    assert "segredo" not in str(logs) and "Pauta" not in str(logs)
