"""Contract for a Content Engine channel profile.

A channel is the unit the owner manages: a niche, an editorial persona, the
formats it publishes and the look of its videos. Every run made for a channel
reads its defaults from the profile, and later the originality check compares a
video only with earlier videos of the same channel.

This module fixes the validated shape only. Persisting profiles (a table owned
by the Content Engine, with a migration), linking runs to them and the
dashboard pages are a later step. The legacy ``channels`` table is not reused:
it mixes the profile with publishing credentials, which stay out of scope.

The profile holds no secret and no file path. The voice reference is named by
an identifier; resolving it to a recording is the TTS adapter's job.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.pipeline.timeline import CaptionTemplate, VideoFormat
from src.pipeline.visual_prompts import VisualStyle

ChannelFormats = Literal["short", "long", "both"]
ChannelStatus = Literal["draft", "active", "paused", "archived"]
MAX_VIDEOS_PER_DAY = 4

_SLUG = r"^[a-z0-9][a-z0-9-]{1,39}$"
_REFERENCE_ID = r"^[a-z0-9][a-z0-9_-]{0,63}$"


class ChannelProfile(BaseModel):
    """Validated settings of one channel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str = Field(pattern=_SLUG)
    name: str = Field(min_length=2, max_length=80)
    language: Literal["pt-BR"]
    niche: str = Field(min_length=3, max_length=300)
    # Same bounds as the persona of an A1 run, which it fills.
    persona: str = Field(min_length=3, max_length=1_000)
    formats: ChannelFormats
    visual_style: VisualStyle
    # One template per published format, and only for those formats.
    caption_templates: dict[VideoFormat, CaptionTemplate]
    voice_reference_id: str | None = Field(default=None, pattern=_REFERENCE_ID)
    # Videos started automatically per day; 0 means only runs started by hand.
    videos_per_day: int = Field(ge=0, le=MAX_VIDEOS_PER_DAY)
    status: ChannelStatus = "draft"

    @field_validator("name", "niche", "persona")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("value must not be empty")
        return normalized

    @model_validator(mode="after")
    def _check_formats(self) -> ChannelProfile:
        if set(self.caption_templates) != set(self.published_formats()):
            raise ValueError("caption_templates must name exactly the published formats")
        return self

    def published_formats(self) -> tuple[VideoFormat, ...]:
        """The video formats this channel publishes."""
        if self.formats == "both":
            return ("short", "long")
        return (self.formats,)
