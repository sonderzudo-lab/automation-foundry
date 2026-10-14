"""The ``ollama`` text model: the local model behind Ollama's native chat API.

Only the native ``/api/chat`` endpoint accepts a fixed context window. The
OpenAI-compatible endpoint loads the model with its default context, several
times larger, which slowed every call and pushed larger models partly onto the
CPU (see the roadmap entry of 2026-10-10). Thinking is disabled at the server
and any reasoning that still leaks into the answer is removed before the answer
is checked, so a reply made only of reasoning counts as empty.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from src.pipeline.text_model import (
    TextModelPermanentError,
    TextModelProviderName,
    TextModelRequest,
    TextModelResult,
    TextModelRetryableError,
)

# A script or scene request is a few thousand tokens and its answer a few
# thousand more; 8k holds both for the short format and keeps qwen3:14b at about
# 9 GB of VRAM.
OLLAMA_NUM_CTX = 8192
OLLAMA_TIMEOUT_SECONDS = 120.0

# Complete <think>…</think> blocks, with the opening tag present.
_THINK_COMPLETE_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def sanitize_llm_output(text: str) -> str:
    """
    Remove TODOS os artefatos de raciocínio interno (thinking mode) da saída do LLM.

    É a CAMADA 2 de defesa (defensiva), aplicada em toda resposta de modelo de
    texto independentemente de a Camada 1 (``think: false`` + /no_think) ter funcionado.

    Trata os três padrões reais observados no Qwen3 via Ollama:

    Padrão A — bloco completo (tags de abertura E fechamento presentes):
        "<think>raciocínio em inglês...</think>\\n\\nConteúdo em PT-BR."
        → "Conteúdo em PT-BR."

    Padrão B — tag de abertura AUSENTE (padrão mais comum no Ollama):
        "nowrap\\n\\nOkay, the user wants a script about...\\n</think>\\n\\nConteúdo."
        → "Conteúdo."
        O "nowrap" e tudo que vem antes de </think> é raciocínio vazado.

    Padrão C — texto limpo sem nenhum artefato:
        "Conteúdo direto em PT-BR."
        → "Conteúdo direto em PT-BR."  (inalterado)

    Args:
        text: Saída bruta do LLM.

    Returns:
        Texto limpo, sem artefatos de raciocínio, com whitespace normalizado.
    """
    # Passo 1: Remove blocos <think>…</think> completos (Padrão A).
    # Usa re.DOTALL para capturar raciocínio multilinha.
    cleaned = _THINK_COMPLETE_RE.sub("", text)

    # Passo 2: Trata </think> ÓRFÃO — tudo antes (e incluindo) o último </think>
    # é raciocínio que vazou sem tag de abertura (Padrão B).
    closing = "</think>"
    lower = cleaned.lower()
    if closing in lower:
        last_idx = lower.rfind(closing)
        cleaned = cleaned[last_idx + len(closing):]

    # Passo 3: Remove o token "nowrap" que o Qwen3 emite como prefixo de raciocínio.
    # Pode restar após o passo 2 se o think foi totalmente órfão sem </think>.
    cleaned = re.sub(r"^\s*nowrap\b[^\n]*\n?", "", cleaned, flags=re.IGNORECASE)

    return cleaned.strip()


def ollama_native_root(base_url: str) -> str:
    """The native API root for a configured base URL that may end in ``/v1``."""
    return base_url.rstrip("/").removesuffix("/v1")


@dataclass(frozen=True, slots=True)
class OllamaTextModel:
    """One local model served by Ollama, always with a fixed context window."""

    base_url: str
    model: str
    num_ctx: int = OLLAMA_NUM_CTX
    timeout_seconds: float = OLLAMA_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("an Ollama model name is required")
        if not 512 <= self.num_ctx <= 131_072:
            raise ValueError("num_ctx is out of range")
        if not 1 <= self.timeout_seconds <= 3600:
            raise ValueError("timeout_seconds is out of range")

    @property
    def provider(self) -> TextModelProviderName:
        return "ollama"

    @property
    def model_id(self) -> str:
        return self.model

    def complete(self, request: TextModelRequest) -> TextModelResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "stream": False,
            # CAMADA 1 — o servidor desliga o thinking mode do Qwen3 antes de gerar.
            "think": False,
            "options": {"num_ctx": self.num_ctx},
        }
        if request.response_schema is not None:
            # Ollama constrains the answer to this JSON Schema; the caller still checks it.
            payload["format"] = dict(request.response_schema)
        started = time.monotonic()
        try:
            response = httpx.post(
                f"{ollama_native_root(self.base_url)}/api/chat",
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise TextModelRetryableError("ollama timed out") from exc
        except httpx.TransportError as exc:
            raise TextModelRetryableError("ollama is unreachable") from exc
        _raise_for_status(response)
        content = sanitize_llm_output(_answer(response))
        if not content:
            raise TextModelRetryableError("ollama returned an empty answer")
        return TextModelResult(
            text=content,
            provider="ollama",
            model_id=self.model,
            duration_seconds=Decimal(f"{time.monotonic() - started:.3f}"),
        )

    def release(self) -> None:
        """Ask Ollama to unload the model so another model can have the GPU.

        Best effort: if the request fails the next model may run short of memory,
        which surfaces as that model's own retryable failure.
        """
        try:
            httpx.post(
                f"{ollama_native_root(self.base_url)}/api/generate",
                json={"model": self.model, "keep_alive": 0},
                timeout=30.0,
            )
        except httpx.HTTPError:
            return


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    # 404 is a model that was never pulled; other client errors are a request
    # Ollama will refuse again. Server errors (out of memory, loading) may pass.
    if 400 <= response.status_code < 500:
        raise TextModelPermanentError(f"ollama refused the request ({response.status_code})")
    raise TextModelRetryableError(f"ollama failed ({response.status_code})")


def _answer(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError as exc:
        raise TextModelRetryableError("ollama answered with invalid JSON") from exc
    message = body.get("message") if isinstance(body, Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    return content if isinstance(content, str) else ""
