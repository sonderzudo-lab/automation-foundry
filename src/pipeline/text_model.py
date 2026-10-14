"""Contract for the language model that writes Content Engine text.

The script (A1), the scene plan (A3) and later the niche suggestions all send a
system and a user message and read one answer. This module fixes that boundary
so a provider can be swapped by configuration: the local Ollama model, or the
owner's ChatGPT plan through ``codex exec``.

Rules every provider follows:

- An answer is returned only when it is non-empty after removing reasoning
  blocks; an empty answer is a retryable failure, never an empty string.
- ``TextModelQuotaError`` means the plan's usage limit was reached. It is the
  only failure that hands the request to a fallback provider, so a broken
  configuration is never hidden behind a silent switch of model.
- The result names the provider and model that actually answered, so each
  piece of text can be attributed in the run's metrics.
- A provider never receives credentials in the request and never logs the
  prompt or the answer.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Literal, Protocol

TextModelProviderName = Literal["ollama", "codex_exec"]
TEXT_MODEL_PROVIDERS: tuple[TextModelProviderName, ...] = ("ollama", "codex_exec")

_PURPOSE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_MAX_MESSAGE_CHARS = 60_000


class TextModelRetryableError(RuntimeError):
    """The provider timed out, was unreachable or answered empty; retry unchanged."""


class TextModelQuotaError(TextModelRetryableError):
    """The provider's plan or rate limit was reached; a fallback may answer instead."""


class TextModelPermanentError(RuntimeError):
    """The provider is misconfigured or not signed in; retrying will not help."""


@dataclass(frozen=True, slots=True)
class TextModelRequest:
    """One system and user message pair sent to a text model.

    ``purpose`` labels the call in logs and metrics (for example ``script`` or
    ``scene_plan``). ``response_schema`` asks for a JSON answer with that JSON
    Schema; the caller still validates the answer, because no provider
    guarantees it.
    """

    purpose: str
    system: str
    user: str
    response_schema: Mapping[str, Any] | None = field(default=None, hash=False)

    def __post_init__(self) -> None:
        if not _PURPOSE.fullmatch(self.purpose):
            raise ValueError("purpose must be a short snake_case label")
        if not self.system.strip() or not self.user.strip():
            raise ValueError("system and user messages must not be empty")
        if len(self.system) > _MAX_MESSAGE_CHARS or len(self.user) > _MAX_MESSAGE_CHARS:
            raise ValueError(f"messages must not exceed {_MAX_MESSAGE_CHARS} characters")


@dataclass(frozen=True, slots=True)
class TextModelResult:
    """The answer and who actually wrote it."""

    text: str
    provider: TextModelProviderName
    model_id: str
    duration_seconds: Decimal
    # Provider that was asked first and hit its usage limit, when a fallback answered.
    fallback_from: TextModelProviderName | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("a text model result must not be empty")
        if self.provider not in TEXT_MODEL_PROVIDERS:
            raise ValueError("unknown text model provider")
        if self.duration_seconds < 0:
            raise ValueError("duration must not be negative")


class TextModel(Protocol):
    """A configured provider able to answer one request."""

    @property
    def provider(self) -> TextModelProviderName: ...

    @property
    def model_id(self) -> str: ...

    def complete(self, request: TextModelRequest) -> TextModelResult: ...


@dataclass(frozen=True, slots=True)
class FallbackTextModel:
    """Ask ``primary`` first and ``fallback`` only when the primary hit its usage limit."""

    primary: TextModel
    fallback: TextModel

    @property
    def provider(self) -> TextModelProviderName:
        return self.primary.provider

    @property
    def model_id(self) -> str:
        return self.primary.model_id

    def complete(self, request: TextModelRequest) -> TextModelResult:
        try:
            return self.primary.complete(request)
        except TextModelQuotaError:
            result = self.fallback.complete(request)
            return replace(result, fallback_from=self.primary.provider)


def as_scene_model_call(model: TextModel, *, purpose: str) -> Callable[[str, str], str]:
    """Adapt a text model to the ``(system, user) -> answer`` call used by A3 prompts."""

    def call(system: str, user: str) -> str:
        return model.complete(TextModelRequest(purpose=purpose, system=system, user=user)).text

    return call
