"""The text model the Content Engine uses, chosen by configuration.

Every call is logged with its purpose, the provider and model that actually
answered and how long it took, so each piece of text can be attributed. The
prompt and the answer are never logged.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from src.core.config import settings
from src.pipeline.text_model import (
    TextModel,
    TextModelPermanentError,
    TextModelProviderName,
    TextModelRequest,
    TextModelResult,
    TextModelRetryableError,
)
from src.pipeline.text_model_ollama import OllamaTextModel

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ObservedTextModel:
    """Log the attribution and outcome of every call made through ``inner``."""

    inner: TextModel

    @property
    def provider(self) -> TextModelProviderName:
        return self.inner.provider

    @property
    def model_id(self) -> str:
        return self.inner.model_id

    def complete(self, request: TextModelRequest) -> TextModelResult:
        try:
            result = self.inner.complete(request)
        except (TextModelRetryableError, TextModelPermanentError) as exc:
            log.warning(
                "text_model_call_failed",
                purpose=request.purpose,
                provider=self.inner.provider,
                model_id=self.inner.model_id,
                error=type(exc).__name__,
            )
            raise
        log.info(
            "text_model_call_done",
            purpose=request.purpose,
            provider=result.provider,
            model_id=result.model_id,
            fallback_from=result.fallback_from,
            duration_seconds=float(result.duration_seconds),
            answer_chars=len(result.text),
        )
        return result


def configured_ollama_model(*, timeout_seconds: float | None = None) -> OllamaTextModel:
    """The local Ollama model from the settings."""
    return OllamaTextModel(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout_seconds=float(
            settings.ollama_timeout if timeout_seconds is None else timeout_seconds
        ),
    )


def configured_text_model(*, ollama_timeout_seconds: float | None = None) -> TextModel:
    """The configured text model, wrapped so that every call is logged."""
    return ObservedTextModel(configured_ollama_model(timeout_seconds=ollama_timeout_seconds))
